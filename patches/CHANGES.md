# CHANGES — the 11 upstream files to modify

To register the `restricted` backend, apply the edits below on a checkout of
[Hermes Agent](https://github.com/nousresearch/hermes-agent). Each entry gives the exact
location and the key snippet; the surrounding code is unchanged (minimal, non-invasive).

Two files are **new** and provided in full in this repo (no upstream edit needed):

- `tools/environments/restricted.py` — the backend itself (`RestrictedEnvironment`)
- `tests/tools/test_restricted_environment.py` — the test suite

---

## 1. `tools/terminal_tool_backends.py`

**(a) import** (near the other `tools.environments.*` imports):

```python
from tools.environments.restricted import RestrictedEnvironment as _RestrictedEnvironment
```

**(b) add a config shaper** (near `_container_config_from_config`):

```python
def _restricted_config_from_config(config: Dict[str, Any]) -> dict:
    """``local_config`` for the restricted backend (shared with ``ensure_task_env``)."""
    return {
        "workdir": config.get("restricted_workdir", ""),
        "env_allowlist": config.get("restricted_env_allowlist", []),
    }
```

**(c) add a builder** (near `_build_local_env`):

```python
def _build_restricted_env(*, cwd, timeout, local_config=None, **_):
    local_config = local_config or {}
    workdir = local_config.get("workdir")
    if not workdir:
        raise ValueError("restricted backend requires terminal.restricted_workdir")
    # The shared cwd resolver defaults non-local backends to "/root"; a locked local
    # sandbox's initial cwd is its allowed_workdir unless the caller's cwd is inside it.
    from tools.environments.restricted import is_path_within, resolve_real_path
    try:
        if not cwd or not is_path_within(resolve_real_path(workdir), resolve_real_path(cwd)):
            cwd = workdir
    except (OSError, ValueError):
        cwd = workdir
    return _RestrictedEnvironment(
        cwd=cwd, timeout=timeout,
        allowed_workdir=workdir,
        env_allowlist=local_config.get("env_allowlist", []),
    )
```

**(d) register in `_ENV_BUILDERS`:**

```python
_ENV_BUILDERS = {..., "ssh": _build_ssh_env, "restricted": _build_restricted_env}
```

**(e) add a requirements check** (near `_daytona_post`, then register in `_BACKEND_SPECS`):

```python
def _restricted_pre(config: Dict[str, Any]) -> bool:
    if config.get("restricted_workdir"):
        return True
    return _reject("the restricted backend requires terminal.restricted_workdir ...")

_BACKEND_SPECS: Dict[str, Dict[str, Any]] = {
    "local": {},
    "restricted": {"pre": _restricted_pre},
    ...
}
```

**(f) add to the built-in name string:**

```python
_BUILTIN_BACKENDS = "local, docker, singularity, modal, daytona, vercel_sandbox, ssh, restricted"
```

**(g) forward `local_config` in `_create_environment` kwargs:**

```python
kwargs = dict(..., ssh_config=ssh_config, local_config=local_config, host_cwd=host_cwd, ...)
```

---

## 2. `tools/terminal_tool_lifecycle.py`

Import the shaper and use it in `_create_configured_env`:

```python
from tools.terminal_tool_backends import (
    _container_config_from_config,
    _restricted_config_from_config,
    _ssh_config_from_config,
)

# in _create_configured_env(...)
        local_config=(
            _restricted_config_from_config(config) if env_type == "restricted"
            else local_config
        ), task_id=task_id, host_cwd=host_cwd,
```

---

## 3. `tools/code_execution_tool.py`

Same treatment in `_get_or_create_env`:

```python
from tools.terminal_tool_backends import (
    _container_config_from_config, _create_environment, _restricted_config_from_config,
    _ssh_config_from_config,
)

# in _create_environment(...)
            local_config=(
                {"persistent": config.get("local_persistent", False)} if env_type == "local"
                else _restricted_config_from_config(config) if env_type == "restricted"
                else None
            ),
```

---

## 4. `tools/terminal_tool.py` — `_get_env_config()`

After `cwd, host_cwd = _resolve_config_cwd(...)`, pin restricted's cwd to its workdir:

```python
    if env_type == "restricted":
        cwd = _tenv("TERMINAL_RESTRICTED_WORKDIR", "") or cwd
```

And in the returned dict:

```python
        "restricted_workdir": _tenv("TERMINAL_RESTRICTED_WORKDIR", ""),
        "restricted_env_allowlist": _parse_env_var(
            "TERMINAL_RESTRICTED_ENV_ALLOWLIST", "[]", json.loads, "valid JSON"),
```

---

## 5. `agent/terminal_env_registry.py`

Add `restricted` to the reserved built-in names:

```python
BUILTIN_BACKEND_NAMES = frozenset({
    "local", "docker", "singularity", "modal", "managed_modal",
    "daytona", "vercel_sandbox", "ssh", "restricted",
})
```

---

## 6. `tools/terminal_tool_config.py`

Add `restricted` to the built-in set — but **NOT** to `_CONTAINER_BACKENDS`
(it is a local-host backend, not a container/remote one):

```python
_BUILTIN_BACKENDS = _CONTAINER_BACKENDS | {"local", "ssh", "managed_modal", "restricted"}
```

---

## 7. `hermes_cli/doctor_tools.py`

```python
_BUILTIN_TERMINAL_BACKENDS = {"local", "docker", "singularity", "modal", "managed_modal",
                              "daytona", "vercel_sandbox", "ssh", "restricted"}
```

---

## 8. `hermes_cli/setup_terminal.py`

Add a wizard entry:

```python
    ("restricted", "Restricted - local with a locked workdir and env allowlist")]
```

---

## 9–11. The three `config.yaml → TERMINAL_*` env bridges

These three maps **must stay in sync** (Hermes has a test
`tests/tools/test_terminal_config_env_sync.py` enforcing it). Add the same two keys to each:

- `hermes_cli/cli_config_load.py::_TERMINAL_ENV_MAPPINGS` — add to the key tuple:
  `"restricted_workdir", "restricted_env_allowlist"`
- `gateway/run.py::_terminal_env_map`:
  ```python
  "restricted_workdir": "TERMINAL_RESTRICTED_WORKDIR",
  "restricted_env_allowlist": "TERMINAL_RESTRICTED_ENV_ALLOWLIST"
  ```
- `hermes_cli/config.py::TERMINAL_CONFIG_ENV_MAP` — add to the generated-key tuple:
  `"restricted_workdir", "restricted_env_allowlist"`

---

## Verification after applying

```bash
python -m pytest tests/tools/test_restricted_environment.py -v
# 19 passed, 1 skipped
```

