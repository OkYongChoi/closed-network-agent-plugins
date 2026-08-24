#!/usr/bin/env python3
"""Strictly validate authoring structure, provenance, vendored files, and catalog."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VENDOR_HASHES = {
    "schemas/1.0.0/plugin.schema.json": "0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883",
    "schemas/1.0.0/mcp.schema.json": "6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb",
    "LICENSES/Apache-2.0.txt": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
}


def _creator():
    path = ROOT / "plugins/plugin-creator/skills/plugin-creator/scripts/create_plugin.py"
    spec = importlib.util.spec_from_file_location("portable_plugin_creator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin creator")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def validate() -> list[str]:
    errors: list[str] = []
    creator = _creator()
    for relative, expected in EXPECTED_VENDOR_HASHES.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"vendored hash mismatch: {relative}: {actual}")
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog.get("plugins", [])
    catalog_names = {entry.get("name") for entry in entries}
    directory_names = {path.name for path in (ROOT / "plugins").iterdir() if path.is_dir()}
    if catalog_names != directory_names:
        errors.append(f"catalog/directory mismatch: {catalog_names} != {directory_names}")
    for entry in entries:
        path = ROOT / entry["path"]
        try:
            manifest = creator.validate_plugin(path)
            if manifest["name"] != entry["name"]:
                errors.append(f"manifest mismatch: {entry['name']}")
            actual = creator.tree_digest(path)
            if actual != entry["content_sha256"]:
                errors.append(f"catalog digest mismatch: {entry['name']}: {actual}")
        except Exception as exc:
            errors.append(f"invalid plugin {entry.get('name')}: {exc}")
    provenance_path = ROOT / "plugins/engineering-starter/VENDORED_SKILLS.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))["skills"][0]
    source_commit = provenance["source_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        errors.append("repo-summary source_commit must be a full SHA")
    actual_skill = creator.tree_digest(ROOT / "plugins/engineering-starter/skills/repo-summary")
    if actual_skill != provenance["tree_sha256"]:
        errors.append(f"repo-summary provenance digest mismatch: {actual_skill}")
    return errors


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("error: validate_repo.py accepts no arguments", file=sys.stderr)
        return 2
    errors = validate()
    if errors:
        print("\n".join(f"error: {item}" for item in errors), file=sys.stderr)
        return 1
    print("strict repository authoring validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
