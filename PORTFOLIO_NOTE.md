# PORTFOLIO NOTE

> This is an extension task for a portfolio / interview assignment: add a `restricted`
> execution backend for [Hermes Agent](https://github.com/nousresearch/hermes-agent).

## Task (paraphrased)

Add a new `restricted` execution backend that:

- runs commands via subprocess;
- force-kills the process tree on a configurable timeout;
- locks the working directory and blocks path traversal (including symlink escapes);
- exposes an environment-variable allowlist instead of the full host env;
- is registered as an optional backend, selectable like `local` / `docker` / `ssh`.

## What I did

- **Followed Hermes's existing backend pattern.** The new
  `RestrictedEnvironment` subclasses `BaseEnvironment` (`tools/environments/base.py`) and only
  implements the two hooks the base class requires — `_run_bash()` and `cleanup()` — while
  reusing the base `execute()` template (command wrapping, timeout poll, output collection,
  process wait).
- **Reused, did not duplicate.** Bash resolution, process-group kill (TERM→KILL→sweep
  `setsid` escapees), and the secret scrubber are imported from `tools/environments/local.py`
  (Hermes's "extend, don't duplicate" principle).
- **Registered it the Hermes way.** Wired `restricted` into `_ENV_BUILDERS`, the backend-name
  allowlists, and the three `config.yaml → TERMINAL_*` env bridges, plus
  `_get_env_config()` — so it is selected exactly like any other built-in backend.
- **Tested it for real.** 19 unit tests (`tests/tools/test_restricted_environment.py`:
  19 passed, 1 skipped), plus a real-subprocess E2E that spawns actual `bash -c` processes.
  The E2E caught and fixed two real bugs that mock-only tests could never surface.

## Design rationale (why this shape)

Hermes's backend layer is a **template-method + narrow-waist** design: the base class owns the
whole execution pipeline, and each backend only answers "how do I spawn" and "how do I clean
up". `restricted` therefore adds zero new core surface — it extends the existing abstraction
(Hermes's Footprint Ladder: extend existing code before adding anything new).

## Security boundary (important)

- ⚠️ **This is NOT a production-grade secure sandbox.**
- No seccomp, no Linux namespaces, no cgroups.
- The subprocess still shares the kernel and UID with the host; the directory lock + env
  allowlist only **mitigate**, they do not **eliminate**, absolute-path reads/writes outside
  the allowed directory.
- There is a TOCTOU window between validation and `Popen`; a process-level guard cannot
  fully close it (that requires seccomp/landlock, which the assignment explicitly excludes).

## Environment limitation (honest note)

The development machine is Windows without bash (WSL install timed out). I provisioned a
portable MinGit bash to run the real-subprocess E2E; the subprocess/timeout-kill logic itself
is inherited from Hermes's existing `LocalEnvironment` base and is not a code defect.
