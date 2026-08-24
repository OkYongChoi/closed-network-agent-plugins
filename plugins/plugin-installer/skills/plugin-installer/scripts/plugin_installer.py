#!/usr/bin/env python3
"""Secure, dependency-free installer for catalogued Agent Plugins."""

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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NamedTuple
from urllib.parse import urlsplit


CANONICAL_SOURCE = "https://github.com/OkYongChoi/plugins.git"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
MAX_FILES = 2_000
MAX_BYTES = 50 * 1024 * 1024
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
HTTP_FIELD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key",
}
SENSITIVE_ENV_RE = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)$", re.I)


class InstallError(RuntimeError):
    """Expected source, validation, or installation failure."""


class LoadedPlugin(NamedTuple):
    """Components accepted by resilient Agent Plugins client loading."""

    manifest: dict[str, Any]
    skills: tuple[str, ...]
    mcp: dict[str, Any] | None
    warnings: tuple[str, ...]


class InstallLayout(NamedTuple):
    plugin_parent: Path
    destination: Path
    marketplace_path: Path | None
    lock_path: Path


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise InstallError(f"git command failed: {detail.strip()}") from exc
    return result.stdout.strip()


def inspect_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise InstallError(f"expected a real plugin directory: {root}")
    resolved_root = root.resolve(strict=True)
    seen: dict[str, str] = {}
    files = 0
    size = 0
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        key = relative.as_posix().casefold()
        if key in seen and seen[key] != relative.as_posix():
            raise InstallError(f"case-colliding paths: {seen[key]!r} and {relative.as_posix()!r}")
        seen[key] = relative.as_posix()
        if item.is_symlink():
            raise InstallError(f"symlinks are not allowed: {relative}")
        try:
            item.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise InstallError(f"path escapes plugin root: {relative}") from exc
        if item.is_dir():
            continue
        if not item.is_file():
            raise InstallError(f"unsupported filesystem entry: {relative}")
        stat = item.stat(follow_symlinks=False)
        if stat.st_nlink > 1:
            raise InstallError(f"hard-linked files are not allowed: {relative}")
        files += 1
        size += stat.st_size
        if files > MAX_FILES or size > MAX_BYTES:
            raise InstallError("plugin exceeds file-count or byte-size limit")


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


def _load_manifest(root: Path, warnings: list[str]) -> dict[str, Any]:
    manifest = load_json(root / "plugin.json")
    if not isinstance(manifest, dict):
        raise InstallError("plugin.json must contain an object")
    allowed = {
        "$schema", "name", "version", "description", "author", "homepage",
        "repository", "license", "keywords", "extensions",
    }
    sanitized = {key: value for key, value in manifest.items() if key in allowed}
    for key in sorted(set(manifest) - allowed):
        warnings.append(f"ignored unknown plugin.json field: {key}")
    if "extensions" in sanitized and not isinstance(sanitized["extensions"], dict):
        warnings.append("ignored non-object plugin.json extensions field")
        sanitized.pop("extensions")
    name = sanitized.get("name")
    if sanitized.get("$schema") != PLUGIN_SCHEMA:
        raise InstallError("unsupported Agent Plugins schema")
    if not isinstance(name, str) or len(name) > 64 or not NAME_RE.fullmatch(name):
        raise InstallError(f"invalid plugin name: {name!r}")
    if root.name != name:
        raise InstallError(f"plugin folder {root.name!r} does not match manifest {name!r}")
    for field in ("version", "description", "homepage", "repository", "license"):
        if field in sanitized and not isinstance(sanitized[field], str):
            raise InstallError(f"plugin.json {field!r} must be a string")
    if "author" in sanitized:
        author = sanitized["author"]
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
            raise InstallError("plugin.json author must contain only name, email, and url")
        if any(not isinstance(value, str) for value in author.values()):
            raise InstallError("plugin.json author values must be strings")
    if "keywords" in sanitized and (
        not isinstance(sanitized["keywords"], list)
        or any(not isinstance(value, str) for value in sanitized["keywords"])
    ):
        raise InstallError("plugin.json keywords must be an array of strings")
    return sanitized


def _skill_metadata(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(str(exc)) from exc
    if not lines or lines[0].strip() != "---":
        raise InstallError("missing YAML frontmatter")
    fields: dict[str, str] = {}
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line != line.lstrip():
            raise InstallError("structured YAML is outside the limited parser profile")
        if ":" not in line:
            raise InstallError(f"unsupported frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in {"name", "description", "license", "compatibility", "allowed-tools"}:
            raise InstallError(f"unsupported frontmatter field: {key!r}")
        if key in fields:
            raise InstallError(f"duplicate frontmatter field: {key!r}")
        if not value or value[0] in "[{|>&*!" or " #" in value or ": " in value:
            raise InstallError(f"ambiguous YAML scalar for {key!r}")
        if value[:1] in {"\"", "'"}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1] or "\\" in value[1:-1]:
                raise InstallError(f"unsupported quoted YAML scalar for {key!r}")
            value = value[1:-1]
        elif value[-1:] in {"\"", "'"}:
            raise InstallError(f"unbalanced quoted YAML scalar for {key!r}")
        fields[key] = value
    if not closed:
        raise InstallError("unterminated YAML frontmatter")
    return fields.get("name", ""), fields.get("description", "")


def _load_skills(root: Path, warnings: list[str]) -> tuple[str, ...]:
    skills_root = root / "skills"
    if not skills_root.exists():
        return ()
    if not skills_root.is_dir() or skills_root.is_symlink():
        warnings.append("disabled skills: fixed skills location is not a real directory")
        return ()
    accepted: list[str] = []
    for child in sorted(skills_root.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or child.is_symlink() or not skill_md.is_file() or skill_md.is_symlink():
            continue
        try:
            name, description = _skill_metadata(skill_md)
            if name != child.name or not SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
                raise InstallError("frontmatter name does not match the skill directory")
            if not description or len(description) > 1024:
                raise InstallError("description must contain 1-1024 characters")
            accepted.append(child.name)
        except InstallError as exc:
            warnings.append(f"skipped invalid skill {child.name}: {exc}")
    return tuple(accepted)


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


def _valid_stdio(server: dict[str, Any]) -> str | None:
    allowed = {"type", "command", "args", "env", "cwd"}
    if set(server) - allowed:
        return "contains unknown fields"
    command = server.get("command")
    if not isinstance(command, str) or not command or any(char.isspace() for char in command) or "\x00" in command:
        return "command must be one non-empty executable token"
    if command.startswith("./"):
        if not _safe_relative(command, "./"):
            return "plugin-relative command escapes the plugin root"
    elif "/" in command or "\\" in command or command.startswith((".", "~")) or ":" in command:
        return "command must be a bare executable name or a safe ./ path"
    args = server.get("args", [])
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        return "args must be an array of strings"
    env = server.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        return "env must map strings to strings"
    if {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(env):
        return "env overrides a reserved plugin variable"
    if any(
        SENSITIVE_ENV_RE.search(key)
        and value
        and not value.startswith(("${PLUGIN_ROOT}", "${PLUGIN_DATA}"))
        for key, value in env.items()
    ):
        return "env appears to embed a secret"
    cwd = server.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            return "cwd must be a string"
        if cwd == "${PLUGIN_ROOT}" or cwd == "${PLUGIN_DATA}":
            pass
        elif cwd.startswith("./"):
            if not _safe_relative(cwd, "./"):
                return "cwd escapes the plugin root"
        elif cwd.startswith("${PLUGIN_ROOT}/"):
            if not _safe_relative(cwd, "${PLUGIN_ROOT}/"):
                return "cwd escapes PLUGIN_ROOT"
        elif cwd.startswith("${PLUGIN_DATA}/"):
            if not _safe_relative(cwd, "${PLUGIN_DATA}/"):
                return "cwd escapes PLUGIN_DATA"
        else:
            return "cwd must be plugin-relative or use a supported placeholder"
    return None


def _valid_http(server: dict[str, Any]) -> str | None:
    if set(server) - {"type", "url", "headers"}:
        return "contains unknown fields"
    url = server.get("url")
    if not isinstance(url, str) or not url:
        return "url must be a non-empty string"
    parsed = urlsplit(url)
    try:
        _ = parsed.port
    except ValueError:
        return "url contains an invalid port"
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return "url must not contain user information or a fragment"
    host = parsed.hostname
    loopback = host == "localhost"
    if host:
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        return "url must use HTTPS except for exact loopback hosts"
    if not host:
        return "url must be absolute"
    headers = server.get("headers", {})
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        return "headers must map strings to strings"
    folded = [key.casefold() for key in headers]
    if len(folded) != len(set(folded)):
        return "header names collide case-insensitively"
    for key, value in headers.items():
        if not HTTP_FIELD_RE.fullmatch(key) or "\r" in value or "\n" in value:
            return "contains an invalid HTTP header field"
        if key.casefold() in SENSITIVE_HEADER_NAMES or re.match(r"^(?:Bearer|Basic)\s", value, re.I):
            return "headers appear to embed credentials"
    return None


def _load_mcp(root: Path, warnings: list[str]) -> dict[str, Any] | None:
    path = root / "mcp.json"
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        warnings.append("disabled MCP: fixed mcp.json location is not a real file")
        return None
    try:
        data = load_json(path)
    except InstallError as exc:
        warnings.append(f"disabled MCP: {exc}")
        return None
    if (
        not isinstance(data, dict)
        or set(data) != {"$schema", "mcpServers"}
        or data.get("$schema") != MCP_SCHEMA
        or not isinstance(data.get("mcpServers"), dict)
    ):
        warnings.append("disabled MCP: invalid top-level mcp.json")
        return None
    accepted: dict[str, Any] = {}
    for name, server in data["mcpServers"].items():
        reason: str | None
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            reason = "entry name and value must be a non-empty string and object"
        elif server.get("type") == "stdio":
            reason = _valid_stdio(server)
        elif server.get("type") in {"streamable-http", "sse"}:
            reason = _valid_http(server)
        else:
            reason = "unsupported transport"
        if reason:
            warnings.append(f"skipped invalid MCP server {name!r}: {reason}")
        else:
            accepted[name] = server
    return {"$schema": MCP_SCHEMA, "mcpServers": accepted}


def load_plugin(root: Path) -> LoadedPlugin:
    """Load with Agent Plugins failure boundaries; package safety remains strict."""
    inspect_tree(root)
    warnings: list[str] = []
    manifest = _load_manifest(root, warnings)
    skills = _load_skills(root, warnings)
    mcp = _load_mcp(root, warnings)
    return LoadedPlugin(manifest, skills, mcp, tuple(warnings))


def discover_checkout() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "catalog.json").is_file() and (parent / "plugins").is_dir():
            return parent
    return None


def choose_source(explicit: str | None) -> str:
    if explicit:
        return explicit
    environment = os.environ.get("AGENT_PLUGINS_SOURCE")
    if environment:
        return environment
    checkout = discover_checkout()
    if checkout:
        return str(checkout)
    return CANONICAL_SOURCE


@contextmanager
def materialize_source(source: str, ref: str | None, allow_mutable: bool) -> Iterator[tuple[Path, str | None]]:
    local = Path(source).expanduser()
    if local.exists():
        local_root = local.resolve(strict=True)
        if not local_root.is_dir():
            raise InstallError(f"local source is not a directory: {local_root}")
        if not ref:
            yield local_root, None
            return
        if not FULL_SHA_RE.fullmatch(ref) and not allow_mutable:
            raise InstallError("--ref must be a full commit SHA unless --allow-mutable-ref is set")
        with tempfile.TemporaryDirectory(prefix="plugin-source-") as temp_name:
            root = Path(temp_name) / "repository"
            run_git(["init", "--quiet", str(root)])
            run_git(["-C", str(root), "remote", "add", "origin", str(local_root)])
            run_git(["-C", str(root), "fetch", "--quiet", "--depth=1", "origin", ref])
            run_git(["-C", str(root), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
            actual_ref = run_git(["-C", str(root), "rev-parse", "HEAD"])
            if FULL_SHA_RE.fullmatch(ref) and actual_ref.lower() != ref.lower():
                raise InstallError(f"local source resolved to {actual_ref}, expected {ref}")
            yield root, actual_ref
        return

    if not ref:
        raise InstallError(
            "remote sources require --ref with a full commit SHA; set "
            "AGENT_PLUGINS_SOURCE to an approved local mirror for offline use"
        )
    if not FULL_SHA_RE.fullmatch(ref) and not allow_mutable:
        raise InstallError("remote --ref must be a full 40-character SHA")
    with tempfile.TemporaryDirectory(prefix="plugin-source-") as temp_name:
        root = Path(temp_name) / "repository"
        run_git(["init", "--quiet", str(root)])
        run_git(["-C", str(root), "remote", "add", "origin", source])
        run_git(["-C", str(root), "fetch", "--quiet", "--depth=1", "origin", ref])
        run_git(["-C", str(root), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        actual_ref = run_git(["-C", str(root), "rev-parse", "HEAD"])
        if FULL_SHA_RE.fullmatch(ref) and actual_ref.lower() != ref.lower():
            raise InstallError(f"remote resolved to {actual_ref}, expected {ref}")
        yield root, actual_ref


def read_catalog(root: Path) -> list[dict[str, str]]:
    data = load_json(root / "catalog.json")
    if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(data.get("plugins"), list):
        raise InstallError("unsupported catalog format")
    result: list[dict[str, str]] = []
    names: set[str] = set()
    resolved_root = root.resolve(strict=True)
    for raw in data["plugins"]:
        if not isinstance(raw, dict) or set(raw) != {"name", "path", "content_sha256"}:
            raise InstallError("each catalog entry must contain name, path, and content_sha256")
        name, relative, digest = raw["name"], raw["path"], raw["content_sha256"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise InstallError(f"invalid catalog plugin name: {name!r}")
        if name.casefold() in names:
            raise InstallError(f"duplicate catalog plugin name: {name}")
        names.add(name.casefold())
        if not isinstance(relative, str):
            raise InstallError(f"invalid catalog path for {name}")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise InstallError(f"unsafe catalog path for {name}: {relative!r}")
        try:
            (root / path).resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise InstallError(f"catalog path escapes repository for {name}") from exc
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise InstallError(f"invalid content digest for {name}")
        result.append({"name": name, "path": relative, "content_sha256": digest})
    return result


def select_entry(entries: list[dict[str, str]], name: str) -> dict[str, str]:
    matches = [entry for entry in entries if entry["name"] == name]
    if not matches:
        raise InstallError(f"plugin not found in catalog: {name}")
    return matches[0]


@contextmanager
def verify_entry(root: Path, entry: dict[str, str]) -> Iterator[tuple[Path, LoadedPlugin]]:
    """Snapshot first, then validate the exact private bytes that will be installed."""
    source = (root / entry["path"]).resolve(strict=True)
    inspect_tree(source)
    with tempfile.TemporaryDirectory(prefix=f"verified-{entry['name']}-") as temp_name:
        plugin_root = Path(temp_name) / entry["name"]
        # Preserve links as links so snapshot validation rejects them instead of following them.
        shutil.copytree(source, plugin_root, symlinks=True)
        loaded = load_plugin(plugin_root)
        if loaded.manifest["name"] != entry["name"]:
            raise InstallError("catalog name does not match plugin manifest")
        actual = tree_digest(plugin_root)
        if actual != entry["content_sha256"]:
            raise InstallError(
                f"content digest mismatch for {entry['name']}: expected "
                f"{entry['content_sha256']}, got {actual}"
            )
        yield plugin_root, loaded


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


def project_plugin(source: Path, loaded: LoadedPlugin, target: str, output: Path) -> None:
    shutil.copytree(source, output, symlinks=False)
    manifest = loaded.manifest
    write_json(output / "plugin.json", manifest)
    skills_root = output / "skills"
    if skills_root.exists() and not skills_root.is_dir():
        skills_root.unlink()
    elif skills_root.is_dir():
        for child in skills_root.iterdir():
            if child.name not in loaded.skills:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    mcp_path = output / "mcp.json"
    if loaded.mcp is None:
        if mcp_path.exists():
            if mcp_path.is_dir():
                shutil.rmtree(mcp_path)
            else:
                mcp_path.unlink()
    else:
        write_json(mcp_path, loaded.mcp)
    if target == "portable":
        return
    if loaded.mcp is not None:
        write_json(output / ".mcp.json", _native_mcp(loaded.mcp))
    portable = _native_manifest(manifest)
    if target == "codex":
        description = portable["description"]
        portable["skills"] = "./skills/"
        if loaded.mcp is not None:
            portable["mcpServers"] = "./.mcp.json"
        portable["interface"] = {
            "displayName": manifest["name"].replace("-", " ").title(),
            "shortDescription": description[:120],
            "longDescription": description,
            "developerName": portable["author"]["name"],
            "category": "Developer Tools",
            "capabilities": ["Read", "Write"],
            "defaultPrompt": [f"Use {manifest['name']} to help with this task."],
        }
        write_json(output / ".codex-plugin" / "plugin.json", portable)
    elif target == "claude":
        write_json(output / ".claude-plugin" / "plugin.json", portable)
    else:
        raise InstallError(f"unsupported target: {target}")


def marketplace_value(path: Path, target: str, manifest: dict[str, Any]) -> dict[str, Any]:
    name = manifest["name"]
    if path.exists():
        value = load_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("plugins"), list):
            raise InstallError(f"invalid existing marketplace: {path}")
    elif target == "codex":
        value = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    else:
        value = {
            "name": "personal",
            "owner": {"name": "Local"},
            "metadata": {"description": "Local projections installed from portable Agent Plugins."},
            "plugins": [],
        }
    if any(isinstance(item, dict) and item.get("name") == name for item in value["plugins"]):
        raise InstallError(f"marketplace already contains {name}")
    if target == "codex":
        value["plugins"].append({
            "name": name,
            "source": {"source": "local", "path": f"./plugins/{name}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        })
    else:
        native = _native_manifest(manifest)
        value["plugins"].append({
            "name": name,
            "source": f"./plugins/{name}",
            "description": native["description"],
            "version": native["version"],
        })
    return value


def _layout(args: argparse.Namespace, name: str) -> InstallLayout:
    if args.agent_home:
        agent_home = Path(args.agent_home).expanduser().resolve()
    elif args.scope == "project":
        agent_home = Path.cwd().resolve() / (".claude" if args.target == "claude" else ".agents")
    else:
        agent_home = Path.home() / (".claude" if args.target == "claude" else ".agents")

    explicit_parent = Path(args.dest).expanduser().resolve() if args.dest else None
    if args.target == "portable":
        parent = explicit_parent or agent_home / "plugins"
        return InstallLayout(parent, parent / name, None, parent / f".{name}.install.lock")
    if args.target == "codex":
        # Codex resolves ./plugins/name in ~/.agents/plugins/marketplace.json to ~/plugins/name.
        expected_parent = agent_home.parent / "plugins"
        if explicit_parent is not None and explicit_parent != expected_parent:
            raise InstallError(f"Codex --dest must be {expected_parent} for marketplace path semantics")
        parent = expected_parent
        marketplace = agent_home / "plugins" / "marketplace.json"
        return InstallLayout(parent, parent / name, marketplace, marketplace.parent / ".marketplace.install.lock")
    # Claude marketplaces are self-contained roots with .claude-plugin/ and plugins/ siblings.
    if explicit_parent is not None and explicit_parent.name != "plugins":
        raise InstallError("Claude --dest must name the plugins/ directory directly below a marketplace root")
    marketplace_root = explicit_parent.parent if explicit_parent else agent_home / "plugins/marketplaces/okyongchoi-portable"
    parent = explicit_parent or marketplace_root / "plugins"
    marketplace = marketplace_root / ".claude-plugin" / "marketplace.json"
    return InstallLayout(parent, parent / name, marketplace, marketplace_root / ".marketplace.install.lock")


def install(source_plugin: Path, loaded: LoadedPlugin, args: argparse.Namespace) -> Path:
    manifest = loaded.manifest
    layout = _layout(args, manifest["name"])
    parent, destination = layout.plugin_parent, layout.destination
    parent.mkdir(parents=True, exist_ok=True)
    layout.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = layout.lock_path
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise InstallError(f"another install is in progress: {lock}") from exc
    installed = False
    marketplace_tmp: Path | None = None
    try:
        if destination.exists():
            raise InstallError(f"destination already exists: {destination}")
        temp_root = Path(tempfile.mkdtemp(prefix=f".{manifest['name']}.", dir=parent))
        staged = temp_root / manifest["name"]
        project_plugin(source_plugin, loaded, args.target, staged)
        validate_projection(staged, manifest["name"], args.target)
        marketplace = layout.marketplace_path
        marketplace_data = marketplace_value(marketplace, args.target, manifest) if marketplace else None
        os.replace(staged, destination)
        installed = True
        if marketplace and marketplace_data is not None:
            marketplace_tmp = marketplace.with_name(f".{marketplace.name}.{os.getpid()}.tmp")
            write_json(marketplace_tmp, marketplace_data)
            os.replace(marketplace_tmp, marketplace)
            marketplace_tmp = None
        shutil.rmtree(temp_root, ignore_errors=True)
        return destination
    except Exception:
        if installed and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if marketplace_tmp is not None:
            try:
                marketplace_tmp.unlink()
            except FileNotFoundError:
                pass
        try:
            lock.rmdir()
        except OSError:
            pass


def validate_projection(root: Path, name: str, target: str) -> None:
    if target == "portable":
        load_plugin(root)
        return
    vendor_manifest = root / f".{target}-plugin" / "plugin.json"
    data = load_json(vendor_manifest)
    if not isinstance(data, dict) or data.get("name") != name:
        raise InstallError(f"invalid {target} projection")
    native_mcp = root / ".mcp.json"
    if native_mcp.exists():
        payload = load_json(native_mcp)
        if not isinstance(payload, dict) or set(payload) != {"mcpServers"} or not isinstance(payload["mcpServers"], dict):
            raise InstallError(f"invalid {target} native .mcp.json")
        for server_name, server in payload["mcpServers"].items():
            if not isinstance(server_name, str) or not server_name or not isinstance(server, dict):
                raise InstallError(f"invalid {target} native MCP server entry")
            if server.get("type") == "streamable-http":
                raise InstallError(f"unprojected Agent Plugins MCP transport in {target} layout")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source")
    common.add_argument("--ref")
    common.add_argument("--allow-mutable-ref", action="store_true")
    sub.add_parser("list", parents=[common], help="list catalogued plugins")
    install_parser = sub.add_parser("install", parents=[common], help="install a catalogued plugin")
    install_parser.add_argument("name")
    install_parser.add_argument("--target", choices=("portable", "codex", "claude"), required=True)
    install_parser.add_argument("--agent-home")
    install_parser.add_argument("--scope", choices=("user", "project"), default="user")
    install_parser.add_argument("--dest", help="override the destination plugin parent")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        source = choose_source(args.source)
        with materialize_source(source, args.ref, args.allow_mutable_ref) as (root, actual_ref):
            entries = read_catalog(root)
            if args.command == "list":
                for entry in entries:
                    print(f"{entry['name']}\t{entry['content_sha256']}\t{entry['path']}")
            else:
                entry = select_entry(entries, args.name)
                with verify_entry(root, entry) as (plugin_root, loaded):
                    for warning in loaded.warnings:
                        print(f"warning: {warning}", file=sys.stderr)
                    destination = install(plugin_root, loaded, args)
                    revision = actual_ref or "local checkout"
                    print(f"installed {loaded.manifest['name']} from {revision} to {destination}")
        return 0
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
