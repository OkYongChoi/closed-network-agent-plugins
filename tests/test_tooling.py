from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


creator = load("creator", ROOT / "plugins/plugin-creator/skills/plugin-creator/scripts/create_plugin.py")
installer = load("installer", ROOT / "plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py")


def make_plugin(root: Path, name: str = "boundary-plugin", **extra_manifest):
    plugin = root / name
    plugin.mkdir()
    manifest = {"$schema": installer.PLUGIN_SCHEMA, "name": name, **extra_manifest}
    (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return plugin


class ToolingTests(unittest.TestCase):
    def test_creator_builds_canonical_and_isolated_projections(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(
                name="Demo Plugin", output=temp, adapters="codex,claude",
                version="0.1.0", description="Demo", author="Team", license="Apache-2.0",
                import_skill=None, ref=None, path=None, import_license="Apache-2.0",
            )
            canonical = creator.create_plugin(args)
            self.assertEqual(creator.validate_plugin(canonical)["name"], "demo-plugin")
            self.assertFalse((canonical / ".codex-plugin").exists())
            self.assertFalse((canonical / ".claude-plugin").exists())
            self.assertTrue((Path(temp) / ".staging/codex/plugins/demo-plugin/.codex-plugin/plugin.json").is_file())
            self.assertTrue((Path(temp) / ".staging/claude/plugins/demo-plugin/.claude-plugin/plugin.json").is_file())

    def test_creator_rejects_invalid_frontmatter_and_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "bad"
            (root / "skills/bad").mkdir(parents=True)
            (root / "plugin.json").write_text(json.dumps({"$schema": creator.PLUGIN_SCHEMA, "name": "bad"}))
            (root / "skills/bad/SKILL.md").write_text("name: bad\n")
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)
            shutil.rmtree(root / "skills")
            (root / "mcp.json").write_text(json.dumps({"$schema": "bad", "mcpServers": {}}))
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)

    def test_authoring_validation_remains_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp), futureField=True)
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)
            manifest = json.loads((root / "plugin.json").read_text())
            manifest.pop("futureField")
            manifest["extensions"] = "invalid"
            (root / "plugin.json").write_text(json.dumps(manifest))
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)

    def test_runtime_ignores_unknown_fields_and_non_object_extensions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp), futureField={"value": 1}, extensions="invalid")
            loaded = installer.load_plugin(root)
            self.assertEqual(loaded.manifest["name"], "boundary-plugin")
            self.assertNotIn("futureField", loaded.manifest)
            self.assertNotIn("extensions", loaded.manifest)
            self.assertEqual(len(loaded.warnings), 2)
            manifest = json.loads((root / "plugin.json").read_text())
            manifest.pop("futureField")
            manifest["extensions"] = {"com.example.future": "opaque-to-this-client"}
            (root / "plugin.json").write_text(json.dumps(manifest))
            loaded = installer.load_plugin(root)
            self.assertEqual(loaded.manifest["extensions"]["com.example.future"], "opaque-to-this-client")

    def test_invalid_skill_is_skipped_without_invalidating_valid_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            (root / "skills/good").mkdir(parents=True)
            (root / "skills/good/SKILL.md").write_text(
                "---\nname: good\ndescription: A valid skill.\n---\n# Good\n"
            )
            (root / "skills/bad").mkdir()
            (root / "skills/bad/SKILL.md").write_text("name: bad\n")
            loaded = installer.load_plugin(root)
            self.assertEqual(loaded.skills, ("good",))
            self.assertTrue(any("skipped invalid skill bad" in item for item in loaded.warnings))
            (root / "skills/period.name").mkdir()
            (root / "skills/period.name/SKILL.md").write_text(
                "---\nname: period.name\ndescription: Invalid Agent Skills name.\n---\n"
            )
            loaded = installer.load_plugin(root)
            self.assertNotIn("period.name", loaded.skills)

    def test_invalid_mcp_document_disables_only_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            (root / "skills/good").mkdir(parents=True)
            (root / "skills/good/SKILL.md").write_text(
                "---\nname: good\ndescription: A valid skill.\n---\n# Good\n"
            )
            (root / "mcp.json").write_text(json.dumps({"$schema": "wrong", "mcpServers": {}}))
            loaded = installer.load_plugin(root)
            self.assertEqual(loaded.skills, ("good",))
            self.assertIsNone(loaded.mcp)
            self.assertTrue(any("disabled MCP" in item for item in loaded.warnings))

    def test_invalid_mcp_servers_are_individually_skipped_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp), futureField=True, extensions="invalid")
            (root / "mcp.json").write_text(json.dumps({
                "$schema": installer.MCP_SCHEMA,
                "mcpServers": {
                    "good": {"type": "stdio", "command": "python3", "cwd": "${PLUGIN_ROOT}"},
                    "bad-command": {"type": "stdio", "command": "../bin/server"},
                    "bad-cwd": {"type": "stdio", "command": "python3", "cwd": "${PLUGIN_ROOT}/../escape"},
                    "embedded-secret": {
                        "type": "streamable-http", "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer visible-secret"},
                    },
                },
            }))
            loaded = installer.load_plugin(root)
            self.assertEqual(set(loaded.mcp["mcpServers"]), {"good"})
            self.assertGreaterEqual(sum("skipped invalid MCP server" in item for item in loaded.warnings), 3)
            destination_parent = Path(temp) / "installed"
            args = argparse.Namespace(dest=str(destination_parent), agent_home=None, scope="user", target="portable")
            destination = installer.install(root, loaded, args)
            installed_manifest = json.loads((destination / "plugin.json").read_text())
            installed_mcp = json.loads((destination / "mcp.json").read_text())
            self.assertNotIn("futureField", installed_manifest)
            self.assertNotIn("extensions", installed_manifest)
            self.assertEqual(set(installed_mcp["mcpServers"]), {"good"})

    def test_creator_rejects_mcp_traversal_and_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            (root / "mcp.json").write_text(json.dumps({
                "$schema": installer.MCP_SCHEMA,
                "mcpServers": {
                    "escape": {"type": "stdio", "command": "./bin/server", "cwd": "${PLUGIN_DATA}/../escape"}
                },
            }))
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)
            (root / "mcp.json").write_text(json.dumps({
                "$schema": installer.MCP_SCHEMA,
                "mcpServers": {
                    "secret": {
                        "type": "streamable-http", "url": "https://example.com/mcp",
                        "headers": {"X-Api-Key": "visible-secret"},
                    }
                },
            }))
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)

    def test_native_mcp_projection_maps_streamable_http(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            (root / "mcp.json").write_text(json.dumps({
                "$schema": installer.MCP_SCHEMA,
                "mcpServers": {
                    "remote": {"type": "streamable-http", "url": "https://example.com/mcp"}
                },
            }))
            loaded = installer.load_plugin(root)
            for target in ("codex", "claude"):
                output = Path(temp) / f"projected-{target}"
                installer.project_plugin(root, loaded, target, output)
                native = json.loads((output / ".mcp.json").read_text())
                self.assertEqual(native["mcpServers"]["remote"]["type"], "http")
            codex_manifest = json.loads((Path(temp) / "projected-codex/.codex-plugin/plugin.json").read_text())
            self.assertEqual(codex_manifest["mcpServers"], "./.mcp.json")

    def test_minimal_portable_manifest_gets_identical_native_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp), author={"email": "   ", "url": ""})
            original = json.loads((root / "plugin.json").read_text())
            creator.build_projection(root, "codex", Path(temp) / "creator-stage")
            creator.build_projection(root, "claude", Path(temp) / "creator-stage")
            loaded = installer.load_plugin(root)
            installer.project_plugin(root, loaded, "codex", Path(temp) / "installer-codex")
            installer.project_plugin(root, loaded, "claude", Path(temp) / "installer-claude")
            manifests = [
                json.loads((Path(temp) / "creator-stage/codex/plugins/boundary-plugin/.codex-plugin/plugin.json").read_text()),
                json.loads((Path(temp) / "creator-stage/claude/plugins/boundary-plugin/.claude-plugin/plugin.json").read_text()),
                json.loads((Path(temp) / "installer-codex/.codex-plugin/plugin.json").read_text()),
                json.loads((Path(temp) / "installer-claude/.claude-plugin/plugin.json").read_text()),
            ]
            for manifest in manifests:
                self.assertEqual(manifest["version"], "0.1.0")
                self.assertEqual(manifest["description"], "Portable projection for boundary-plugin.")
                self.assertEqual(manifest["author"], {"name": "Unknown"})
            self.assertEqual(json.loads((root / "plugin.json").read_text()), original)

    def test_native_author_url_requires_well_formed_https_origin(self):
        invalid = (
            "https://",
            "https://example.com:not-a-port",
            "https://user@example.com/profile",
            "https://example.com/profile#fragment",
            "https://bad host.example/profile",
        )
        for value in invalid:
            canonical = {
                "$schema": installer.PLUGIN_SCHEMA,
                "name": "url-test",
                "author": {"name": "Test", "url": value},
            }
            before = json.loads(json.dumps(canonical))
            self.assertNotIn("url", creator._native_manifest(canonical)["author"])
            self.assertNotIn("url", installer._native_manifest(canonical)["author"])
            self.assertEqual(canonical, before)
        valid = {
            "$schema": installer.PLUGIN_SCHEMA,
            "name": "url-test",
            "author": {"name": "Test", "url": "https://docs.example.com:8443/profile"},
        }
        self.assertEqual(creator._native_manifest(valid)["author"]["url"], valid["author"]["url"])
        self.assertEqual(installer._native_manifest(valid)["author"]["url"], valid["author"]["url"])

    def test_claude_dest_must_be_marketplace_plugins_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = argparse.Namespace(
                dest=str(root / "arbitrary"), agent_home=str(root / ".claude"),
                scope="user", target="claude",
            )
            with self.assertRaises(installer.InstallError):
                installer._layout(invalid, "demo")
            valid = argparse.Namespace(
                dest=str(root / "marketplace/plugins"), agent_home=str(root / ".claude"),
                scope="user", target="claude",
            )
            layout = installer._layout(valid, "demo")
            self.assertEqual(layout.destination, (root / "marketplace/plugins/demo").resolve())
            self.assertEqual(
                layout.marketplace_path,
                (root / "marketplace/.claude-plugin/marketplace.json").resolve(),
            )

    def test_local_ref_materializes_committed_tree_and_unpinned_provenance_is_null(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "source"
            skill = repository / "skills/demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Committed version.\n---\n")
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
                check=True,
            )
            commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Dirty version.\n---\n")
            (skill / "UNTRACKED").write_text("must not be imported")

            pinned_output = Path(temp) / "pinned"
            pinned_args = argparse.Namespace(
                name="pinned", output=str(pinned_output), adapters="", version="0.1.0",
                description="Pinned", author="Team", license="Apache-2.0",
                import_skill=str(repository), ref=commit, path="skills/demo", import_license="Apache-2.0",
            )
            pinned = creator.create_plugin(pinned_args)
            self.assertIn("Committed version", (pinned / "skills/demo/SKILL.md").read_text())
            self.assertFalse((pinned / "skills/demo/UNTRACKED").exists())
            provenance = json.loads((pinned / "VENDORED_SKILLS.json").read_text())
            self.assertEqual(provenance["skills"][0]["source_commit"], commit)

            unpinned_output = Path(temp) / "unpinned"
            unpinned_args = argparse.Namespace(**{**vars(pinned_args), "name": "unpinned", "output": str(unpinned_output), "ref": None})
            unpinned = creator.create_plugin(unpinned_args)
            provenance = json.loads((unpinned / "VENDORED_SKILLS.json").read_text())
            self.assertIsNone(provenance["skills"][0]["source_commit"])

            with installer.materialize_source(str(repository), commit, False) as (materialized, actual):
                self.assertEqual(actual, commit)
                self.assertIn("Committed version", (materialized / "skills/demo/SKILL.md").read_text())
                self.assertFalse((materialized / "skills/demo/UNTRACKED").exists())

    def test_marketplace_lock_is_shared_across_plugin_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = make_plugin(root, "first-plugin")
            second = make_plugin(root, "second-plugin")
            first_loaded = installer.load_plugin(first)
            second_loaded = installer.load_plugin(second)
            agent_home = root / "home/.agents"
            args = argparse.Namespace(dest=None, agent_home=str(agent_home), scope="user", target="codex")
            entered = threading.Event()
            release = threading.Event()
            original = installer.project_plugin
            outcomes: list[object] = []

            def blocking_project(source, loaded, target, output):
                if loaded.manifest["name"] == "first-plugin":
                    entered.set()
                    self.assertTrue(release.wait(5))
                return original(source, loaded, target, output)

            def run_first():
                try:
                    outcomes.append(installer.install(first, first_loaded, args))
                except Exception as exc:
                    outcomes.append(exc)

            with mock.patch.object(installer, "project_plugin", side_effect=blocking_project):
                thread = threading.Thread(target=run_first)
                thread.start()
                self.assertTrue(entered.wait(5))
                with self.assertRaises(installer.InstallError):
                    installer.install(second, second_loaded, args)
                release.set()
                thread.join(5)
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], Path)
            marketplace = json.loads((agent_home / "plugins/marketplace.json").read_text())
            self.assertEqual([item["name"] for item in marketplace["plugins"]], ["first-plugin"])

    def test_symlink_and_catalog_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            (target / "file").write_text("x")
            (root / "tree").mkdir()
            (root / "tree/link").symlink_to(target / "file")
            with self.assertRaises(installer.InstallError):
                installer.inspect_tree(root / "tree")
            (root / "catalog.json").write_text(json.dumps({
                "format_version": 1,
                "plugins": [{"name": "escape", "path": "../escape", "content_sha256": "0" * 64}],
            }))
            with self.assertRaises(installer.InstallError):
                installer.read_catalog(root)

    def test_runtime_artifacts_fail_closed_and_clean_digest_is_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            clean_creator_digest = creator.tree_digest(root)
            clean_installer_digest = installer.tree_digest(root)
            self.assertEqual(clean_creator_digest, clean_installer_digest)
            cache = root / "scripts/__pycache__"
            cache.mkdir(parents=True)
            (cache / "generated.cpython-313.pyc").write_bytes(b"runtime-cache")
            with self.assertRaises(creator.PluginError):
                creator.tree_digest(root)
            with self.assertRaises(creator.PluginError):
                creator.validate_plugin(root)
            with self.assertRaises(installer.InstallError):
                installer.tree_digest(root)
            with self.assertRaises(installer.InstallError):
                installer.load_plugin(root)
            shutil.rmtree(cache)
            (root / "scripts/generated.pyo").write_bytes(b"optimized-cache")
            with self.assertRaises(creator.PluginError):
                creator.tree_digest(root)
            with self.assertRaises(installer.InstallError):
                installer.tree_digest(root)
            (root / "scripts/generated.pyo").unlink()
            self.assertEqual(creator.tree_digest(root), clean_creator_digest)
            self.assertEqual(installer.tree_digest(root), clean_installer_digest)

    def test_digest_mismatch_blocks_install(self):
        catalog = json.loads((ROOT / "catalog.json").read_text())
        entry = next(item for item in catalog["plugins"] if item["name"] == "engineering-starter").copy()
        entry["content_sha256"] = "0" * 64
        with self.assertRaises(installer.InstallError):
            with installer.verify_entry(ROOT, entry):
                pass

    def test_verified_snapshot_closes_source_mutation_window(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source_root = base / "source"
            source_root.mkdir()
            source = make_plugin(source_root)
            (source / "payload.txt").write_text("verified-bytes")
            entry = {
                "name": "boundary-plugin",
                "path": "boundary-plugin",
                "content_sha256": installer.tree_digest(source),
            }
            with installer.verify_entry(base / "source", entry) as (snapshot, loaded):
                (source / "payload.txt").write_text("mutated-after-verification")
                args = argparse.Namespace(
                    dest=str(base / "installed"), agent_home=None,
                    scope="user", target="portable",
                )
                destination = installer.install(snapshot, loaded, args)
            self.assertEqual((destination / "payload.txt").read_text(), "verified-bytes")

    def test_all_install_targets_and_no_runtime_cross_fetch(self):
        entries = installer.read_catalog(ROOT)
        entry = installer.select_entry(entries, "engineering-starter")
        with installer.verify_entry(ROOT, entry) as (plugin_root, manifest):
            with tempfile.TemporaryDirectory() as temp:
                for target in ("portable", "codex", "claude"):
                    target_root = Path(temp) / target
                    if target == "portable":
                        args = argparse.Namespace(dest=str(target_root / "plugins"), agent_home=None, scope="user", target=target)
                    else:
                        args = argparse.Namespace(
                            dest=None,
                            agent_home=str(target_root / (".agents" if target == "codex" else ".claude")),
                            scope="user",
                            target=target,
                        )
                    with mock.patch.object(installer, "run_git", side_effect=AssertionError("unexpected network/git fetch")):
                        destination = installer.install(plugin_root, manifest, args)
                    self.assertTrue((destination / "plugin.json").is_file())
                    if target != "portable":
                        self.assertTrue((destination / f".{target}-plugin/plugin.json").is_file())
                    if target == "codex":
                        self.assertEqual(destination, (target_root / "plugins/engineering-starter").resolve())
                        self.assertTrue((target_root / ".agents/plugins/marketplace.json").is_file())
                    elif target == "claude":
                        marketplace_root = target_root / ".claude/plugins/marketplaces/okyongchoi-portable"
                        self.assertEqual(destination, (marketplace_root / "plugins/engineering-starter").resolve())
                        self.assertTrue((marketplace_root / ".claude-plugin/marketplace.json").is_file())

    def test_invalid_marketplace_leaves_no_partial_install(self):
        entries = installer.read_catalog(ROOT)
        with installer.verify_entry(ROOT, installer.select_entry(entries, "engineering-starter")) as (plugin_root, manifest):
            with tempfile.TemporaryDirectory() as temp:
                parent = Path(temp)
                marketplace = parent / ".agents/plugins/marketplace.json"
                marketplace.parent.mkdir(parents=True)
                marketplace.write_text("[]")
                args = argparse.Namespace(dest=None, agent_home=str(parent / ".agents"), scope="user", target="codex")
                with self.assertRaises(installer.InstallError):
                    installer.install(plugin_root, manifest, args)
                self.assertFalse((parent / "plugins/engineering-starter").exists())

    def test_concurrent_lock_blocks_install(self):
        entries = installer.read_catalog(ROOT)
        with installer.verify_entry(ROOT, installer.select_entry(entries, "engineering-starter")) as (plugin_root, manifest):
            with tempfile.TemporaryDirectory() as temp:
                parent = Path(temp)
                (parent / ".engineering-starter.install.lock").mkdir()
                args = argparse.Namespace(dest=str(parent), agent_home=None, scope="user", target="portable")
                with self.assertRaises(installer.InstallError):
                    installer.install(plugin_root, manifest, args)

    def test_source_precedence(self):
        with mock.patch.dict(os.environ, {"AGENT_PLUGINS_SOURCE": "/internal/mirror"}):
            self.assertEqual(installer.choose_source(None), "/internal/mirror")
            self.assertEqual(installer.choose_source("/explicit"), "/explicit")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Path(installer.choose_source(None)), ROOT)

    def test_remote_source_requires_full_sha(self):
        with self.assertRaises(installer.InstallError):
            with installer.materialize_source("https://example.invalid/plugins.git", None, False):
                pass
        with self.assertRaises(installer.InstallError):
            with installer.materialize_source("https://example.invalid/plugins.git", "main", False):
                pass


if __name__ == "__main__":
    unittest.main()
