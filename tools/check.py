#!/usr/bin/env -S uv run --no-project
"""Enforce the repository's plain-text publication boundary."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNTRACKED_BYTES = 25 * 1024 * 1024
MAX_IGNORED_BYTES = 50 * 1024 * 1024
MAX_UNTRACKED_FILES = 500
MAX_IGNORED_FILES = 2_000

PROHIBITED_EXTENSIONS = {
    ".7z", ".avi", ".bmp", ".doc", ".docx", ".exe", ".gif", ".gz",
    ".ico", ".jpeg", ".jpg", ".mkv", ".mov", ".mp4", ".pdf", ".png",
    ".ppt", ".pptx", ".rar", ".tar", ".tgz", ".tif", ".tiff", ".webp",
    ".xls", ".xlsx", ".zip",
}
PROHIBITED_NAMES = {".DS_Store", "Thumbs.db"}
PROHIBITED_PARTS = {
    ".history", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
}
PROHIBITED_SUFFIXES = {".bak", ".log", ".pyc", ".pyo", ".swp", ".swo", ".tmp"}
SENSITIVE_PATTERNS = {
    "macOS user path": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "Windows user path": re.compile(
        r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"
    ),
    "private key": re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
    "credential assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|password|secret|token)\s*=\s*[^\s$<{][^\s]*"
    ),
}


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        sys.stderr.write(result.stderr.decode("utf-8", "replace"))
        raise SystemExit(result.returncode)
    return result.stdout


def split_z(data: bytes) -> list[str]:
    return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]


def staged_paths() -> list[str]:
    return split_z(git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))


def tracked_paths() -> list[str]:
    return split_z(git("ls-files", "-z"))


def candidate_paths() -> list[str]:
    paths = set(tracked_paths())
    paths.update(split_z(git("ls-files", "--others", "--exclude-standard", "-z")))
    return sorted(paths)


def path_bytes(path: str, source: str) -> bytes:
    if source == "index":
        return git("show", f":{path}")
    return Path(path).read_bytes()


def diagnostic(
    path: str,
    message: str,
    help_text: str,
    line: int | None = None,
    column: int | None = None,
) -> str:
    location = path
    if line is not None and column is not None:
        location = f"{path}:{line}:{column}"
    return f"{location}: error: {message}\n  help: {help_text}"


def text_position(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    column = offset - previous_newline
    return line, column


def byte_position(data: bytes, offset: int) -> tuple[int, int]:
    prefix = data[:offset]
    line = prefix.count(b"\n") + 1
    previous_newline = prefix.rfind(b"\n")
    column = offset - previous_newline
    return line, column


def inspect_path(path_text: str, data: bytes) -> list[str]:
    path = PurePosixPath(path_text)
    suffix = path.suffix.lower()
    errors: list[str] = []

    if path.name in PROHIBITED_NAMES:
        errors.append(diagnostic(
            path_text,
            "operating-system metadata is not versionable",
            "remove the file; its name is already covered by .gitignore",
        ))
    if any(part in PROHIBITED_PARTS for part in path.parts):
        errors.append(diagnostic(
            path_text,
            "cache or local-history path is prohibited",
            "remove it and keep generated state in an ignored location",
        ))
    if suffix in PROHIBITED_EXTENSIONS:
        errors.append(diagnostic(
            path_text,
            "binary, archive, office, or video content is prohibited",
            "remove it or replace it with a documented plain-text source",
        ))
    if suffix in PROHIBITED_SUFFIXES or path.name.endswith("~"):
        errors.append(diagnostic(
            path_text,
            "temporary, backup, log, or bytecode file is prohibited",
            "remove it and place disposable output outside the repository",
        ))
    if len(data) > MAX_FILE_BYTES:
        errors.append(diagnostic(
            path_text,
            f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MiB",
            "split the source or keep the large artifact outside ordinary Git",
        ))

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line, column = byte_position(data, error.start)
        errors.append(diagnostic(
            path_text,
            "content is not valid UTF-8 text",
            "re-save the file as UTF-8 plain text, then replace non-ASCII text",
            line,
            column,
        ))
        return errors

    for match in re.finditer(r"[^\x00-\x7f]+", text):
        line, column = text_position(text, match.start())
        fragment = match.group()
        if len(fragment) > 24:
            fragment = fragment[:21] + "..."
        errors.append(diagnostic(
            path_text,
            f"non-ASCII text {fragment!r}",
            "replace it with English ASCII text or remove it; spell symbols "
            "as words such as 'to', 'deg', or 'pi'",
            line,
            column,
        ))

    for match in re.finditer("\0", text):
        line, column = text_position(text, match.start())
        errors.append(diagnostic(
            path_text,
            "NUL byte indicates non-text content",
            "remove the binary content or replace the file with plain text",
            line,
            column,
        ))
    for label, pattern in SENSITIVE_PATTERNS.items():
        for match in pattern.finditer(text):
            line, column = text_position(text, match.start())
            if label.endswith("user path"):
                help_text = "replace the personal path with a relative path or environment variable"
            else:
                help_text = "remove the value and load credentials from a secure external source"
            errors.append(diagnostic(
                path_text,
                f"possible {label}",
                help_text,
                line,
                column,
            ))
    return errors


def check_paths(paths: list[str], source: str) -> list[str]:
    failures: list[str] = []
    for path in paths:
        try:
            data = path_bytes(path, source)
        except OSError as error:
            failures.append(diagnostic(
                path,
                f"cannot read file: {error}",
                "restore the file or remove the stale path from the candidate set",
            ))
            continue
        failures.extend(inspect_path(path, data))
    return failures


def history_errors() -> list[str]:
    objects = git("rev-list", "--objects", "--all")
    if not objects:
        return []
    records = git(
        "cat-file", "--batch-check=%(objecttype) %(objectsize) %(rest)",
        input_bytes=objects,
    ).decode("utf-8", "surrogateescape")
    failures: list[str] = []
    for record in records.splitlines():
        fields = record.split(" ", 2)
        if len(fields) == 3 and fields[0] == "blob" and int(fields[1]) > MAX_FILE_BYTES:
            failures.append(diagnostic(
                f"history:{fields[2]}",
                f"historical blob exceeds 5 MiB ({fields[1]} bytes)",
                "stop publication and request authorization before rewriting history",
            ))
    return failures


def file_totals(paths: list[str]) -> tuple[int, int]:
    files = [Path(path) for path in paths]
    existing = [path for path in files if path.is_file() and not path.is_symlink()]
    return len(existing), sum(path.stat().st_size for path in existing)


def worktree_report() -> list[str]:
    untracked = split_z(git("ls-files", "--others", "--exclude-standard", "-z"))
    ignored = split_z(git("ls-files", "--others", "--ignored", "--exclude-standard", "-z"))
    ignored = [
        path
        for path in ignored
        if not PurePosixPath(path).parts
        or PurePosixPath(path).parts[0] != ".venv"
    ]
    untracked_count, untracked_size = file_totals(untracked)
    ignored_count, ignored_size = file_totals(ignored)
    print(f"untracked: {untracked_count} files, {untracked_size} bytes")
    print(f"ignored: {ignored_count} files, {ignored_size} bytes")

    failures: list[str] = []
    if untracked_count > MAX_UNTRACKED_FILES or untracked_size > MAX_UNTRACKED_BYTES:
        failures.append(diagnostic(
            ".",
            "untracked workspace growth exceeds its limit",
            "review untracked files and move disposable data outside the repository",
        ))
    if ignored_count > MAX_IGNORED_FILES or ignored_size > MAX_IGNORED_BYTES:
        failures.append(diagnostic(
            ".",
            "ignored workspace growth exceeds its limit",
            "remove stale ignored output or move required tool state outside the repository",
        ))

    root = Path.cwd().resolve()
    for current, directories, _files in os.walk(root):
        current_path = Path(current)
        if current_path == root / ".git":
            directories[:] = []
            continue
        for name in list(directories):
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            if name == ".history":
                failures.append(diagnostic(
                    relative,
                    "local-history directory is prohibited",
                    "remove it after preserving any intentionally recoverable work elsewhere",
                ))
            if name == ".git" and candidate != root / ".git":
                failures.append(diagnostic(
                    relative,
                    "nested Git repository is prohibited",
                    "move it outside this repository or remove its nested .git metadata",
                ))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all-tracked", action="store_true")
    selection.add_argument("--all-files", action="store_true")
    selection.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--report-worktree", action="store_true")
    args = parser.parse_args()

    if args.staged:
        paths, source, label = staged_paths(), "index", "staged"
    elif args.all_tracked:
        paths, source, label = tracked_paths(), "worktree", "tracked"
    else:
        paths, source, label = candidate_paths(), "worktree", "candidate"

    failures = check_paths(paths, source)
    if args.history:
        failures.extend(history_errors())
    if args.report_worktree:
        failures.extend(worktree_report())

    print(f"checked: {len(paths)} {label} paths")
    if failures:
        sys.stdout.flush()
        print("repository check failed:", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("repository check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
