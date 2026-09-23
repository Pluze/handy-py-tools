#!/usr/bin/env -S uv run --no-project
"""Remove generated repository clutter without deleting project sources."""

from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


TOP_LEVEL_GENERATED = {
    ".coverage",
    ".hypothesis",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".work",
    "build",
    "dist",
    "htmlcov",
}

DUPLICATE_COPY = re.compile(r"^(?P<base>.+) 2(?P<extension>\.[^/]+)?$")


def duplicate_copy_targets(root: Path = ROOT) -> set[Path]:
    """Find byte-identical `name 2.ext` copies that have an original sibling."""

    targets: set[Path] = set()
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        if current_path == root / ".git":
            directories[:] = []
            continue

        for name in files:
            match = DUPLICATE_COPY.fullmatch(name)
            if match is None:
                continue
            duplicate = current_path / name
            original = current_path / (
                match.group("base") + (match.group("extension") or "")
            )
            try:
                if (
                    duplicate.is_symlink()
                    or original.is_symlink()
                    or not duplicate.is_file()
                    or not original.is_file()
                    or duplicate.stat().st_size != original.stat().st_size
                ):
                    continue
                if filecmp.cmp(duplicate, original, shallow=False):
                    targets.add(duplicate)
            except OSError:
                continue
    return targets


def repository_targets() -> set[Path]:
    targets = {
        ROOT / name
        for name in TOP_LEVEL_GENERATED
        if (ROOT / name).exists() or (ROOT / name).is_symlink()
    }
    targets.update(ROOT.glob(".coverage.*"))
    targets.update(ROOT.glob("**/*.egg-info"))
    shared_environment = ROOT / ".venv"

    for current, directories, files in os.walk(ROOT):
        current_path = Path(current)
        if current_path in {ROOT / ".git", shared_environment}:
            directories[:] = []
            continue

        retained: list[str] = []
        for name in directories:
            path = current_path / name
            if name == "__pycache__" or name.endswith(".egg-info"):
                targets.add(path)
            else:
                retained.append(name)
        directories[:] = retained

        for name in files:
            path = current_path / name
            if name == ".DS_Store" or path.suffix in {".pyc", ".pyo"}:
                targets.add(path)
    targets.update(duplicate_copy_targets())
    return targets


def remove(path: Path, dry_run: bool) -> None:
    try:
        label = path.relative_to(ROOT)
    except ValueError:
        label = path
    print(f"remove: {label}")
    if dry_run:
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clean_repository(*, deep: bool, dry_run: bool) -> int:
    targets = repository_targets()
    if deep:
        path = ROOT / ".venv"
        if path.exists() or path.is_symlink():
            targets = {
                target
                for target in targets
                if target == path or path not in target.parents
            }
            targets.add(path)

    for path in sorted(targets, key=str):
        remove(path, dry_run)
    verb = "would remove" if dry_run else "removed"
    level = "deep" if deep else "normal"
    print(f"{level} cleanup complete: {verb} {len(targets)} paths")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return clean_repository(deep=False, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
