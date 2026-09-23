#!/usr/bin/env -S uv run --no-project
"""Synchronize the shared project environment and local VS Code settings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def configure_vscode(dry_run: bool) -> None:
    path = ROOT / ".vscode" / "settings.json"
    settings: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(
                f"{path}: invalid JSON at {error.lineno}:{error.colno}; "
                "fix the local settings before deployment"
            ) from error
        if not isinstance(loaded, dict):
            raise SystemExit(f"{path}: top-level value must be an object")
        settings.update(loaded)

    interpreter = (
        "${workspaceFolder}/.venv/Scripts/python.exe"
        if os.name == "nt"
        else "${workspaceFolder}/.venv/bin/python"
    )
    settings["python.defaultInterpreterPath"] = interpreter

    print(f"configure: {path.relative_to(ROOT)}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(settings, indent=4, sort_keys=True) + "\n",
            encoding="ascii",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required; install it from https://docs.astral.sh/uv/")

    command = [uv, "sync", "--project", str(ROOT)]
    print("+", " ".join(command))
    if not args.dry_run:
        sync_environment = os.environ.copy()
        sync_environment.pop("VIRTUAL_ENV", None)
        subprocess.run(command, check=True, env=sync_environment)

        environment = ROOT / ".venv"
        python = (
            environment / "Scripts" / "python.exe"
            if os.name == "nt"
            else environment / "bin" / "python"
        )
        if not python.exists():
            raise SystemExit(f"deployment failed: expected interpreter was not created at {python}")

    configure_vscode(args.dry_run)
    print("deployment complete")
    print("shared project environment: .venv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
