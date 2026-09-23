# handy-py-tools

A collection of small, independent Python tools that share one uv-managed
project environment.

## Requirement

- [uv](https://docs.astral.sh/uv/) 0.12.17 or newer

uv manages Python, the shared `.venv`, dependencies, and `uv.lock`. Do not use a
separate pip, Poetry, Conda, or per-tool environment.

## Deploy

Run the deployment helper from the repository root:

```sh
./tools/deploy.py
```

It creates `.venv` from scratch when absent, or synchronizes the existing
environment with `uv.lock`. It also writes ignored local VS Code settings.

## Run a tool

From a terminal:

```sh
uv run scripts/svg_to_dxf/run.py
```

In VS Code, open `scripts/svg_to_dxf/run.py` and select **Run Python File**.
Each user tool owns one directory under `scripts/`; its `run.py` is the direct
entry point and its implementation stays inside the same directory.

## Clean

Normal cleanup removes caches, build output, egg-info, Finder metadata, and
byte-identical `name 2.ext` synchronization copies when the original sibling is
present. It scans the shared environment for these safe duplicate copies while
otherwise preserving `.venv` and local VS Code settings:

```sh
./tools/clean.py
```

Preview normal cleanup with `--dry-run`. To return to a bootstrap state, run the
dedicated deep-clean script. It removes all installed project dependencies and
recreates an empty `.venv`, preserving the Python entry point and local VS Code
settings so the maintenance scripts remain directly runnable:

```sh
./tools/deep_clean.py
```

Source files, local VS Code settings, `pyproject.toml`, and `uv.lock` are
preserved at both levels. Run `tools/deploy.py` after deep cleaning to restore
project dependencies.

## Repository check

Enable the versioned hook once per clone:

```sh
git config core.hooksPath .githooks
```

Run the check directly with:

```sh
./tools/check.py --all-files --history --report-worktree
```

The checker enforces English ASCII text, repository hygiene, and compiler-style
diagnostics. The shared ignored `.venv` is not treated as repository content.
On platforms that do not support executable shebangs, use
`uv run --no-project tools/<name>.py` instead.

## Layout

```text
.
|-- scripts/
|   `-- svg_to_dxf/      # One isolated user tool
|       |-- run.py       # Direct VS Code and terminal entry point
|       |-- app.py       # Desktop interface
|       `-- converter.py # Geometry conversion
|-- tools/              # Deploy, clean, deep clean, and repository checks
|-- pyproject.toml      # Shared dependencies and uv configuration
`-- uv.lock             # Shared reproducible dependency lock
```
