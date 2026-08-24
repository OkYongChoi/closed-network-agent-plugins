#!/usr/bin/env python3
"""Offline clean-clone verification for Linux and Windows runners."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 120


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


def make_snapshot(seed: Path) -> None:
    """Commit the current tracked+untracked, non-ignored working bytes for pre-commit checks."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=COMMAND_TIMEOUT_SECONDS,
    ).stdout
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        source = ROOT / relative
        destination = seed / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        else:
            shutil.copy2(source, destination)
    binary_probe = seed / "tests/fixtures/line-ending-probe.bin"
    binary_probe.parent.mkdir(parents=True, exist_ok=True)
    binary_probe.write_bytes(b"\x00binary\r\nbytes\xff\n")
    run(["git", "init", "--quiet", str(seed)])
    run(["git", "-C", str(seed), "add", "--all"])
    run([
        "git", "-C", str(seed), "-c", "user.name=Platform Verify",
        "-c", "user.email=platform-verify@example.invalid", "commit", "--quiet",
        "-m", "verification snapshot",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autocrlf", choices=("true", "false", "input"), default="true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="plugins-platform-") as temp_name:
        temp = Path(temp_name)
        seed = temp / "seed"
        seed.mkdir()
        make_snapshot(seed)
        clone = temp / "checkout"
        run([
            "git", "-c", f"core.autocrlf={args.autocrlf}", "clone", "--no-local",
            "--quiet", str(seed), str(clone),
        ])
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        run([sys.executable, "-B", "scripts/validate_repo.py"], cwd=clone, env=env)
        run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=clone,
            env=env,
        )
        attributes = run([
            "git", "check-attr", "eol", "--", "catalog.json",
            "plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py",
        ], cwd=clone)
        if any(not line.rstrip().endswith("eol: lf") for line in attributes.splitlines()):
            raise RuntimeError(f"hashed text is not pinned to LF:\n{attributes}")
        binary_attribute = run(
            ["git", "check-attr", "text", "--", "future-plugin/assets/probe.png"],
            cwd=clone,
        )
        if not binary_attribute.rstrip().endswith("text: unset"):
            raise RuntimeError(f"binary payloads are not protected from text conversion:\n{binary_attribute}")
        if (clone / "tests/fixtures/line-ending-probe.bin").read_bytes() != b"\x00binary\r\nbytes\xff\n":
            raise RuntimeError("binary probe bytes changed during checkout")
        status = run(["git", "status", "--porcelain"], cwd=clone)
        if status.strip():
            raise RuntimeError(f"verification modified the clean checkout:\n{status}")
    print(f"offline clean-clone verification passed (core.autocrlf={args.autocrlf})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
