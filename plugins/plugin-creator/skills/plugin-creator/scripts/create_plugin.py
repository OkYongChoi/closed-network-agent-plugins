#!/usr/bin/env python3
"""Create and validate portable Agent Plugins 1.0 packages.

This file intentionally uses only the Python standard library and Git CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
MAX_FILES = 2_000
MAX_BYTES = 50 * 1024 * 1024
HTTP_FIELD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key",
}
SENSITIVE_ENV_RE = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)$", re.I)


class PluginError(RuntimeError):
    """Expected validation or creation failure."""


def normalize_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9.]+", "-", value.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise PluginError(f"invalid plugin name after normalization: {name!r}")
    return name


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def inspect_tree(root: Path, *, max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> None:
    """Reject unsafe or unexpectedly large source trees before copying."""
    if not root.is_dir() or root.is_symlink():
        raise PluginError(f"expected a real directory: {root}")
    resolved_root = root.resolve(strict=True)
    seen: dict[str, str] = {}
    file_count = 0
    total_bytes = 0
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        key = relative.as_posix().casefold()
        if key in seen and seen[key] != relative.as_posix():
            raise PluginError(f"case-colliding paths: {seen[key]!r} and {relative.as_posix()!r}")
        seen[key] = relative.as_posix()
        if item.is_symlink():
            raise PluginError(f"symlinks are not allowed: {relative}")
        try:
            resolved = item.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise PluginError(f"path escapes source root: {relative}") from exc
        if item.is_dir():
            continue
        if not item.is_file():
            raise PluginError(f"unsupported filesystem entry: {relative}")
        stat = item.stat(follow_symlinks=False)
        if stat.st_nlink > 1:
            raise PluginError(f"hard-linked files are not allowed: {relative}")
        file_count += 1
        total_bytes += stat.st_size
        if file_count > max_files or total_bytes > max_bytes:
            raise PluginError("source tree exceeds safety limits")


def tree_digest(root: Path) -> str:
    inspect_tree(root)
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _frontmatter(skill_md: Path) -> dict[str, str]:
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginError(f"cannot read {skill_md}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        raise PluginError(f"missing YAML frontmatter in {skill_md}")
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line != line.lstrip():
            raise PluginError(f"structured YAML is not supported in {skill_md}: {line!r}")
        if ":" not in line:
            raise PluginError(f"unsupported frontmatter line in {skill_md}: {line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key not in {"name", "description", "license", "compatibility", "allowed-tools"}:
            raise PluginError(f"unsupported frontmatter field {key!r} in {skill_md}")
        if not value or value[0] in "[{|>&*!" or " #" in value or ": " in value:
            raise PluginError(f"ambiguous YAML scalar for {key!r} in {skill_md}")
        if value[:1] in {"\"", "'"}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1] or "\\" in value[1:-1]:
                raise PluginError(f"unsupported quoted YAML scalar in {skill_md}")
            value = value[1:-1]
        elif value[-1:] in {"\"", "'"}:
            raise PluginError(f"unbalanced quoted YAML scalar in {skill_md}")
        if key in result:
            raise PluginError(f"duplicate frontmatter field {key!r} in {skill_md}")
        result[key] = value
    else:
        raise PluginError(f"unterminated YAML frontmatter in {skill_md}")
    return result


def validate_skill(skill_root: Path) -> str:
    inspect_tree(skill_root)
    metadata = _frontmatter(skill_root / "SKILL.md")
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        raise PluginError(f"invalid skill name {name!r} in {skill_root}")
    if skill_root.name != name:
        raise PluginError(f"skill folder {skill_root.name!r} does not match name {name!r}")
    if not description or len(description) > 1024:
        raise PluginError(f"skill description must contain 1-1024 characters: {skill_root}")
    return name


def validate_plugin(plugin_root: Path) -> dict[str, Any]:
    inspect_tree(plugin_root)
    manifest_path = plugin_root / "plugin.json"
    if not manifest_path.is_file():
        raise PluginError(f"missing plugin.json: {plugin_root}")
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise PluginError("plugin.json must contain an object")
    allowed = {
        "$schema", "name", "version", "description", "author", "homepage",
        "repository", "license", "keywords", "extensions",
    }
    unknown = set(manifest) - allowed
    if unknown:
        raise PluginError(f"unsupported plugin.json fields: {sorted(unknown)}")
    if manifest.get("$schema") != PLUGIN_SCHEMA:
        raise PluginError(f"plugin.json must target {PLUGIN_SCHEMA}")
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or len(name) > 64:
        raise PluginError(f"invalid plugin name: {name!r}")
    if plugin_root.name != name:
        raise PluginError(f"plugin folder {plugin_root.name!r} does not match name {name!r}")
    for field in ("version", "description", "homepage", "repository", "license"):
        if field in manifest and not isinstance(manifest[field], str):
            raise PluginError(f"plugin.json {field!r} must be a string")
    if "author" in manifest:
        author = manifest["author"]
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
            raise PluginError("plugin.json author must contain only name, email, and url")
        if any(not isinstance(value, str) for value in author.values()):
            raise PluginError("plugin.json author values must be strings")
    if "keywords" in manifest and (
        not isinstance(manifest["keywords"], list)
        or any(not isinstance(value, str) for value in manifest["keywords"])
    ):
        raise PluginError("plugin.json keywords must be an array of strings")
    if "extensions" in manifest and (
        not isinstance(manifest["extensions"], dict)
        or any(not isinstance(value, dict) for value in manifest["extensions"].values())
    ):
        raise PluginError("plugin.json extensions must map namespaces to objects")
    skills = plugin_root / "skills"
    if skills.exists():
        if not skills.is_dir() or skills.is_symlink():
            raise PluginError("skills must be a real directory")
        for child in sorted(skills.iterdir()):
            if child.is_dir():
                validate_skill(child)
            else:
                raise PluginError(f"skills may contain only skill directories: {child.name}")
    _validate_mcp(plugin_root / "mcp.json")
    return manifest


def _safe_relative(value: str, prefix: str) -> bool:
    if not value.startswith(prefix) or "\\" in value or "\x00" in value:
        return False
    tail = value[len(prefix):]
    if not tail or tail.startswith("/"):
        return False
    parts = tail.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    normalized = posixpath.normpath(tail)
    return not normalized.startswith("../") and not posixpath.isabs(normalized)


def _safe_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.fragment or not parsed.hostname:
        return False
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    return parsed.scheme == "https" or (parsed.scheme == "http" and loopback)


def _validate_mcp(path: Path) -> None:
    if not path.exists():
        return
    data = _load_json(path)
    schema = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    if not isinstance(data, dict) or set(data) != {"$schema", "mcpServers"}:
        raise PluginError("mcp.json must contain only $schema and mcpServers")
    if data["$schema"] != schema or not isinstance(data["mcpServers"], dict):
        raise PluginError("invalid mcp.json schema or mcpServers object")
    for name, server in data["mcpServers"].items():
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            raise PluginError("invalid MCP server entry")
        kind = server.get("type")
        if kind == "stdio":
            allowed, required = {"type", "command", "args", "env", "cwd"}, {"type", "command"}
            command = server.get("command")
            if not isinstance(command, str) or not command or any(c.isspace() for c in command) or "\x00" in command:
                raise PluginError(f"MCP stdio command for {name!r} must be one token")
            if command.startswith("./"):
                if not _safe_relative(command, "./"):
                    raise PluginError(f"MCP stdio command for {name!r} escapes the plugin root")
            elif "/" in command or "\\" in command or command.startswith((".", "~")) or ":" in command:
                raise PluginError(f"MCP stdio command for {name!r} must be bare or a safe ./ path")
            if "args" in server and (not isinstance(server["args"], list) or any(not isinstance(v, str) for v in server["args"])):
                raise PluginError(f"MCP args for {name!r} must be strings")
            if "env" in server:
                env = server["env"]
                if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
                    raise PluginError(f"MCP env for {name!r} must map strings to strings")
                if {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(env):
                    raise PluginError(f"MCP env for {name!r} overrides reserved variables")
                if any(
                    SENSITIVE_ENV_RE.search(key)
                    and value
                    and not value.startswith(("${PLUGIN_ROOT}", "${PLUGIN_DATA}"))
                    for key, value in env.items()
                ):
                    raise PluginError(f"MCP env for {name!r} appears to embed a secret")
            if "cwd" in server:
                cwd = server["cwd"]
                valid_cwd = isinstance(cwd, str) and (
                    cwd in {"${PLUGIN_ROOT}", "${PLUGIN_DATA}"}
                    or (cwd.startswith("./") and _safe_relative(cwd, "./"))
                    or (cwd.startswith("${PLUGIN_ROOT}/") and _safe_relative(cwd, "${PLUGIN_ROOT}/"))
                    or (cwd.startswith("${PLUGIN_DATA}/") and _safe_relative(cwd, "${PLUGIN_DATA}/"))
                )
                if not valid_cwd:
                    raise PluginError(f"invalid or escaping MCP cwd for {name!r}")
        elif kind in {"streamable-http", "sse"}:
            allowed, required = {"type", "url", "headers"}, {"type", "url"}
            url = server.get("url")
            if not isinstance(url, str) or not _safe_http_url(url):
                raise PluginError(f"MCP URL for {name!r} must be HTTPS or loopback HTTP without credentials or fragments")
            if "headers" in server:
                headers = server["headers"]
                if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                    raise PluginError(f"MCP headers for {name!r} must map strings to strings")
                folded = [key.casefold() for key in headers]
                if len(folded) != len(set(folded)):
                    raise PluginError(f"MCP headers for {name!r} collide case-insensitively")
                for key, value in headers.items():
                    if not HTTP_FIELD_RE.fullmatch(key) or "\r" in value or "\n" in value:
                        raise PluginError(f"MCP headers for {name!r} contain an invalid HTTP field")
                    if key.casefold() in SENSITIVE_HEADER_NAMES or re.match(r"^(?:Bearer|Basic)\s", value, re.I):
                        raise PluginError(f"MCP headers for {name!r} appear to embed credentials")
        else:
            raise PluginError(f"unsupported MCP transport for {name!r}: {kind!r}")
        if not required <= set(server) or set(server) - allowed:
            raise PluginError(f"invalid fields for MCP server {name!r}")


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise PluginError(f"git command failed: {detail.strip()}") from exc
    return completed.stdout.strip()


def _obtain_skill(source: str, ref: str | None, skill_path: str | None, temp: Path) -> tuple[Path, str | None]:
    local = Path(source).expanduser()
    if local.exists():
        root = local.resolve(strict=True)
        if ref:
            if not FULL_SHA_RE.fullmatch(ref):
                raise PluginError("--ref must be a full 40-character Git commit SHA")
            materialized = temp / "source"
            _run_git(["init", "--quiet", str(materialized)])
            _run_git(["-C", str(materialized), "remote", "add", "origin", str(root)])
            _run_git(["-C", str(materialized), "fetch", "--quiet", "--depth=1", "origin", ref])
            _run_git(["-C", str(materialized), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
            actual_ref = _run_git(["-C", str(materialized), "rev-parse", "HEAD"])
            if actual_ref.lower() != ref.lower():
                raise PluginError(f"local source resolved to {actual_ref}, expected {ref}")
            root = materialized.resolve(strict=True)
        else:
            # An unpinned working tree is a snapshot, not authoritative proof of HEAD.
            actual_ref = None
    else:
        if not ref or not FULL_SHA_RE.fullmatch(ref):
            raise PluginError("remote skill imports require a full 40-character --ref")
        root = temp / "source"
        _run_git(["init", "--quiet", str(root)])
        _run_git(["-C", str(root), "remote", "add", "origin", source])
        _run_git(["-C", str(root), "fetch", "--quiet", "--depth=1", "origin", ref])
        _run_git(["-C", str(root), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        actual_ref = _run_git(["-C", str(root), "rev-parse", "HEAD"])
        if actual_ref.lower() != ref.lower():
            raise PluginError(f"remote resolved to {actual_ref}, expected {ref}")
        root = root.resolve(strict=True)
    relative = Path(skill_path) if skill_path else Path(".")
    if relative.is_absolute() or ".." in relative.parts:
        raise PluginError("--path must be a repository-relative path without '..'")
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PluginError("imported skill escapes source root") from exc
    validate_skill(candidate)
    return candidate, actual_ref


def _copy_tree(source: Path, target: Path) -> None:
    inspect_tree(source)
    shutil.copytree(source, target, symlinks=False)


def _native_mcp(canonical: dict[str, Any]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, server in canonical["mcpServers"].items():
        native = dict(server)
        if native.get("type") == "streamable-http":
            native["type"] = "http"
        servers[name] = native
    return {"mcpServers": servers}


def _native_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    hostname = host[:-1] if host.endswith(".") else host
    if not hostname or len(hostname) > 253:
        return False
    label_re = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
    return all(label_re.fullmatch(label) is not None for label in hostname.split("."))


def _native_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in manifest.items() if k not in {"$schema", "extensions"}}
    name = manifest["name"]
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        result["version"] = "0.1.0"
    description = manifest.get("description")
    if not isinstance(description, str) or not description.strip():
        result["description"] = f"Portable projection for {name}."
    author = manifest.get("author")
    native_author = dict(author) if isinstance(author, dict) else {}
    if not isinstance(native_author.get("name"), str) or not native_author["name"].strip():
        native_author["name"] = "Unknown"
    if "email" in native_author and (
        not isinstance(native_author["email"], str) or not native_author["email"].strip()
    ):
        native_author.pop("email")
    if "url" in native_author and not _native_https_url(native_author["url"]):
        native_author.pop("url")
    result["author"] = native_author
    return result


def _codex_manifest(manifest: dict[str, Any], has_mcp: bool) -> dict[str, Any]:
    name = manifest["name"]
    result = _native_manifest(manifest)
    description = result["description"]
    result["skills"] = "./skills/"
    if has_mcp:
        result["mcpServers"] = "./.mcp.json"
    result["interface"] = {
        "displayName": name.replace("-", " ").title(),
        "shortDescription": description[:120],
        "longDescription": description,
        "developerName": result["author"]["name"],
        "category": "Developer Tools",
        "capabilities": ["Read", "Write"],
        "defaultPrompt": [f"Use {name} to help with this task."],
    }
    return result


def _claude_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return _native_manifest(manifest)


def build_projection(canonical: Path, adapter: str, staging_root: Path) -> None:
    manifest = validate_plugin(canonical)
    name = manifest["name"]
    adapter_root = staging_root / adapter
    plugin_root = adapter_root / "plugins" / name
    if plugin_root.exists():
        raise PluginError(f"projection already exists: {plugin_root}")
    _copy_tree(canonical, plugin_root)
    canonical_mcp = _load_json(canonical / "mcp.json") if (canonical / "mcp.json").is_file() else None
    if canonical_mcp is not None:
        _write_json(plugin_root / ".mcp.json", _native_mcp(canonical_mcp))
    if adapter == "codex":
        (plugin_root / ".codex-plugin").mkdir()
        _write_json(plugin_root / ".codex-plugin" / "plugin.json", _codex_manifest(manifest, canonical_mcp is not None))
        _write_json(adapter_root / "marketplace.json", {
            "name": "local-portable",
            "interface": {"displayName": "Local Portable Plugins"},
            "plugins": [{
                "name": name,
                "source": {"source": "local", "path": f"./plugins/{name}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Developer Tools",
            }],
        })
    elif adapter == "claude":
        (plugin_root / ".claude-plugin").mkdir()
        _write_json(plugin_root / ".claude-plugin" / "plugin.json", _claude_manifest(manifest))
        _write_json(adapter_root / ".claude-plugin" / "marketplace.json", {
            "name": "local-portable",
            "owner": {"name": manifest.get("author", {}).get("name", "Unknown")},
            "metadata": {"description": "Local projections generated from portable Agent Plugins."},
            "plugins": [{
                "name": name,
                "source": f"./plugins/{name}",
                "description": manifest.get("description", ""),
                "version": manifest.get("version", "0.1.0"),
            }],
        })
    else:
        raise PluginError(f"unsupported adapter: {adapter}")


def create_plugin(args: argparse.Namespace) -> Path:
    name = normalize_name(args.name)
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    if target.exists():
        raise PluginError(f"destination already exists: {target}")
    adapters = [item.strip() for item in args.adapters.split(",") if item.strip()]
    if len(adapters) != len(set(adapters)) or set(adapters) - {"codex", "claude"}:
        raise PluginError("--adapters accepts a comma-separated subset of codex,claude")

    with tempfile.TemporaryDirectory(prefix="plugin-create-") as tmp_name:
        tmp = Path(tmp_name)
        canonical = tmp / name
        (canonical / "skills").mkdir(parents=True)
        manifest = {
            "$schema": PLUGIN_SCHEMA,
            "name": name,
            "version": args.version,
            "description": args.description or f"Portable tools for {name}.",
            "author": {"name": args.author},
            "license": args.license,
        }
        _write_json(canonical / "plugin.json", manifest)
        if args.import_skill:
            imported, actual_ref = _obtain_skill(args.import_skill, args.ref, args.path, tmp)
            destination = canonical / "skills" / imported.name
            _copy_tree(imported, destination)
            _write_json(canonical / "VENDORED_SKILLS.json", {
                "format_version": 1,
                "skills": [{
                    "name": imported.name,
                    "source_repository": args.import_skill,
                    "source_commit": actual_ref,
                    "source_path": args.path or ".",
                    "tree_sha256": tree_digest(imported),
                    "license": args.import_license,
                    "vendoring": "build-time snapshot; no runtime fetch",
                }],
            })
        validate_plugin(canonical)
        projection_tmp = tmp / "projections"
        for adapter in adapters:
            build_projection(canonical, adapter, projection_tmp)
        for adapter in adapters:
            destination = output / ".staging" / adapter
            if destination.exists():
                raise PluginError(f"projection destination already exists: {destination}")
        canonical.rename(target)
        for adapter in adapters:
            destination = output / ".staging" / adapter
            destination.parent.mkdir(parents=True, exist_ok=True)
            (projection_tmp / adapter).rename(destination)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-plugin", help="create a canonical plugin package")
    create.add_argument("name")
    create.add_argument("--output", required=True, help="parent directory for the canonical plugin")
    create.add_argument("--adapters", default="", help="optional comma-separated codex,claude projections")
    create.add_argument("--version", default="0.1.0")
    create.add_argument("--description")
    create.add_argument("--author", default="Internal Platform Team")
    create.add_argument("--license", default="Apache-2.0")
    create.add_argument("--import-skill", metavar="SOURCE")
    create.add_argument("--ref", help="full Git commit SHA for an imported source")
    create.add_argument("--path", help="skill path within the imported source")
    create.add_argument("--import-license", default="Apache-2.0")
    validate = sub.add_parser("validate", help="validate a canonical plugin package")
    validate.add_argument("plugin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-plugin":
            if bool(args.import_skill) != bool(args.path):
                raise PluginError("--import-skill and --path must be supplied together")
            created = create_plugin(args)
            print(created)
        else:
            manifest = validate_plugin(Path(args.plugin).expanduser().resolve(strict=True))
            print(f"valid: {manifest['name']}")
        return 0
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
