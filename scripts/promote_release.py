#!/usr/bin/env python3
"""Publish the manifest-only latest-approved plugin branch using Git and stdlib."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class PromoteError(RuntimeError):
    pass


def redact(text: str) -> str:
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def git(
    args: list[str], cwd: Path, *, check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    env.update(extra_env or {})
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120, env=env,
    )
    if check and result.returncode:
        detail = result.stderr.strip().splitlines()
        raise PromoteError(redact(detail[-1]) if detail else "Git operation failed")
    return result.stdout.strip()


def git_bytes(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise PromoteError(redact(detail[-1]) if detail else "Git operation failed")
    return result.stdout


def manifest(
    catalog_bytes: bytes, source: str, ref: str, version: str, updated_at: str,
    sequence: int,
) -> bytes:
    if not FULL_SHA.fullmatch(ref):
        raise PromoteError("ref must be a full 40-character commit SHA")
    parsed_source = urlsplit(source)
    if (
        not source
        or source != source.strip()
        or parsed_source.username is not None
        or parsed_source.password is not None
        or bool(parsed_source.query)
        or bool(parsed_source.fragment)
    ):
        raise PromoteError("source must be a non-credentialed Git URL or mirror path")
    if not version or version != version.strip():
        raise PromoteError("version must be non-empty without surrounding whitespace")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise PromoteError("sequence must be a non-negative integer")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_at):
        raise PromoteError("updated-at must be UTC RFC 3339 (YYYY-MM-DDTHH:MM:SSZ)")
    try:
        catalog = json.loads(catalog_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromoteError(f"invalid catalog.json: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("format_version") != 1:
        raise PromoteError("unsupported catalog.json")
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise PromoteError("catalog plugins must be a non-empty list")
    packages: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in plugins:
        if not isinstance(item, dict) or set(item) != {"name", "path", "content_sha256"}:
            raise PromoteError("catalog plugin must contain name, path, and content_sha256")
        name, path, digest = item["name"], item["path"], item["content_sha256"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
            raise PromoteError(f"invalid or duplicate catalog plugin: {name!r}")
        if path != f"plugins/{name}":
            raise PromoteError(f"invalid catalog path for {name}")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise PromoteError(f"invalid catalog digest for {name}")
        seen.add(name)
        packages.append({"name": name, "digest": digest})
    data = {
        "formatVersion": 1,
        "kind": "agent-plugins-release",
        "name": "plugins",
        "source": source,
        "ref": ref.lower(),
        "version": version,
        "updatedAt": updated_at,
        "sequence": sequence,
        "catalogSha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "packages": sorted(packages, key=lambda item: item["name"]),
    }
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def publish(args: argparse.Namespace) -> None:
    push_url = os.environ.get(args.push_url_env)
    if not push_url:
        raise PromoteError(f"push URL environment variable is unset: {args.push_url_env}")
    with tempfile.TemporaryDirectory(prefix="approved-plugins-") as temp:
        root = Path(temp)
        git(["init", "--quiet"], root)
        remote_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.url",
            "GIT_CONFIG_VALUE_0": push_url,
        }
        git(["fetch", "--quiet", "--depth=1", "origin", args.ref], root, extra_env=remote_env)
        resolved = git(["rev-parse", "FETCH_HEAD^{commit}"], root)
        if resolved.lower() != args.ref.lower():
            raise PromoteError("fetched release commit does not match requested ref")
        catalog_bytes = git_bytes(["show", f"{resolved}:catalog.json"], root)
        pointer_fetch = subprocess.run(
            ["git", "fetch", "--quiet", "--depth=1", "origin", "refs/heads/latest-approved"],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=120, env={**os.environ, **remote_env},
        )
        if pointer_fetch.returncode == 0:
            pointer = git(["rev-parse", "FETCH_HEAD^{commit}"], root)
            if not args.rollback:
                try:
                    previous = json.loads(git_bytes(["show", f"{pointer}:release-manifest.json"], root))
                    previous_sequence = previous.get("sequence", -1)
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    previous_sequence = -1
                if isinstance(previous_sequence, int) and previous_sequence >= args.sequence:
                    print(
                        f"skipped stale promotion sequence {args.sequence}; "
                        f"latest is {previous_sequence}"
                    )
                    return
            git(["checkout", "--quiet", "--detach", pointer], root)
        else:
            git(["checkout", "--quiet", "--orphan", "latest-approved"], root)
        (root / "release-manifest.json").write_bytes(
            manifest(
                catalog_bytes, args.source, resolved, args.version,
                args.updated_at, args.sequence,
            )
        )
        git(["add", "release-manifest.json"], root)
        git([
            "-c", f"user.name={args.git_name}", "-c", f"user.email={args.git_email}",
            "commit", "--quiet", "-m", f"Approve plugins {args.version}",
        ], root)
        git(
            ["push", "--quiet", "origin", "HEAD:refs/heads/latest-approved"],
            root, extra_env=remote_env,
        )
        print(f"approved {resolved} as {args.version}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="credential-free URL stored in manifest")
    parser.add_argument("--push-url-env", default="CI_REPOSITORY_URL")
    parser.add_argument("--ref", required=True, help="approved immutable commit")
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument(
        "--updated-at",
        default=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    parser.add_argument("--git-name", default="Agent Tools Release Bot")
    parser.add_argument("--git-email", default="agent-tools-release@example.invalid")
    args = parser.parse_args()
    try:
        publish(args)
        return 0
    except (PromoteError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
