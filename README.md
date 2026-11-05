# hermes-agent-restricted-backend

> **This is a portfolio / interview assignment** — an extension implementation that adds a
> `restricted` execution backend to the open-source project
> [Hermes Agent](https://github.com/nousresearch/hermes-agent).
>
> **⏱ Built in 24 hours**, as **one of two** projects for a single interview assignment
> (the other is a construction-document RAG assistant).
>
> This is **not** a fork and **not** the upstream repo. It contains only the new files,
> the test suite, the configuration example, and a change list for the few files that must
> be touched to register the backend.

---

## What it is

Hermes Agent runs shell commands through pluggable **execution backends** (`local`, `docker`,
`ssh`, `modal`, …). This repo adds a new backend named **`restricted`** — a lightweight
application-level sandbox that sits between "fully trusted `local`" and "container-isolated
`docker`":

1. **Subprocess execution** — every command spawns a fresh `bash -c` process;
2. **Timeout kill** — the whole process tree is force-killed after a configurable timeout;
3. **Locked working directory** — commands may only run inside `restricted_workdir`;
   `..` and symlink traversal are rejected;
4. **Environment allowlist** — the host env is **not** passed through; only allowlisted
   variables are inherited;
5. **Registered as an optional backend** — selectable exactly like `local`/`docker`/`ssh`.

> ⚠️ **Not a strong sandbox.** No seccomp / Linux namespaces / cgroups. This is an
> application-level guard following Hermes's existing backend pattern, per the assignment.

---

## Repo layout

```
hermes-agent-restricted-backend/
├── README.md                    # this file
├── PORTFOLIO_NOTE.md            # portfolio statement + security boundary
├── RESTRICTED_BACKEND.md        # technical walkthrough (Chinese) of the design
├── example_config.yaml          # config.yaml snippet for the restricted backend
├── .gitignore
├── tools/
│   └── environments/
│       └── restricted.py        # the new backend (RestrictedEnvironment)
├── tests/
│   └── tools/
│       └── test_restricted_environment.py   # pytest suite (19 passed, 1 skipped)
└── patches/
    └── CHANGES.md               # the 11 upstream files to modify (with key snippets)
```

`tools/environments/restricted.py` and `tests/tools/test_restricted_environment.py` use the
**real upstream paths** so they drop straight into a checkout of Hermes Agent.

---

## How to apply (on a Hermes Agent checkout)

1. Copy `tools/environments/restricted.py` into the upstream tree at
   `tools/environments/restricted.py`.
2. Copy `tests/tools/test_restricted_environment.py` into `tests/tools/`.
3. Apply the 11 small edits listed in `patches/CHANGES.md` (registration + config bridging).
4. Add the `example_config.yaml` block to your `config.yaml`.

Then run:

```bash
python -m pytest tests/tools/test_restricted_environment.py -v
# 19 passed, 1 skipped   (skip = symlink test, Windows lacks symlink privilege)
```

---

## Configuration example

```yaml
terminal:
  backend: restricted
  restricted_workdir: /absolute/path/to/allowed/dir
  restricted_env_allowlist:
    - PATH
    - HOME
    - LANG
    - TZ
```

See `example_config.yaml`.

---

## Verification

| Check | Result |
|---|---|
| Unit tests (`test_restricted_environment.py`) | **19 passed, 1 skipped** |
| Real-subprocess E2E (bash) | 5/5 PASS (subprocess / dir lock / timeout rc=124 / traversal rc=126 / env allowlist) |
| Through Hermes's real `terminal_tool` handler | `echo` succeeds; out-of-root cwd → rc=126 |

The real-subprocess E2E surfaced and fixed **two real bugs** that mock-only unit tests would
never catch (MSYS→native cwd normalization; the shared cwd resolver defaulting non-local
backends to `/root`). Details in `RESTRICTED_BACKEND.md`.
