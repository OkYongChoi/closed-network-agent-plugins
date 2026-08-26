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
import stat
import subprocess
import sys
import tempfile
import unicodedata
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
GIT_TIMEOUT_SECONDS = 60
MAX_PORTABLE_COMPONENT_UNITS = 255
MAX_PORTABLE_RELATIVE_UNITS = 240
RUNTIME_ARTIFACT_DIRS = {"__pycache__", ".pytest_cache"}
RUNTIME_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}
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


class PluginError(RuntimeError):
    """Expected validation or creation failure."""


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def validate_portable_component(value: str, *, label: str = "path component") -> None:
    """Apply the Windows-compatible subset used for all portable packages."""
    if not value or value in {".", ".."}:
        raise PluginError(f"invalid {label}: {value!r}")
    if value != unicodedata.normalize("NFC", value):
        raise PluginError(f"{label} is not Unicode NFC-normalized: {value!r}")
    compatibility = unicodedata.normalize("NFKC", value)
    if value[-1] in {".", " "} or compatibility[-1] in {".", " "}:
        raise PluginError(f"{label} ends with a dot or space: {value!r}")
    if any(
        ord(char) < 32 or char in WINDOWS_FORBIDDEN_CHARS
        for char in (*value, *compatibility)
    ):
        raise PluginError(f"{label} contains a Windows-reserved character: {value!r}")
    if compatibility.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        raise PluginError(f"{label} is a Windows device name: {value!r}")
    if _utf16_units(value) > MAX_PORTABLE_COMPONENT_UNITS or len(value.encode("utf-8")) > 255:
        raise PluginError(f"{label} is too long for a portable filesystem: {value!r}")


def validate_portable_relative(parts: tuple[str, ...], *, label: str = "relative path") -> None:
    for part in parts:
        validate_portable_component(part, label=label)
    rendered = "/".join(parts)
    if _utf16_units(rendered) > MAX_PORTABLE_RELATIVE_UNITS:
        raise PluginError(
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
                raise PluginError(f"cannot count links through a reparse point: {path}")
            count = int(information.nNumberOfLinks)
            if count < 1:
                raise OSError("Windows returned an invalid hard-link count")
            return count
        finally:
            close_handle(handle)
    except PluginError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise PluginError(f"cannot determine hard-link count safely: {path}") from exc


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
    raise PluginError(f"cannot determine hard-link count safely: {path}")


def _collect_tree(root: Path) -> list[tuple[Path, str, os.stat_result]]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise PluginError(f"cannot inspect source root {root}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink() or _is_reparse(root_stat):
        raise PluginError(f"expected a real directory: {root}")
    collected: list[tuple[Path, str, os.stat_result]] = []
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, parent_parts = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise PluginError(f"cannot inspect directory {directory}: {exc}") from exc
        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            parts = (*parent_parts, entry.name)
            validate_portable_relative(parts)
            relative = "/".join(parts)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PluginError(f"cannot inspect {relative}: {exc}") from exc
            if entry.is_symlink() or _is_reparse(entry_stat):
                raise PluginError(f"symlinks and junctions are not allowed: {relative}")
            collected.append((Path(entry.path), relative, entry_stat))
            if stat.S_ISDIR(entry_stat.st_mode):
                child_directories.append((Path(entry.path), parts))
            elif not stat.S_ISREG(entry_stat.st_mode):
                raise PluginError(f"unsupported filesystem entry: {relative}")
        stack.extend(reversed(child_directories))
    return collected


def normalize_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9.]+", "-", value.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise PluginError(f"invalid plugin name after normalization: {name!r}")
    validate_portable_component(name, label="plugin name")
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
    seen: dict[str, str] = {}
    file_count = 0
    total_bytes = 0
    for item, relative, item_stat in _collect_tree(root):
        relative_path = Path(relative)
        if (
            any(part in RUNTIME_ARTIFACT_DIRS for part in relative_path.parts)
            or item.suffix.lower() in RUNTIME_ARTIFACT_SUFFIXES
        ):
            raise PluginError(f"runtime artifacts are forbidden in canonical packages: {relative}")
        key = "/".join(_portable_key(part) for part in relative_path.parts)
        if key in seen and seen[key] != relative:
            raise PluginError(f"portable path collision: {seen[key]!r} and {relative!r}")
        seen[key] = relative
        if stat.S_ISDIR(item_stat.st_mode):
            continue
        if _file_link_count(item, item_stat) > 1:
            raise PluginError(f"hard-linked files are not allowed: {relative}")
        file_count += 1
        total_bytes += item_stat.st_size
        if file_count > max_files or total_bytes > max_bytes:
            raise PluginError("source tree exceeds safety limits")


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
    validate_portable_component(name, label="skill name")
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
    validate_portable_component(name, label="plugin name")
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
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginError(f"git command timed out after {GIT_TIMEOUT_SECONDS} seconds") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise PluginError(f"git command failed: {detail.strip()}") from exc
    return completed.stdout.strip()


def _resolve_real_relative(root: Path, value: str, *, label: str) -> Path:
    if not value or value.startswith("/") or "\\" in value:
        raise PluginError(f"{label} must use a portable repository-relative path")
    parts = tuple(value.split("/"))
    validate_portable_relative(parts, label=label)
    current = root
    for part in parts:
        current = current / part
        try:
            item_stat = current.lstat()
        except OSError as exc:
            raise PluginError(f"cannot inspect {label} component {part!r}: {exc}") from exc
        if current.is_symlink() or _is_reparse(item_stat):
            raise PluginError(f"{label} contains a symlink or junction: {value!r}")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PluginError(f"{label} escapes source root") from exc
    return current


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
    if not skill_path or skill_path == ".":
        candidate = root
    else:
        candidate = _resolve_real_relative(root, skill_path, label="--path")
    validate_skill(candidate)
    return candidate, actual_ref


def _copy_tree(source: Path, target: Path) -> None:
    inspect_tree(source)
    shutil.copytree(source, target, symlinks=False)


def create_plugin(args: argparse.Namespace) -> Path:
    name = normalize_name(args.name)
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    if os.name == "nt" and _utf16_units(str(target)) > MAX_PORTABLE_RELATIVE_UNITS:
        raise PluginError(f"destination path exceeds the Windows-safe limit: {target}")
    if os.path.lexists(target):
        raise PluginError(f"destination already exists: {target}")
    tmp = Path(tempfile.mkdtemp(prefix=f".{name}.create-", dir=output))
    try:
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
        try:
            os.replace(canonical, target)
        except Exception as exc:
            raise PluginError(f"failed to publish plugin: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-plugin", help="create a canonical plugin package")
    create.add_argument("name")
    create.add_argument("--output", required=True, help="parent directory for the canonical plugin")
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
