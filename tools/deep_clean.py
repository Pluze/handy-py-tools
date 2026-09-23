#!/usr/bin/env -S uv run --no-project
"""Clear project dependencies and recreate a runnable bootstrap environment."""

import os
import shutil
import subprocess

from clean import ROOT, clean_repository


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required; install it from https://docs.astral.sh/uv/")

    result = clean_repository(deep=True, dry_run=False)
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    subprocess.run(
        [uv, "venv", "--clear", str(ROOT / ".venv")],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    print("bootstrap environment recreated: .venv")
    print("run tools/deploy.py to restore project dependencies")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
