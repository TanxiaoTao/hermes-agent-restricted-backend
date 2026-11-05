"""Tests for the ``restricted`` terminal execution backend.

Covers the standalone pieces added in ``tools/environments/restricted.py`` — path
validation, constructor guards, runtime cwd interception, the env whitelist, and the
builder dispatch. These are unit tests: they do not exercise the heavy BaseEnvironment
spawn/wait path (no subprocess, no bash), only the pure guards and the reused helpers.

Behavior contracts, not snapshots: nothing asserts on host env values or enumeration
counts.
"""

import os

import pytest

from tools.environments.restricted import (
    RestrictedEnvironment,
    is_path_within,
    resolve_real_path,
)


# --- 1. Path validation (pure functions) --------------------------------------

class TestIsPathWithin:
    def test_same_root(self, tmp_path):
        root = str(tmp_path / "root")
        assert is_path_within(root, root) is True

    def test_subdirectory_allowed(self, tmp_path):
        root = tmp_path / "root"
        assert is_path_within(str(root), str(root / "a" / "b")) is True

    def test_parent_escape_rejected(self, tmp_path):
        root = tmp_path / "root"
        assert is_path_within(str(root), str(tmp_path)) is False
        # literal ``..`` traversal resolves back to the parent
        assert is_path_within(str(root), str(root / ".." / "sibling")) is False

    def test_sibling_with_shared_prefix_rejected(self, tmp_path):
        # "/root" must not be treated as a parent of "/root2" (the classic prefix bug).
        assert is_path_within(str(tmp_path / "root"), str(tmp_path / "root2")) is False

    def test_symlink_escape_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/privilege level")
        # realpath follows the link, so the link and anything under it are outside.
        assert is_path_within(str(root), str(link)) is False
        assert is_path_within(str(root), str(link / "sub")) is False

    @pytest.mark.platforms("windows")
    def test_cross_drive_rejected(self, tmp_path):
        # commonpath raises on two drives -> is_path_within fails closed.
        root_drive = os.path.splitdrive(str(tmp_path))[0][:1].upper()
        other_drive = "Z" if root_drive != "Z" else "Y"
        assert is_path_within(str(tmp_path / "root"), f"{other_drive}:\\elsewhere") is False


class TestResolveRealPath:
    def test_absolute_and_resolves_dotdot(self, tmp_path):
        target = tmp_path / "root"
        assert resolve_real_path(str(target / ".." / "root")) == os.path.realpath(str(target))


# --- 2. Constructor guards ----------------------------------------------------

class TestConstruction:
    def test_missing_allowed_workdir_raises(self):
        with pytest.raises(ValueError, match="allowed_workdir"):
            RestrictedEnvironment(allowed_workdir=None)

    def test_workdir_not_a_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            RestrictedEnvironment(allowed_workdir=str(tmp_path / "does-not-exist"))

    def test_initial_cwd_escape_raises(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError, match="escapes"):
            RestrictedEnvironment(cwd=str(outside), allowed_workdir=str(root))

    def test_initial_cwd_defaults_to_root_when_empty(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        env = RestrictedEnvironment(cwd="", timeout=5, allowed_workdir=str(root))
        assert env.cwd == os.path.realpath(str(root))


# --- 3. Runtime cwd interception (execute / _validate_cwd) ---------------------

class TestExecuteCwdGuard:
    def test_dynamic_cwd_escape_refused_without_spawn(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        env = RestrictedEnvironment(cwd=str(root), timeout=5, allowed_workdir=str(root))
        result = env.execute("echo should-not-run", cwd=str(outside))
        assert result["returncode"] == 126
        assert "refused" in result["output"]
        assert "should-not-run" not in result["output"]

    def test_validate_cwd_accepts_subdir(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        sub = root / "sub"
        env = RestrictedEnvironment(cwd=str(root), timeout=5, allowed_workdir=str(root))
        assert env._validate_cwd(str(sub)) == os.path.realpath(str(sub))

    def test_validate_cwd_rejects_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        env = RestrictedEnvironment(cwd=str(root), timeout=5, allowed_workdir=str(root))
        with pytest.raises(ValueError, match="escapes"):
            env._validate_cwd(str(outside))

    def test_extract_cwd_rolls_back_on_msys_escape(self, tmp_path):
        """Regression: Git Bash emits MSYS paths (/c/...) via the stdout cwd marker.
        Normalizing one that escapes the locked root must ROLL BACK self.cwd, not
        pollute it into an un-spawnable /c/... form that the next execute rejects."""
        root = tmp_path / "root"
        root.mkdir()
        env = RestrictedEnvironment(cwd=str(root), timeout=5, allowed_workdir=str(root))
        env._session_id = "testsid"
        env._cwd_marker = "__HERMES_CWD_testsid__"
        prev = env.cwd
        # MSYS path to C:\Windows (outside root)
        result = {"output": "\n__HERMES_CWD_testsid__/c/Windows__HERMES_CWD_testsid__\n",
                  "returncode": 0}
        env._extract_cwd_from_output(result)
        assert env.cwd == prev  # rolled back, never /c/Windows
        assert "cwd" not in result or result.get("cwd") is None or result.get("cwd") == prev


# --- 4. Env whitelist ---------------------------------------------------------

def _make_rooted_env(tmp_path, *, allowlist):
    root = tmp_path / "root"
    root.mkdir()
    return RestrictedEnvironment(
        cwd=str(root), timeout=5,
        allowed_workdir=str(root), env_allowlist=allowlist,
    )


class TestEnvWhitelist:
    def test_host_extra_var_not_leaked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_EXTRA_TEST_VAR", "leak-me")
        env = _make_rooted_env(tmp_path, allowlist=["PATH"])
        child = env._build_whitelisted_env()
        assert not any(k.upper() == "HERMES_EXTRA_TEST_VAR" for k in child)

    def test_allowlisted_var_included(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_ALLOWED_VAR", "hello")
        env = _make_rooted_env(tmp_path, allowlist=["MY_ALLOWED_VAR"])
        child = env._build_whitelisted_env()
        assert any(k.upper() == "MY_ALLOWED_VAR" and v == "hello" for k, v in child.items())

    def test_path_always_present_even_with_empty_allowlist(self, tmp_path):
        env = _make_rooted_env(tmp_path, allowlist=[])
        child = env._build_whitelisted_env()
        assert any(k.upper() == "PATH" for k in child)


# --- 5. Builder dispatch (registration from Step 2) ----------------------------

class TestBuilderDispatch:
    def test_backend_registered_in_builders(self):
        from tools import terminal_tool_backends as backends
        assert "restricted" in backends._ENV_BUILDERS

    def test_builder_instantiates_restricted_environment(self, tmp_path):
        from tools import terminal_tool_backends as backends
        root = tmp_path / "root"
        root.mkdir()
        env = backends._build_restricted_env(
            cwd=str(root), timeout=5,
            local_config={"workdir": str(root), "env_allowlist": ["PATH"]},
        )
        assert isinstance(env, RestrictedEnvironment)
        assert env._allowed_root == os.path.realpath(str(root))



