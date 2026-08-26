#!/usr/bin/env python3
"""Refresh deterministic plugin tree digests in catalog.json."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
PLUGINS = ROOT / "plugins"


def _load_creator():
    path = ROOT / "plugins/plugin-creator/skills/plugin-creator/scripts/create_plugin.py"
    spec = importlib.util.spec_from_file_location("catalog_plugin_creator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin creator: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


CREATOR = _load_creator()


def render_catalog() -> bytes:
    try:
        current = json.loads(CATALOG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    if not isinstance(current, dict):
        raise ValueError("catalog.json must contain a JSON object")
    repository = current.get(
        "repository", "https://github.com/OkYongChoi/air-gapped-agent-plugins.git"
    )

    items: list[dict[str, str]] = []
    for plugin in sorted(PLUGINS.iterdir(), key=lambda path: path.name):
        if plugin.is_symlink():
            raise ValueError(f"plugin directory must not be a symlink: {plugin.name}")
        if not plugin.is_dir():
            continue
        manifest = CREATOR.validate_plugin(plugin)
        name = manifest["name"]
        if name != plugin.name:
            raise ValueError(
                f"plugin manifest name {name!r} does not match directory {plugin.name!r}"
            )
        items.append(
            {
                "name": name,
                "path": f"plugins/{name}",
                "content_sha256": CREATOR.tree_digest(plugin),
            }
        )

    output = {
        "format_version": 1,
        "repository": repository,
        "plugins": items,
    }
    return (json.dumps(output, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = render_catalog()
        if args.check:
            try:
                actual = CATALOG.read_bytes()
            except FileNotFoundError:
                actual = b""
            if actual != expected:
                print(
                    "catalog.json is stale; run scripts/refresh_catalog.py",
                    file=sys.stderr,
                )
                return 1
        else:
            CATALOG.write_bytes(expected)
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, CREATOR.PluginError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
