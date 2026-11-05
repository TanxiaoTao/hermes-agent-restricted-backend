"""Restricted execution environment — a local spawn-per-call backend hardened for
untrusted commands.

Unlike :class:`tools.environments.local.LocalEnvironment`, this backend does NOT
inherit the host environment or the user's login shell: it runs every command with
``bash -c`` under a **whitelisted** environment and a **locked** working directory.

Security model (no seccomp / no namespaces — process-level guards only):

* **Subprocess** — every command spawns a fresh ``bash -c`` via ``subprocess.Popen``,
  reusing the bash resolver and process-group kill from ``local``.
* **Timeout** — enforced by the shared ``BaseEnvironment.execute`` template
  (``_wait_for_process`` poll loop + ``run_bounded_sync`` hard backstop); ``_kill_process``
  / ``_force_kill_process`` tear down the whole process group.
* **Locked workdir (path-traversal guard)** — ``allowed_workdir`` is resolved to a real
  absolute path once; every ``execute`` (and every spawn) re-validates that the effective
  cwd stays inside it, resolving symlinks, so ``..`` / ``//`` / symlink escapes are refused
  before a process starts.
* **Env whitelist** — the child env is built ONLY from ``env_allowlist`` plus a small
  always-included set; the host ``os.environ`` is never passed through wholesale, and the
  shared secret scrubber is still applied as defense-in-depth (an allowlisted provider key
  still cannot leak).

The login-shell session snapshot (``init_session``) is deliberately skipped: sourcing
``~/.bashrc`` / ``~/.profile`` would re-import host state and defeat the whitelist.
"""

import contextlib
import logging
import os
import signal
import subprocess

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _pipe_stdin
from tools.environments.local import (
    _IS_WINDOWS,
    _SANE_PATH,
    _append_missing_sane_path_entries,
    _filter_secret_env,
    _finalize_child_env,
    _find_bash,
    _kill_process_group_posix,
    _kill_process_windows,
    _msys_to_windows_path,
    _path_env_key,
    _plugin_terminal_env_strip_keys,
    _prepend_git_bash_dirs,
    _quote_bash_path,
    _windows_to_msys_path,
)

logger = logging.getLogger(__name__)

# Always-included env names, merged with the configured allowlist so the child shell
# remains minimally usable even when the user allowlists nothing. PATH is special-cased
# below (a sane default is injected when absent). Kept intentionally small: users opt in
# to everything else via ``terminal.restricted_env_allowlist``.
_ALWAYS_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def resolve_real_path(path: str) -> str:
    """Expand ``~`` / env vars and resolve to an absolute real path (symlinks followed)."""
    expanded = os.path.expandvars(os.path.expanduser(path or ""))
    return os.path.realpath(os.path.abspath(expanded))


def is_path_within(root: str, target: str) -> bool:
    """True when *target* equals *root* or is a descendant, after resolving symlinks.

    ``os.path.commonpath`` is case-normalised on Windows, so ``C:\\Srv`` vs ``c:\\srv``
    compare equal; a ValueError (different drives) fails closed.
    """
    try:
        root_real = os.path.realpath(os.path.abspath(root))
        target_real = os.path.realpath(os.path.abspath(target))
    except (OSError, ValueError):
        return False
    try:
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:
        return False


class RestrictedEnvironment(BaseEnvironment):
    """Run commands on this host, but only inside ``allowed_workdir`` and only with an
    allowlisted environment. ``_run_bash`` / ``cleanup`` are the only base hooks required;
    timeout, output collection and process waiting come from the base ``execute`` template.
    """

    # Commands run on the Hermes host itself (controller-side platform behavior applies),
    # but cwd is locked and env is whitelisted.
    is_local = True

    # No login-shell snapshot: keep the child on the non-login ``bash -c`` path so
    # ``~/.bashrc`` / profile init files never re-import host env or host cwd state.
    _profile_scoped_passthrough = False

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None,
                 allowed_workdir: str = None, env_allowlist: list = None):
        if not allowed_workdir:
            raise ValueError(
                "RestrictedEnvironment requires an allowed_workdir "
                "(set terminal.restricted_workdir in config.yaml)")
        root = resolve_real_path(str(allowed_workdir))
        if not os.path.isdir(root):
            raise ValueError(
                f"RestrictedEnvironment allowed_workdir {allowed_workdir!r} is not a directory")
        self._allowed_root = root
        self._env_allowlist = tuple(
            str(name) for name in (env_allowlist or []) if str(name).strip())

        # Reject an out-of-bounds initial cwd up front (fail loud, don't silently clamp).
        target = resolve_real_path(cwd) if cwd else root
        if not is_path_within(root, target):
            raise ValueError(
                f"RestrictedEnvironment: cwd {cwd!r} escapes the allowed workdir {root!r}")

        super().__init__(cwd=target, timeout=timeout, env=env)

        # Skip init_session() (login-shell snapshot); force the non-login path instead.
        self._prefer_nonlogin = True

    # --- Path / env helpers -----------------------------------------------------
    def _validate_cwd(self, cwd: str) -> str:
        """Resolve *cwd* and raise when it escapes the locked root."""
        target = resolve_real_path(cwd or self._allowed_root)
        if not is_path_within(self._allowed_root, target):
            raise ValueError(
                f"cwd {cwd!r} escapes the allowed workdir {self._allowed_root!r}")
        return target

    def _ensure_cwd_valid(self) -> str:
        """Return a spawnable cwd inside the locked root; reset to the root when the
        current cwd vanished (a prior command ``rm -rf``'d it) or escaped."""
        try:
            target = resolve_real_path(self.cwd)
            if not os.path.isdir(target) or not is_path_within(self._allowed_root, target):
                target = self._allowed_root
        except (OSError, ValueError):
            target = self._allowed_root
        self.cwd = target
        return target

    def _build_whitelisted_env(self) -> dict:
        """Build the child env from the allowlist only — never ``os.environ`` wholesale."""
        allowed = {
            name.upper()
            for name in (*self._env_allowlist, *_ALWAYS_ALLOW)
            if str(name).strip()
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}

        # Guarantee a usable PATH even when the allowlist omits it.
        path_key = _path_env_key(env)
        if path_key is None:
            env["PATH"] = _SANE_PATH
        else:
            env[path_key] = _prepend_git_bash_dirs(
                _append_missing_sane_path_entries(env[path_key]))

        # Defense-in-depth: strip Hermes-managed / provider secrets that a permissive
        # allowlist might otherwise let through (mirrors local's secret-scrub invariant).
        scrubbed: dict = {}
        _filter_secret_env(env, scrubbed, unwrap_force=False,
                           plugin_strip=_plugin_terminal_env_strip_keys())
        return _finalize_child_env(scrubbed)

    # --- Unified execute (cwd lock is enforced before the base template runs) ----
    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None,
                stdin_data: str | None = None, rewrite_compound_background: bool = True,
                bounded_capture: bool = False, yield_handler=None) -> dict:
        try:
            locked = self._validate_cwd(cwd or self.cwd)
        except ValueError as exc:
            return {"output": f"[RestrictedEnvironment refused: {exc}]", "returncode": 126}
        self.cwd = locked
        return super().execute(
            command, cwd=locked, timeout=timeout, stdin_data=stdin_data,
            rewrite_compound_background=rewrite_compound_background,
            bounded_capture=bounded_capture, yield_handler=yield_handler)

    # --- Abstract / overridden hooks -------------------------------------------
    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        # login is ignored on purpose: this backend must never source the login shell.
        cwd = self._ensure_cwd_valid()
        args = [bash, "-c", cmd_string]
        proc = subprocess.Popen(
            args, text=True, env=self._build_whitelisted_env(),
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            start_new_session=True, cwd=cwd,
            **( {"creationflags": windows_hide_flags()} if _IS_WINDOWS else {} ))
        if not _IS_WINDOWS:
            with contextlib.suppress(ProcessLookupError):
                proc._hermes_pgid = os.getpgid(proc.pid)
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _kill_process(self, proc):
        """Kill the entire process group (all children)."""
        try:
            (_kill_process_windows if _IS_WINDOWS else _kill_process_group_posix)(proc)
        except OSError:  # ProcessLookupError / PermissionError included
            with contextlib.suppress(Exception):
                proc.kill()

    def _force_kill_process(self, proc):
        """SIGKILL the whole group with no TERM grace: the caller os._exit()s next."""
        if _IS_WINDOWS:
            return self._kill_process(proc)
        with contextlib.suppress(OSError):
            pgid = getattr(proc, "_hermes_pgid", None) or os.getpgid(proc.pid)
            if pgid != os.getpgrp():  # never our own group
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()

    # --- Windows Git Bash path handling (mirrors LocalEnvironment) -------------
    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        return BaseEnvironment._quote_cwd_for_cd(_windows_to_msys_path(cwd))

    def _quote_shell_path(self, path: str) -> str:
        return _quote_bash_path(path)

    def _extract_cwd_from_output(self, result: dict):
        """Base sets ``self.cwd`` from the stdout marker, but Git Bash emits the MSYS
        form (``/c/Users/...``) which the next ``_validate_cwd`` would misread as an
        escape. Normalize MSYS -> native (Windows only) and require it to stay inside
        the locked root; otherwise roll back to the previous cwd."""
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd:
            normalized = _msys_to_windows_path(self.cwd)
            if (normalized and os.path.isdir(normalized)
                    and is_path_within(self._allowed_root, normalized)):
                self.cwd = normalized
                result["cwd"] = normalized
            else:
                self.cwd = prev_cwd
                result.pop("cwd_observed", None)
                result.pop("cwd", None)

    def cleanup(self):
        """No container/connection to release; the snapshot/cwd temp files are never
        created (init_session is skipped), so this is a best-effort unlink for safety."""
        for path in (self._snapshot_path, self._cwd_file):
            with contextlib.suppress(OSError):
                os.unlink(path)
