#!/usr/bin/env python3
"""Produce a deterministic, bounded, offline repository summary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path

IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build"}
MANIFESTS = {
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
}
CREDENTIAL_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".sh": "Shell",
}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 10


class SummaryError(RuntimeError):
    pass


def _git(
    root: Path, *args: str, max_output: int = MAX_GIT_OUTPUT_BYTES
) -> tuple[str | None, bool]:
    """Run a local Git command while bounding output in memory.

    Returns ``(output, truncated)``. A timeout or unavailable Git returns
    ``(None, False)``.
    """
    try:
        process = subprocess.Popen(
            ["git", *args],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, False
    assert process.stdout is not None
    captured: dict[str, bytes] = {}

    def read_bounded() -> None:
        captured["data"] = process.stdout.read(max_output + 1)

    reader = threading.Thread(target=read_bounded, daemon=True)
    reader.start()
    reader.join(GIT_TIMEOUT_SECONDS)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        process.wait()
        process.stdout.close()
        return None, False
    data = captured.get("data", b"")
    truncated = len(data) > max_output
    if truncated and process.poll() is None:
        process.kill()
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        process.stdout.close()
        return None, truncated
    process.stdout.close()
    if return_code != 0 and not truncated:
        return None, False
    output = data[:max_output].decode("utf-8", errors="replace").strip()
    return output, truncated


def _package_commands(path: Path) -> list[str]:
    try:
        if path.is_symlink() or not path.is_file():
            return []
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return []
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [f"npm run {name}" for name in sorted(scripts) if isinstance(name, str)]


def detect_commands(root: Path, manifests: list[str]) -> list[str]:
    commands: list[str] = []
    if "package.json" in manifests:
        commands.extend(_package_commands(root / "package.json"))
    if "pyproject.toml" in manifests:
        commands.extend(["python -m pytest", "python -m unittest discover"])
    if "Cargo.toml" in manifests:
        commands.extend(["cargo build", "cargo test"])
    if "go.mod" in manifests:
        commands.extend(["go build ./...", "go test ./..."])
    if "Makefile" in manifests:
        commands.append("make (inspect targets first)")
    if "pom.xml" in manifests:
        commands.extend(["mvn package", "mvn test"])
    if "build.gradle" in manifests or "build.gradle.kts" in manifests:
        commands.extend(["./gradlew build", "./gradlew test"])
    return list(dict.fromkeys(commands))


def summarize(root: Path, max_files: int, max_bytes: int) -> dict[str, object]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise SummaryError(f"repository path is not a directory: {root}")
    languages: Counter[str] = Counter()
    top_level: set[str] = set()
    manifests: set[str] = set()
    risks: list[str] = []
    files = 0
    total_bytes = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            path = current / name
            rel = path.relative_to(root)
            if len(rel.parts) == 1:
                top_level.add(name + "/")
            if path.is_symlink():
                risks.append(f"symlink directory: {rel.as_posix()}")
            elif name not in IGNORED_DIRS:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(root)
            if len(rel.parts) == 1:
                top_level.add(name)
                if name in MANIFESTS:
                    manifests.add(name)
            files += 1
            if files > max_files:
                truncated = True
                break
            try:
                info = path.lstat()
            except OSError:
                risks.append(f"unreadable path: {rel.as_posix()}")
                continue
            if path.is_symlink():
                risks.append(f"symlink: {rel.as_posix()}")
                continue
            total_bytes += info.st_size
            if total_bytes > max_bytes:
                truncated = True
                break
            if info.st_size > 5 * 1024 * 1024:
                risks.append(f"large file: {rel.as_posix()} ({info.st_size} bytes)")
            if name.lower() in CREDENTIAL_NAMES:
                risks.append(f"credential-shaped filename: {rel.as_posix()}")
            language = EXTENSIONS.get(path.suffix.lower())
            if language:
                languages[language] += 1
        if truncated:
            break
    branch, branch_truncated = _git(root, "branch", "--show-current")
    status, status_truncated = _git(root, "status", "--porcelain")
    if branch_truncated:
        risks.append("Git branch output exceeded the configured limit")
    if status:
        qualifier = "at least " if status_truncated else ""
        suffix = ", output truncated" if status_truncated else ""
        risks.append(
            f"dirty Git worktree ({qualifier}{len(status.splitlines())} changed paths{suffix})"
        )
    elif status_truncated:
        risks.append("Git status output exceeded the configured limit")
    return {
        "root": str(root),
        "git": {"branch": branch or None, "dirty": bool(status) if status is not None else None},
        "top_level": sorted(top_level, key=str.casefold),
        "manifests": sorted(manifests),
        "languages": dict(languages.most_common()),
        "commands": detect_commands(root, sorted(manifests)),
        "risks": sorted(set(risks)),
        "scan": {"files_seen": min(files, max_files), "bytes_seen": total_bytes, "truncated": truncated},
    }


def render_markdown(data: dict[str, object]) -> str:
    git = data["git"]
    scan = data["scan"]
    lines = [
        f"# Repository summary: {Path(str(data['root'])).name}",
        "",
        f"- Root: `{data['root']}`",
        f"- Git branch: `{git['branch'] or 'unavailable'}`",
        f"- Files scanned: {scan['files_seen']}" + (" (limit reached)" if scan["truncated"] else ""),
        "",
        "## Top-level structure",
        "",
    ]
    if data["top_level"]:
        lines.extend(f"- `{item}`" for item in data["top_level"])
    else:
        lines.append("- None detected")
    lines.extend(["", "## Languages", ""])
    if data["languages"]:
        lines.extend(f"- {name}: {count} files" for name, count in data["languages"].items())
    else:
        lines.append("- None detected")
    lines.extend(["", "## Candidate commands", ""])
    if data["commands"]:
        lines.extend(f"- `{command}`" for command in data["commands"])
    else:
        lines.append("- None detected")
    lines.extend(["", "## Review signals", ""])
    if data["risks"]:
        lines.extend(f"- {risk}" for risk in data["risks"])
    else:
        lines.append("- No signals detected by this bounded metadata scan")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-summary")
    parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--max-files", type=int, default=20_000)
    parser.add_argument("--max-bytes", type=int, default=200 * 1024 * 1024)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_files < 1 or args.max_bytes < 1:
        print("error: limits must be positive", file=sys.stderr)
        return 2
    try:
        data = summarize(args.path, args.max_files, args.max_bytes)
    except SummaryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(data), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
