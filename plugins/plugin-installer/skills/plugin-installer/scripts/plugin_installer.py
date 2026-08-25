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
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple
from urllib.parse import urlsplit, urlunsplit


CANONICAL_SOURCE = "https://github.com/OkYongChoi/closed-network-agent-plugins.git"
APPROVED_POINTER_REF = "refs/heads/latest-approved"
CANONICAL_REF = "e0fbb53a8d04a26fd6f14051ed4ca855edb31070"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
MAX_FILES = 2_000
MAX_BYTES = 50 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 60
LOCK_STALE_SECONDS = 60 * 60
MAX_PORTABLE_COMPONENT_UNITS = 255
MAX_PORTABLE_RELATIVE_UNITS = 240
RUNTIME_ARTIFACT_DIRS = {"__pycache__", ".pytest_cache"}
RUNTIME_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
HTTP_FIELD_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key",
}
SENSITIVE_ENV_RE = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)$", re.I)
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_CONFIG_BYTES = 64 * 1024
CONFIG_TOP_LEVEL_KEYS = frozenset({"skills", "plugins", "agentHome"})
CONFIG_REPOSITORY_KEYS = frozenset({"source", "ref", "allowMutableRef"})
CONFIG_PLUGIN_KEYS = CONFIG_REPOSITORY_KEYS | {"defaultTarget"}
RELEASE_MANIFEST_NAME = "release-manifest.json"
RELEASE_MANIFEST_KEYS = frozenset({
    "formatVersion", "kind", "name", "source", "ref", "version", "updatedAt",
    "sequence", "catalogSha256", "packages",
})
RELEASE_PACKAGE_KEYS = frozenset({"name", "digest"})
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


class InstallError(RuntimeError):
    """Expected source, validation, or installation failure."""


class EffectiveConfig(NamedTuple):
    source: str
    ref: str | None
    allow_mutable_ref: bool
    target: str
    agent_home: str | None
    scope: str
    provenance: dict[str, str]


class ApprovedRelease(NamedTuple):
    source: str
    ref: str
    version: str
    updated_at: str
    catalog_sha256: str
    packages: dict[str, str]
    sequence: int


class InstallRecord(NamedTuple):
    source: str
    ref: str | None
    digest: str
    version: str | None


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def validate_portable_component(value: str, *, label: str = "path component") -> None:
    if not value or value in {".", ".."}:
        raise InstallError(f"invalid {label}: {value!r}")
    if value != unicodedata.normalize("NFC", value):
        raise InstallError(f"{label} is not Unicode NFC-normalized: {value!r}")
    compatibility = unicodedata.normalize("NFKC", value)
    if value[-1] in {".", " "} or compatibility[-1] in {".", " "}:
        raise InstallError(f"{label} ends with a dot or space: {value!r}")
    if any(
        ord(char) < 32 or char in WINDOWS_FORBIDDEN_CHARS
        for char in (*value, *compatibility)
    ):
        raise InstallError(f"{label} contains a Windows-reserved character: {value!r}")
    if compatibility.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        raise InstallError(f"{label} is a Windows device name: {value!r}")
    if _utf16_units(value) > MAX_PORTABLE_COMPONENT_UNITS or len(value.encode("utf-8")) > 255:
        raise InstallError(f"{label} is too long for a portable filesystem: {value!r}")


def validate_portable_relative(parts: tuple[str, ...], *, label: str = "relative path") -> None:
    for part in parts:
        validate_portable_component(part, label=label)
    rendered = "/".join(parts)
    if _utf16_units(rendered) > MAX_PORTABLE_RELATIVE_UNITS:
        raise InstallError(
            f"{label} exceeds the {MAX_PORTABLE_RELATIVE_UNITS}-unit portable limit: {rendered!r}"
        )


def _is_reparse(stat_result: os.stat_result) -> bool:
    return bool(getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_file_link_count(path: Path) -> int:
    """Read the hard-link count from an opened Windows file handle."""
    try:
        import ctypes
        from ctypes import wintypes

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation),
        )
        get_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        handle = create_file(str(path), 0, share_all, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            information = ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
            if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise InstallError(f"cannot count links through a reparse point: {path}")
            count = int(information.nNumberOfLinks)
            if count < 1:
                raise OSError("Windows returned an invalid hard-link count")
            return count
        finally:
            close_handle(handle)
    except InstallError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise InstallError(f"cannot determine hard-link count safely: {path}") from exc


def _file_link_count(path: Path, file_stat: object, *, platform: str | None = None) -> int:
    initial = getattr(file_stat, "st_nlink", 0)
    if isinstance(initial, int) and initial > 0:
        return initial
    try:
        refreshed = os.stat(path, follow_symlinks=False)
    except OSError:
        refreshed = None
    refreshed_count = getattr(refreshed, "st_nlink", 0)
    if isinstance(refreshed_count, int) and refreshed_count > 0:
        return refreshed_count
    if (platform or os.name) == "nt":
        return _windows_file_link_count(path)
    raise InstallError(f"cannot determine hard-link count safely: {path}")


def _collect_tree(root: Path) -> list[tuple[Path, str, os.stat_result]]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InstallError(f"cannot inspect plugin root {root}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink() or _is_reparse(root_stat):
        raise InstallError(f"expected a real plugin directory: {root}")
    collected: list[tuple[Path, str, os.stat_result]] = []
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, parent_parts = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise InstallError(f"cannot inspect directory {directory}: {exc}") from exc
        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            parts = (*parent_parts, entry.name)
            validate_portable_relative(parts)
            relative = "/".join(parts)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise InstallError(f"cannot inspect {relative}: {exc}") from exc
            if entry.is_symlink() or _is_reparse(entry_stat):
                raise InstallError(f"symlinks and junctions are not allowed: {relative}")
            collected.append((Path(entry.path), relative, entry_stat))
            if stat.S_ISDIR(entry_stat.st_mode):
                child_directories.append((Path(entry.path), parts))
            elif not stat.S_ISREG(entry_stat.st_mode):
                raise InstallError(f"unsupported filesystem entry: {relative}")
        stack.extend(reversed(child_directories))
    return collected


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
    state_path: Path
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
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"git command timed out after {GIT_TIMEOUT_SECONDS} seconds") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise InstallError(f"git command failed: {detail.strip()}") from exc
    return result.stdout.strip()


def inspect_tree(root: Path) -> None:
    seen: dict[str, str] = {}
    files = 0
    size = 0
    for item, relative, item_stat in _collect_tree(root):
        relative_path = Path(relative)
        if (
            any(part in RUNTIME_ARTIFACT_DIRS for part in relative_path.parts)
            or item.suffix.lower() in RUNTIME_ARTIFACT_SUFFIXES
        ):
            raise InstallError(f"runtime artifacts are forbidden in canonical packages: {relative}")
        key = "/".join(_portable_key(part) for part in relative_path.parts)
        if key in seen and seen[key] != relative:
            raise InstallError(f"portable path collision: {seen[key]!r} and {relative!r}")
        seen[key] = relative
        if stat.S_ISDIR(item_stat.st_mode):
            continue
        if _file_link_count(item, item_stat) > 1:
            raise InstallError(f"hard-linked files are not allowed: {relative}")
        files += 1
        size += item_stat.st_size
        if files > MAX_FILES or size > MAX_BYTES:
            raise InstallError("plugin exceeds file-count or byte-size limit")


def tree_digest(root: Path) -> str:
    inspect_tree(root)
    digest = hashlib.sha256()
    files = [(path, relative) for path, relative, info in _collect_tree(root) if stat.S_ISREG(info.st_mode)]
    for path, relative_text in sorted(files, key=lambda item: item[1]):
        relative = relative_text.encode("utf-8")
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
    validate_portable_component(name, label="plugin name")
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
            validate_portable_component(name, label="skill name")
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


def config_paths() -> tuple[Path, Path]:
    """Return user and system config paths for the current platform."""
    if os.name == "nt":
        user_root = Path(os.environ.get("USERPROFILE") or Path.home())
        system_root = Path(os.environ.get("ProgramData") or "C:/ProgramData")
        return user_root / ".agents" / "config.json", system_root / "AgentTools" / "config.json"
    return Path.home() / ".agents" / "config.json", Path("/etc/agent-tools/config.json")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _valid_config_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\x00" not in value
    )


def _validate_repository_config(value: Any, *, section: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise InstallError(f"invalid config {path}: {section} must be an object")
    allowed = CONFIG_PLUGIN_KEYS if section == "plugins" else CONFIG_REPOSITORY_KEYS
    unknown = set(value) - allowed
    if unknown:
        raise InstallError(f"invalid config {path}: unknown {section} key(s): {', '.join(sorted(unknown))}")
    for key in ("source", "ref"):
        if key in value and not _valid_config_string(value[key]):
            raise InstallError(
                f"invalid config {path}: {section}.{key} must be a non-empty, trimmed string without NUL"
            )
    if "allowMutableRef" in value and not isinstance(value["allowMutableRef"], bool):
        raise InstallError(f"invalid config {path}: {section}.allowMutableRef must be boolean")
    if section == "plugins" and "defaultTarget" in value:
        if value["defaultTarget"] not in {"portable", "codex", "claude"}:
            raise InstallError(
                f"invalid config {path}: plugins.defaultTarget must be portable, codex, or claude"
            )


def _read_config_bytes(path: Path) -> bytes | None:
    """Read a real, singly-linked config file without following a final symlink."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(f"cannot inspect config {path}: {exc}") from exc
    if path.is_symlink() or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise InstallError(f"config must be a real regular file, not a link or reparse point: {path}")
    if _file_link_count(path, before) != 1:
        raise InstallError(f"config must not be hard-linked: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallError(f"cannot open config safely {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if _is_reparse(opened) or not stat.S_ISREG(opened.st_mode):
            raise InstallError(f"opened config is not a real regular file: {path}")
        if _file_link_count(path, opened) != 1:
            raise InstallError(f"opened config must not be hard-linked: {path}")
        before_identity = (getattr(before, "st_dev", None), getattr(before, "st_ino", None))
        opened_identity = (getattr(opened, "st_dev", None), getattr(opened, "st_ino", None))
        if before_identity != opened_identity:
            raise InstallError(f"config changed while it was being opened: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise InstallError(f"config is larger than {MAX_CONFIG_BYTES} bytes: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_config(path: Path) -> dict[str, Any]:
    """Load one strict, size-bounded JSON config; an absent file is an empty layer."""
    raw = _read_config_bytes(path)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"invalid JSON config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"invalid config {path}: top level must be an object")
    unknown = set(data) - CONFIG_TOP_LEVEL_KEYS
    if unknown:
        raise InstallError(f"invalid config {path}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    if "agentHome" in data and not _valid_config_string(data["agentHome"]):
        raise InstallError(
            f"invalid config {path}: agentHome must be a non-empty, trimmed string without NUL"
        )
    for section in ("skills", "plugins"):
        if section in data:
            _validate_repository_config(data[section], section=section, path=path)
    return data


def _environment_value(name: str) -> str | None:
    if name not in os.environ:
        return None
    value = os.environ[name]
    if not _valid_config_string(value):
        raise InstallError(
            f"environment variable {name} must be non-empty, trimmed, and contain no NUL"
        )
    return value


def _select_value(
    cli_value: Any,
    environment_name: str | None,
    user_value: Any,
    system_value: Any,
    fallback: Any,
    *,
    field: str,
    provenance: dict[str, str],
) -> Any:
    if cli_value is not None:
        provenance[field] = "cli"
        return cli_value
    if environment_name:
        environment = _environment_value(environment_name)
        if environment is not None:
            provenance[field] = f"environment:{environment_name}"
            return environment
    if user_value is not None:
        provenance[field] = "user-config"
        return user_value
    if system_value is not None:
        provenance[field] = "system-config"
        return system_value
    provenance[field] = "embedded-fallback"
    return fallback


def resolve_effective_config(args: argparse.Namespace) -> EffectiveConfig:
    """Resolve plugin settings field-by-field using the documented precedence."""
    user_path, system_path = config_paths()
    # config_paths is the platform authority for the current user's home. Using
    # its parent also works in stripped-down Windows service/CI environments
    # where pathlib.Path.home() cannot consult USERPROFILE or HOMEDRIVE/HOMEPATH.
    user_home = user_path.parent.parent
    system = load_config(system_path)
    user = load_config(user_path)
    system_plugins = system.get("plugins", {})
    user_plugins = user.get("plugins", {})
    provenance: dict[str, str] = {}

    cli_source = getattr(args, "source", None)
    cli_ref = getattr(args, "ref", None)
    checkout = discover_checkout()
    configured_source = _select_value(
        cli_source,
        "AGENT_PLUGINS_SOURCE",
        user_plugins.get("source"),
        system_plugins.get("source"),
        None,
        field="source",
        provenance=provenance,
    )
    configured_ref = _select_value(
        cli_ref,
        "AGENT_PLUGINS_REF",
        user_plugins.get("ref"),
        system_plugins.get("ref"),
        None,
        field="ref",
        provenance=provenance,
    )
    if configured_source is not None and not _valid_config_string(configured_source):
        raise InstallError("effective source must be a non-empty, trimmed string without NUL")
    if configured_ref is not None and not _valid_config_string(configured_ref):
        raise InstallError("effective ref must be a non-empty, trimmed string without NUL")
    if configured_source is None and checkout is not None:
        source = str(checkout)
        provenance["source"] = "current-checkout"
        ref = configured_ref
        if ref is None:
            provenance["ref"] = "current-checkout"
    else:
        source = configured_source or CANONICAL_SOURCE
        if configured_source is None:
            provenance["source"] = "embedded-fallback"
        if configured_ref is not None:
            ref = configured_ref
        elif configured_source is not None:
            # A centrally selected source without a fixed ref follows that
            # repository's latest-approved pointer. The pointer itself is
            # mutable; its payload ref is always an immutable full SHA.
            ref = None
            provenance["ref"] = "approval-pointer"
        else:
            ref = CANONICAL_REF
            provenance["ref"] = "embedded-fallback"

    cli_mutable = getattr(args, "allow_mutable_ref", None)
    allow_mutable = _select_value(
        cli_mutable,
        None,
        user_plugins.get("allowMutableRef"),
        system_plugins.get("allowMutableRef"),
        False,
        field="allowMutableRef",
        provenance=provenance,
    )
    target = _select_value(
        getattr(args, "target", None),
        None,
        user_plugins.get("defaultTarget"),
        system_plugins.get("defaultTarget"),
        "portable",
        field="target",
        provenance=provenance,
    )
    agent_home = _select_value(
        getattr(args, "agent_home", None),
        None,
        user.get("agentHome"),
        system.get("agentHome"),
        None,
        field="agentHome",
        provenance=provenance,
    )
    scope = _select_value(
        getattr(args, "scope", None),
        None,
        None,
        None,
        "user",
        field="scope",
        provenance=provenance,
    )
    ignored_agent_home_origin: str | None = None
    if scope == "project" and agent_home is not None and provenance["agentHome"] != "cli":
        origin = provenance["agentHome"]
        agent_home = None
        ignored_agent_home_origin = origin
    if agent_home is not None and not _valid_config_string(agent_home):
        raise InstallError("effective agent home must be a non-empty, trimmed string without NUL")
    if agent_home is None:
        leaf = ".claude" if target == "claude" else ".agents"
        if scope == "project":
            agent_home = str((Path.cwd() / leaf).resolve())
            provenance["agentHome"] = (
                f"project-default:ignored-{ignored_agent_home_origin}"
                if ignored_agent_home_origin else "project-default"
            )
        else:
            agent_home = str((user_home / leaf).resolve())
            provenance["agentHome"] = "user-default"
    else:
        if agent_home == "~":
            resolved_home = user_home
        elif agent_home.startswith("~/") or agent_home.startswith("~\\"):
            resolved_home = user_home / agent_home[2:]
        elif agent_home.startswith("~"):
            raise InstallError(
                "effective agent home only supports the current user's ~ prefix"
            )
        else:
            resolved_home = Path(agent_home)
        agent_home = str(resolved_home.resolve())
    if ref is not None and not FULL_SHA_RE.fullmatch(ref) and not allow_mutable:
        raise InstallError("effective ref must be a full commit SHA unless mutable refs are explicitly allowed")
    return EffectiveConfig(source, ref, allow_mutable, target, agent_home, scope, provenance)


def _redact_source(source: str) -> str:
    try:
        parsed = urlsplit(source)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc:
        return source
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if port is not None:
        hostname = f"{hostname}:{port}"
    netloc = f"***@{hostname}" if parsed.username is not None else hostname
    query = "REDACTED" if parsed.query else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def effective_config_payload(config: EffectiveConfig) -> dict[str, Any]:
    return {
        "source": _redact_source(config.source),
        "ref": config.ref,
        "allowMutableRef": config.allow_mutable_ref,
        "target": config.target,
        "agentHome": config.agent_home,
        "scope": config.scope,
        "provenance": config.provenance,
    }


def _source_identity(value: str) -> str:
    """Return a credential-free identity suitable for release source binding."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.rstrip("/\\")
    if not parsed.scheme or not parsed.netloc:
        return value.rstrip("/\\")
    host = (parsed.hostname or "").casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise InstallError("source contains an invalid port") from exc
    if port is not None:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), host, path, "", ""))


def _manifest_source_for(source: str) -> str:
    """Resolve a local Git mirror to its credential-free upstream identity."""
    local = Path(source).expanduser()
    if local.exists():
        try:
            origin = run_git(["-C", str(local.resolve(strict=True)), "remote", "get-url", "origin"])
        except InstallError:
            return str(local.resolve(strict=True))
        return _source_identity(origin)
    return _source_identity(source)


def _local_has_approved_pointer(source: str) -> bool:
    local = Path(source).expanduser()
    if not local.exists():
        return False
    if (local / "HEAD").is_file() and (local / "objects").is_dir():
        return True
    try:
        result = subprocess.run(
            [
                "git", "-C", str(local), "show-ref", "--verify", "--quiet",
                APPROVED_POINTER_REF,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _load_release_manifest(path: Path) -> ApprovedRelease:
    try:
        raw = _read_config_bytes(path)
        if raw is None:
            raise InstallError(f"approved release manifest is missing: {path}")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except InstallError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"invalid approved release manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != RELEASE_MANIFEST_KEYS:
        raise InstallError("approved release manifest has unsupported or missing fields")
    if data["formatVersion"] != 1 or data["kind"] != "agent-plugins-release" or data["name"] != "plugins":
        raise InstallError("approved release manifest has an unsupported identity")
    source = data["source"]
    ref = data["ref"]
    version = data["version"]
    updated_at = data["updatedAt"]
    catalog_sha = data["catalogSha256"]
    sequence = data["sequence"]
    if not _valid_config_string(source):
        raise InstallError("approved release source must be a non-empty trimmed string")
    try:
        parsed_source = urlsplit(source)
    except ValueError as exc:
        raise InstallError("approved release source is invalid") from exc
    if parsed_source.scheme and (
        parsed_source.username is not None
        or parsed_source.password is not None
        or parsed_source.query
        or parsed_source.fragment
    ):
        raise InstallError("approved release source must not contain credentials, query, or fragment")
    if not isinstance(ref, str) or FULL_SHA_RE.fullmatch(ref) is None:
        raise InstallError("approved release ref must be a full commit SHA")
    if ref != ref.lower():
        raise InstallError("approved release ref must use lowercase hexadecimal")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise InstallError("approved release version is invalid")
    if not isinstance(updated_at, str):
        raise InstallError("approved release updatedAt must be a UTC timestamp")
    try:
        parsed_time = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise InstallError("approved release updatedAt must use YYYY-MM-DDTHH:MM:SSZ") from exc
    if parsed_time.strftime("%Y-%m-%dT%H:%M:%SZ") != updated_at:
        raise InstallError("approved release updatedAt is not canonical")
    if not isinstance(catalog_sha, str) or DIGEST_RE.fullmatch(catalog_sha) is None:
        raise InstallError("approved release catalogSha256 is invalid")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise InstallError("approved release sequence must be a non-negative integer")
    packages_raw = data["packages"]
    if not isinstance(packages_raw, list) or not packages_raw:
        raise InstallError("approved release packages must be a non-empty array")
    packages: dict[str, str] = {}
    portable_names: set[str] = set()
    for item in packages_raw:
        if not isinstance(item, dict) or set(item) != RELEASE_PACKAGE_KEYS:
            raise InstallError("approved release package must contain only name and digest")
        package_name, digest = item["name"], item["digest"]
        if not isinstance(package_name, str) or NAME_RE.fullmatch(package_name) is None:
            raise InstallError(f"invalid approved package name: {package_name!r}")
        validate_portable_component(package_name, label="approved package name")
        key = _portable_key(package_name)
        if key in portable_names:
            raise InstallError(f"duplicate approved package name: {package_name}")
        portable_names.add(key)
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise InstallError(f"invalid approved package digest: {package_name}")
        packages[package_name] = digest
    return ApprovedRelease(source, ref, version, updated_at, catalog_sha, packages, sequence)


@contextmanager
def resolve_approved_release(source: str) -> Iterator[ApprovedRelease]:
    """Read the mutable pointer, but trust only its immutable, source-bound payload."""
    with tempfile.TemporaryDirectory(prefix="plugin-approved-pointer-") as temp_name:
        root = Path(temp_name) / "pointer"
        run_git(["init", "--quiet", str(root)])
        run_git(["-C", str(root), "remote", "add", "origin", source])
        run_git([
            "-C", str(root), "fetch", "--quiet", "--depth=1", "origin", APPROVED_POINTER_REF,
        ])
        run_git(["-C", str(root), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        pointer_files = run_git(["-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
        if pointer_files != [RELEASE_MANIFEST_NAME]:
            raise InstallError("latest-approved must contain only release-manifest.json")
        manifest = _load_release_manifest(root / RELEASE_MANIFEST_NAME)
        expected_source = _manifest_source_for(source)
        if _source_identity(manifest.source) != expected_source:
            raise InstallError(
                "approved release source does not match the configured repository"
            )
        yield manifest


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


@contextmanager
def materialize_effective_source(
    effective: EffectiveConfig,
) -> Iterator[tuple[Path, str | None, ApprovedRelease | None]]:
    """Resolve either an explicit snapshot, embedded checkout, or approved pointer."""
    local_without_pointer = (
        Path(effective.source).expanduser().exists()
        and not _local_has_approved_pointer(effective.source)
    )
    if (
        effective.ref is not None
        or effective.provenance.get("ref") == "current-checkout"
        or local_without_pointer
    ):
        with materialize_source(
            effective.source, effective.ref, effective.allow_mutable_ref
        ) as (root, actual_ref):
            yield root, actual_ref, None
        return
    with resolve_approved_release(effective.source) as release:
        with materialize_source(effective.source, release.ref, False) as (root, actual_ref):
            if actual_ref is None or actual_ref.lower() != release.ref:
                raise InstallError("approved release did not resolve to its immutable ref")
            yield root, actual_ref, release


def _real_catalog_path(root: Path, relative: str, name: str) -> Path:
    if not relative or relative.startswith("/") or "\\" in relative:
        raise InstallError(f"unsafe catalog path for {name}: {relative!r}")
    parts = tuple(relative.split("/"))
    validate_portable_relative(parts, label=f"catalog path for {name}")
    if len(parts) != 2 or parts[0] != "plugins" or parts[1] != name:
        raise InstallError(f"catalog path for {name} must be exactly plugins/{name}")
    current = root
    for part in parts:
        current = current / part
        try:
            item_stat = current.lstat()
        except OSError as exc:
            raise InstallError(f"cannot inspect catalog path component for {name}: {part!r}") from exc
        if current.is_symlink() or _is_reparse(item_stat):
            raise InstallError(f"catalog path for {name} contains a symlink or junction")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise InstallError(f"catalog path escapes repository for {name}") from exc
    return current


def read_catalog(root: Path) -> list[dict[str, str]]:
    catalog_path = root / "catalog.json"
    try:
        catalog_stat = catalog_path.lstat()
    except OSError as exc:
        raise InstallError(f"cannot inspect catalog.json: {exc}") from exc
    if catalog_path.is_symlink() or _is_reparse(catalog_stat) or not stat.S_ISREG(catalog_stat.st_mode):
        raise InstallError("catalog.json must be a real regular file, not a symlink or junction")
    data = load_json(catalog_path)
    if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(data.get("plugins"), list):
        raise InstallError("unsupported catalog format")
    result: list[dict[str, str]] = []
    names: set[str] = set()
    paths: set[str] = set()
    for raw in data["plugins"]:
        if not isinstance(raw, dict) or set(raw) != {"name", "path", "content_sha256"}:
            raise InstallError("each catalog entry must contain name, path, and content_sha256")
        name, relative, digest = raw["name"], raw["path"], raw["content_sha256"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise InstallError(f"invalid catalog plugin name: {name!r}")
        validate_portable_component(name, label="catalog plugin name")
        name_key = _portable_key(name)
        if name_key in names:
            raise InstallError(f"duplicate catalog plugin name: {name}")
        names.add(name_key)
        if not isinstance(relative, str):
            raise InstallError(f"invalid catalog path for {name}")
        path_key = "/".join(_portable_key(part) for part in relative.split("/"))
        if path_key in paths:
            raise InstallError(f"portable catalog path collision for {name}: {relative!r}")
        paths.add(path_key)
        _real_catalog_path(root, relative, name)
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise InstallError(f"invalid content digest for {name}")
        result.append({"name": name, "path": relative, "content_sha256": digest})
    return result


def verify_release_catalog(
    root: Path, entries: list[dict[str, str]], release: ApprovedRelease
) -> None:
    try:
        actual_catalog = hashlib.sha256((root / "catalog.json").read_bytes()).hexdigest()
    except OSError as exc:
        raise InstallError(f"cannot hash approved catalog: {exc}") from exc
    if actual_catalog != release.catalog_sha256:
        raise InstallError(
            f"approved catalog digest mismatch: expected {release.catalog_sha256}, "
            f"got {actual_catalog}"
        )
    catalog_packages = {entry["name"]: entry["content_sha256"] for entry in entries}
    if catalog_packages != release.packages:
        raise InstallError("approved release package digests do not match catalog.json")


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


def marketplace_value(
    path: Path, target: str, manifest: dict[str, Any], *, replace_existing: bool = False
) -> dict[str, Any]:
    name = manifest["name"]
    if os.path.lexists(path):
        path_stat = path.lstat()
        if path.is_symlink() or _is_reparse(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise InstallError(f"marketplace must be a real regular file: {path}")
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
    matches = [
        index for index, item in enumerate(value["plugins"])
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) > 1:
        raise InstallError(f"marketplace contains duplicate entries for {name}")
    if matches and not replace_existing:
        raise InstallError(f"marketplace already contains {name}")
    if target == "codex":
        entry = {
            "name": name,
            "source": {"source": "local", "path": f"./plugins/{name}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }
    else:
        native = _native_manifest(manifest)
        entry = {
            "name": name,
            "source": f"./plugins/{name}",
            "description": native["description"],
            "version": native["version"],
        }
    if matches:
        value["plugins"][matches[0]] = entry
    else:
        value["plugins"].append(entry)
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
        return InstallLayout(
            parent, parent / name, None, parent / f".{name}.install-state.json",
            parent / f".{name}.install.lock",
        )
    if args.target == "codex":
        # Codex resolves ./plugins/name in ~/.agents/plugins/marketplace.json to ~/plugins/name.
        expected_parent = agent_home.parent / "plugins"
        if explicit_parent is not None and explicit_parent != expected_parent:
            raise InstallError(f"Codex --dest must be {expected_parent} for marketplace path semantics")
        parent = expected_parent
        marketplace = agent_home / "plugins" / "marketplace.json"
        return InstallLayout(
            parent, parent / name, marketplace, parent / f".{name}.install-state.json",
            marketplace.parent / ".marketplace.install.lock",
        )
    # Claude marketplaces are self-contained roots with .claude-plugin/ and plugins/ siblings.
    if explicit_parent is not None and explicit_parent.name != "plugins":
        raise InstallError("Claude --dest must name the plugins/ directory directly below a marketplace root")
    marketplace_root = explicit_parent.parent if explicit_parent else agent_home / "plugins/marketplaces/okyongchoi-portable"
    parent = explicit_parent or marketplace_root / "plugins"
    marketplace = marketplace_root / ".claude-plugin" / "marketplace.json"
    return InstallLayout(
        parent, parent / name, marketplace, parent / f".{name}.install-state.json",
        marketplace_root / ".marketplace.install.lock",
    )


def _windows_exit_code_is_alive(exit_code: int | None) -> bool:
    """Treat unknown state as live; Windows reports 259 for a running process."""
    return exit_code is None or exit_code == 259


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    exit_code = wintypes.DWORD()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return _windows_exit_code_is_alive(None)
                    return _windows_exit_code_is_alive(exit_code.value)
                finally:
                    kernel32.CloseHandle(handle)
            # Access denied means the process exists but is protected. Invalid parameter means gone.
            return ctypes.get_last_error() != 87
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Fields that must remain stable while a lock directory is being claimed."""
    return (
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
        int(getattr(value, "st_nlink", 0)),
        int(getattr(value, "st_size", 0)),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _acquire_recovery_guard(lock: Path) -> tuple[Path, str] | None:
    guard = lock.with_name(f".{lock.name}.recovery-guard")
    token = secrets.token_hex(16)
    try:
        guard.mkdir()
    except FileExistsError:
        return None
    try:
        write_json(guard / "owner.json", {"token": token})
    except Exception:
        shutil.rmtree(guard, ignore_errors=True)
        raise
    return guard, token


def _release_recovery_guard(guard: Path, token: str) -> None:
    """Remove only the guard created by this caller; otherwise fail closed."""
    try:
        owner_path = guard / "owner.json"
        owner_stat = owner_path.lstat()
        if owner_path.is_symlink() or _is_reparse(owner_stat):
            return
        owner = load_json(owner_path)
        if not isinstance(owner, dict) or owner.get("token") != token:
            return
        owner_path.unlink()
        guard.rmdir()
    except (InstallError, OSError):
        pass


def _same_lock_snapshot(
    lock: Path,
    expected_lock: tuple[int, int, int, int, int, int],
    expected_owner: tuple[int, int, int, int, int, int],
    expected_token: str,
) -> bool:
    """Recheck identity and ownership immediately before a directory rename."""
    owner_path = lock / "owner.json"
    try:
        lock_before = lock.lstat()
        owner_before = owner_path.lstat()
        if (
            lock.is_symlink()
            or _is_reparse(lock_before)
            or not stat.S_ISDIR(lock_before.st_mode)
            or owner_path.is_symlink()
            or _is_reparse(owner_before)
            or not stat.S_ISREG(owner_before.st_mode)
            or _stat_identity(lock_before) != expected_lock
            or _stat_identity(owner_before) != expected_owner
        ):
            return False
        owner = load_json(owner_path)
        lock_after = lock.lstat()
        owner_after = owner_path.lstat()
    except (InstallError, OSError):
        return False
    return (
        isinstance(owner, dict)
        and owner.get("token") == expected_token
        and _stat_identity(lock_after) == expected_lock
        and _stat_identity(owner_after) == expected_owner
    )


def _remove_owned_lock(lock: Path, token: str) -> None:
    """Quarantine and remove only the lock directory still owned by token."""
    guard_claim = _acquire_recovery_guard(lock)
    if guard_claim is None:
        return
    guard, guard_token = guard_claim
    try:
        owner_path = lock / "owner.json"
        try:
            lock_stat = lock.lstat()
            owner_stat = owner_path.lstat()
        except OSError:
            return
        if not _same_lock_snapshot(
            lock, _stat_identity(lock_stat), _stat_identity(owner_stat), token
        ):
            return
        quarantine = lock.with_name(f".{lock.name}.complete-{secrets.token_hex(8)}")
        try:
            os.rename(lock, quarantine)
        except OSError:
            return
        shutil.rmtree(quarantine, ignore_errors=True)
    finally:
        _release_recovery_guard(guard, guard_token)


def _recover_stale_lock(lock: Path, *, stale_seconds: int = LOCK_STALE_SECONDS) -> bool:
    try:
        lock_stat = lock.lstat()
    except FileNotFoundError:
        return True
    if lock.is_symlink() or _is_reparse(lock_stat) or not stat.S_ISDIR(lock_stat.st_mode):
        raise InstallError(f"install lock is not a real directory: {lock}")
    owner_path = lock / "owner.json"
    try:
        owner_stat = owner_path.lstat()
        if owner_path.is_symlink() or _is_reparse(owner_stat) or not stat.S_ISREG(owner_stat.st_mode):
            return False
        loaded = load_json(owner_path)
    except (InstallError, OSError):
        return False
    required = {"format_version", "pid", "host", "created", "token"}
    if (
        not isinstance(loaded, dict)
        or set(loaded) != required
        or loaded.get("format_version") != 1
        or not isinstance(loaded.get("pid"), int)
        or isinstance(loaded.get("pid"), bool)
        or loaded["pid"] <= 0
        or not isinstance(loaded.get("host"), str)
        or not loaded["host"]
        or not isinstance(loaded.get("created"), (int, float))
        or isinstance(loaded.get("created"), bool)
        or not isinstance(loaded.get("token"), str)
        or not loaded["token"]
    ):
        return False
    age = max(0.0, time.time() - float(loaded["created"]))
    if age <= stale_seconds:
        return False
    if loaded["host"] != socket.gethostname() or _pid_alive(loaded["pid"]):
        return False
    guard_claim = _acquire_recovery_guard(lock)
    if guard_claim is None:
        return False
    guard, guard_token = guard_claim
    try:
        if not _same_lock_snapshot(
            lock,
            _stat_identity(lock_stat),
            _stat_identity(owner_stat),
            loaded["token"],
        ):
            return False
        quarantine = lock.with_name(f".{lock.name}.stale-{secrets.token_hex(8)}")
        try:
            # os.rename never replaces an existing non-empty quarantine directory.
            os.rename(lock, quarantine)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        shutil.rmtree(quarantine, ignore_errors=True)
        return True
    finally:
        _release_recovery_guard(guard, guard_token)


@contextmanager
def install_lock(lock: Path) -> Iterator[None]:
    token = secrets.token_hex(16)
    for _ in range(3):
        try:
            lock.mkdir()
        except FileExistsError:
            if _recover_stale_lock(lock):
                continue
            raise InstallError(f"another install is in progress: {lock}")
        try:
            write_json(lock / "owner.json", {
                "format_version": 1,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created": time.time(),
                "token": token,
            })
        except Exception:
            shutil.rmtree(lock, ignore_errors=True)
            raise
        try:
            yield
        finally:
            _remove_owned_lock(lock, token)
        return
    raise InstallError(f"could not acquire install lock after stale-lock recovery: {lock}")


def _install_state_payload(
    manifest: dict[str, Any], args: argparse.Namespace, record: InstallRecord,
    installed_digest: str,
) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "name": manifest["name"],
        "source": _redact_source(record.source),
        "ref": record.ref,
        "digest": record.digest,
        "installedDigest": installed_digest,
        "version": record.version,
        "target": args.target,
        "installedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _read_install_state(path: Path) -> dict[str, Any] | None:
    raw = _read_config_bytes(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError(f"invalid installation state {path}: {exc}") from exc
    required = {
        "formatVersion", "name", "source", "ref", "digest", "installedDigest",
        "version", "target", "installedAt",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("formatVersion") != 1:
        raise InstallError(f"invalid installation state schema: {path}")
    if (
        not isinstance(value.get("name"), str)
        or not isinstance(value.get("source"), str)
        or (value.get("ref") is not None and not isinstance(value.get("ref"), str))
        or not isinstance(value.get("digest"), str)
        or not isinstance(value.get("installedDigest"), str)
        or (value.get("version") is not None and not isinstance(value.get("version"), str))
        or value.get("target") not in {"portable", "codex", "claude"}
        or not isinstance(value.get("installedAt"), str)
    ):
        raise InstallError(f"invalid installation state values: {path}")
    if (
        DIGEST_RE.fullmatch(value["digest"]) is None
        or DIGEST_RE.fullmatch(value["installedDigest"]) is None
    ):
        raise InstallError(f"invalid installation state digest: {path}")
    return value


def _state_matches(
    state: dict[str, Any] | None, name: str, args: argparse.Namespace, record: InstallRecord
) -> bool:
    return bool(state) and all((
        state.get("name") == name,
        state.get("source") == _redact_source(record.source),
        state.get("ref") == record.ref,
        state.get("digest") == record.digest,
        state.get("version") == record.version,
        state.get("target") == args.target,
    ))


def _temporary_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _remove_transaction_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _publish_installation(
    source_plugin: Path,
    loaded: LoadedPlugin,
    args: argparse.Namespace,
    record: InstallRecord | None,
    *,
    replace_existing: bool,
) -> Path:
    manifest = loaded.manifest
    layout = _layout(args, manifest["name"])
    parent, destination = layout.plugin_parent, layout.destination
    if os.name == "nt" and _utf16_units(str(destination)) > MAX_PORTABLE_RELATIVE_UNITS:
        raise InstallError(f"destination path exceeds the Windows-safe limit: {destination}")
    parent.mkdir(parents=True, exist_ok=True)
    layout.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = layout.lock_path
    temporary_files: list[Path] = []
    temp_root: Path | None = None
    with install_lock(lock):
        destination_exists = os.path.lexists(destination)
        if destination_exists:
            destination_stat = destination.lstat()
            if (
                destination.is_symlink()
                or _is_reparse(destination_stat)
                or not stat.S_ISDIR(destination_stat.st_mode)
            ):
                raise InstallError(f"installed plugin must be a real directory: {destination}")
        if destination_exists != replace_existing:
            if destination_exists:
                raise InstallError(f"destination already exists: {destination}")
            raise InstallError(f"plugin is not installed: {destination}")
        try:
            temp_root = Path(tempfile.mkdtemp(prefix=f".{manifest['name']}.", dir=parent))
            staged = temp_root / manifest["name"]
            project_plugin(source_plugin, loaded, args.target, staged)
            validate_projection(staged, manifest["name"], args.target)
            installed_digest = tree_digest(staged)
            marketplace = layout.marketplace_path
            marketplace_data = (
                marketplace_value(
                    marketplace, args.target, manifest, replace_existing=replace_existing
                ) if marketplace else None
            )
            marketplace_tmp = (
                _temporary_json(marketplace, marketplace_data)
                if marketplace is not None and marketplace_data is not None else None
            )
            if marketplace_tmp is not None:
                temporary_files.append(marketplace_tmp)
            state_tmp = (
                _temporary_json(
                    layout.state_path,
                    _install_state_payload(manifest, args, record, installed_digest),
                )
                if record is not None else None
            )
            if state_tmp is not None:
                temporary_files.append(state_tmp)

            token = secrets.token_hex(8)
            backup_destination = parent / f".{manifest['name']}.backup-{token}"
            backup_marketplace = (
                marketplace.with_name(f".{marketplace.name}.backup-{token}") if marketplace else None
            )
            backup_state = layout.state_path.with_name(
                f".{layout.state_path.name}.backup-{token}"
            )
            marketplace_existed = (
                marketplace is not None and os.path.lexists(marketplace)
            )
            state_existed = os.path.lexists(layout.state_path)
            moved_destination = False
            moved_marketplace = False
            moved_state = False
            published_destination = False
            published_marketplace = False
            published_state = False
            try:
                if replace_existing:
                    os.replace(destination, backup_destination)
                    moved_destination = True
                if marketplace_existed:
                    os.replace(marketplace, backup_marketplace)
                    moved_marketplace = True
                if state_existed:
                    os.replace(layout.state_path, backup_state)
                    moved_state = True
                os.replace(staged, destination)
                published_destination = True
                if marketplace is not None and marketplace_tmp is not None:
                    os.replace(marketplace_tmp, marketplace)
                    temporary_files.remove(marketplace_tmp)
                    published_marketplace = True
                if state_tmp is not None:
                    os.replace(state_tmp, layout.state_path)
                    temporary_files.remove(state_tmp)
                    published_state = True
            # Restore on all catchable in-process failures, including
            # KeyboardInterrupt/SystemExit. Filesystem state, rather than the
            # post-syscall flags alone, handles an interrupt immediately after
            # a successful rename.
            except BaseException as publish_error:
                rollback_errors: list[str] = []
                recovery = (
                    (
                        layout.state_path,
                        backup_state,
                        state_tmp,
                        state_existed,
                    ),
                    (
                        marketplace,
                        backup_marketplace,
                        marketplace_tmp,
                        marketplace_existed,
                    ),
                    (
                        destination,
                        backup_destination,
                        staged,
                        destination_exists,
                    ),
                )
                for original, backup, staged_path, existed_before in recovery:
                    if original is None or backup is None:
                        continue
                    try:
                        if os.path.lexists(backup):
                            if os.path.lexists(original):
                                _remove_transaction_path(original)
                            os.replace(backup, original)
                        elif (
                            not existed_before
                            and staged_path is not None
                            and not os.path.lexists(staged_path)
                            and os.path.lexists(original)
                        ):
                            _remove_transaction_path(original)
                    except OSError as exc:
                        rollback_errors.append(str(exc))
                if rollback_errors:
                    raise InstallError(
                        "installation failed and rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    ) from publish_error
                raise
            if moved_destination:
                shutil.rmtree(backup_destination, ignore_errors=True)
            if moved_marketplace and backup_marketplace is not None:
                try:
                    backup_marketplace.unlink()
                except OSError:
                    pass
            if moved_state:
                try:
                    backup_state.unlink()
                except OSError:
                    pass
            shutil.rmtree(temp_root, ignore_errors=True)
            temp_root = None
            return destination
        finally:
            for temporary in temporary_files:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)


def install(
    source_plugin: Path,
    loaded: LoadedPlugin,
    args: argparse.Namespace,
    record: InstallRecord | None = None,
) -> Path:
    return _publish_installation(
        source_plugin, loaded, args, record, replace_existing=False
    )


def update_installation(
    source_plugin: Path,
    loaded: LoadedPlugin,
    args: argparse.Namespace,
    record: InstallRecord,
) -> tuple[Path, bool]:
    layout = _layout(args, loaded.manifest["name"])
    if not os.path.lexists(layout.destination):
        raise InstallError(
            f"plugin is not installed; run install first: {layout.destination}"
        )
    state = _read_install_state(layout.state_path)
    if _state_matches(state, loaded.manifest["name"], args, record):
        validate_projection(layout.destination, loaded.manifest["name"], args.target)
        marketplace_current = True
        if layout.marketplace_path is not None:
            marketplace_current = False
            if os.path.lexists(layout.marketplace_path):
                expected_marketplace = marketplace_value(
                    layout.marketplace_path,
                    args.target,
                    loaded.manifest,
                    replace_existing=True,
                )
                marketplace_current = (
                    load_json(layout.marketplace_path) == expected_marketplace
                )
        if (
            tree_digest(layout.destination) == state.get("installedDigest")
            and marketplace_current
        ):
            return layout.destination, False
    destination = _publish_installation(
        source_plugin, loaded, args, record, replace_existing=True
    )
    return destination, True


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
    mutable = common.add_mutually_exclusive_group()
    mutable.add_argument("--allow-mutable-ref", action="store_true", default=None)
    mutable.add_argument("--no-allow-mutable-ref", action="store_false", dest="allow_mutable_ref")
    sub.add_parser("list", parents=[common], help="list catalogued plugins")
    for command, help_text in (
        ("install", "install a catalogued plugin"),
        ("update", "safely update an installed plugin to the approved release"),
    ):
        command_parser = sub.add_parser(command, parents=[common], help=help_text)
        command_parser.add_argument("name")
        command_parser.add_argument("--target", choices=("portable", "codex", "claude"))
        command_parser.add_argument("--agent-home")
        command_parser.add_argument("--scope", choices=("user", "project"))
        command_parser.add_argument("--dest", help="override the destination plugin parent")
    config_parser = sub.add_parser(
        "effective-config", parents=[common], help="print resolved settings and their provenance"
    )
    config_parser.add_argument("--target", choices=("portable", "codex", "claude"))
    config_parser.add_argument("--agent-home")
    config_parser.add_argument("--scope", choices=("user", "project"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        effective = resolve_effective_config(args)
        if args.command == "effective-config":
            print(json.dumps(effective_config_payload(effective), indent=2, sort_keys=True))
            return 0
        args.target = effective.target
        args.agent_home = effective.agent_home
        args.scope = effective.scope
        with materialize_effective_source(effective) as (root, actual_ref, release):
            entries = read_catalog(root)
            if release is not None:
                verify_release_catalog(root, entries, release)
            if args.command == "list":
                for entry in entries:
                    print(f"{entry['name']}\t{entry['content_sha256']}\t{entry['path']}")
            else:
                entry = select_entry(entries, args.name)
                with verify_entry(root, entry) as (plugin_root, loaded):
                    for warning in loaded.warnings:
                        print(f"warning: {warning}", file=sys.stderr)
                    record = InstallRecord(
                        effective.source,
                        actual_ref,
                        entry["content_sha256"],
                        release.version if release is not None else None,
                    )
                    if args.command == "update":
                        destination, changed = update_installation(
                            plugin_root, loaded, args, record
                        )
                    else:
                        destination = install(plugin_root, loaded, args, record)
                        changed = True
                    revision = actual_ref or "local checkout"
                    action = "updated" if args.command == "update" and changed else (
                        "already current" if args.command == "update" else "installed"
                    )
                    print(
                        f"{action} {loaded.manifest['name']} from {revision} to {destination}"
                    )
        return 0
    except (InstallError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
