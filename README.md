# vpython

`vpython` is a thread- and process-safe `python` wrapper/launcher script. It exposes a
`python`-compatible CLI, sets up (or reuses) a virtual environment, installs dependencies
when needed, and then delegates execution to the target Python interpreter.

## How it works

1. Determine the project root by walking up from the current working directory and
   looking for common Python project markers (`requirements.txt`, `pyproject.toml`,
   `.git`, etc.).
2. Decide where to store the virtual environment (project-local `.venv` or a cache
   directory).
3. Acquire a cross-platform file lock so concurrent invocations share the same venv.
4. Create the venv if missing and optionally install dependencies.
5. `exec` into the venv's `python` with the original CLI arguments.

## Usage

The wrapper is intended to be invoked like `python`:

```bash
./python3 -c "print('hello from vpython')"
./python3 -m pip --version
./python3 path/to/script.py --arg value
```

## Dependency installation modes

Dependency installation is automatic unless disabled. The mode is determined by
`PYWRAP_DEP_MODE` or by project files:

* `requirements` (default if `requirements.txt` exists) installs from
  the file specified by `PYWRAP_REQUIREMENTS` (default: `<project_root>/requirements.txt`).
  It also considers optional constraint/lock files (`constraints.txt`,
  `constraints.lock`, `requirements.lock`) when computing the dependency hash.
* `pyproject` (default when `pyproject.toml` exists) installs the project in editable
  mode (`pip install -e .`). It includes `poetry.lock`, `pdm.lock`, `uv.lock`, and
  `Pipfile.lock` in the dependency hash when present.
* `none` skips dependency installation entirely.

When dependencies (or the base interpreter) change, the wrapper recomputes a hash and
reinstalls as needed, storing metadata in `.venv/.pywrap/ok.json`.

## Environment variables

* `PYWRAP_BASE_PYTHON`: Path to the interpreter used to create the venv (defaults to
  the current `sys.executable`).
* `PYWRAP_VENV_MODE`: `project` for `<project_root>/.venv` (default) or `cache` for a
  shared cache in `~/.cache/pywrap`.
* `PYWRAP_CACHE_DIR`: Override the cache root when `PYWRAP_VENV_MODE=cache`.
* `PYWRAP_DEP_MODE`: `requirements`, `pyproject`, or `none` (auto-detected if unset).
* `PYWRAP_REQUIREMENTS`: Path to the requirements file (default: `<project_root>/requirements.txt`).
* `PYWRAP_INSTALL_DEPS`: Set to `1` to install dependencies when the marker hash has
  changed (defaults to `0`).
* `PYWRAP_FORCE_RECREATE`: Set to `1` to delete and recreate the venv on every run.
* `PYWRAP_UPGRADE_PIP`: Set to `0` to skip upgrading `pip`, `setuptools`, and `wheel`
  before installs (defaults to `1`).
* `PYWRAP_PIP_ARGS`: Extra arguments passed to `pip` (e.g. `--index-url`, `--extra-index-url`).
* `PYWRAP_LOCK_TIMEOUT_SEC`: Seconds to wait for the venv lock (default: `1800`).
* `PYWRAP_LOCK_POLL_SEC`: Lock polling interval in seconds (default: `0.2`).
* `PYWRAP_VERBOSE`: Set to `1` for stderr diagnostics.

## Notes

* Concurrency is guarded by an OS-level file lock (`.venv.lock` or cache lock) to keep
  multiple processes from corrupting the environment.
* The wrapper will self-heal a partially-created venv by removing it and recreating.
