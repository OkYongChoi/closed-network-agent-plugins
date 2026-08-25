from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
repo_summary = load(
    "repo_summary",
    ROOT / "plugins/engineering-starter/skills/repo-summary/scripts/repo_summary.py",
)


def make_plugin(root: Path, name: str = "boundary-plugin", **extra_manifest):
    plugin = root / name
    plugin.mkdir()
    manifest = {"$schema": installer.PLUGIN_SCHEMA, "name": name, **extra_manifest}
    (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return plugin


class ToolingTests(unittest.TestCase):
    def test_gitlab_uses_platform_python_commands(self):
        pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
        linux, windows = pipeline.split("validate:windows:", 1)
        self.assertIn("python3 -B scripts/validate_repo.py", linux)
        self.assertNotIn("python -B scripts/validate_repo.py", linux)
        self.assertIn("python -B scripts/validate_repo.py", windows)

    @unittest.skipUnless(os.name == "nt", "Windows launchers require Windows")
    def test_windows_cmd_and_powershell_entrypoints(self):
        subprocess.run(
            ["cmd", "/c", str(ROOT / "bin/plugin-installer.cmd"), "--help"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh is None:
            self.skipTest("PowerShell is unavailable")
        subprocess.run(
            [pwsh, "-NoProfile", "-File", str(ROOT / "bin/create-plugin.ps1"), "--help"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def test_repo_summary_detects_windows_and_dotnet_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in (
                "Demo.sln", "Demo.csproj", "Directory.Build.props", "build.gradle", "gradlew.bat",
                "bootstrap.ps1", "build.cmd", "legacy.bat",
            ):
                (root / name).write_text("", encoding="utf-8")
            data = repo_summary.summarize(root, 100, 1024 * 1024)
            self.assertTrue({"Demo.sln", "Demo.csproj", "Directory.Build.props", "gradlew.bat"} <= set(data["manifests"]))
            self.assertTrue({
                "dotnet build", "dotnet test", ".\\gradlew.bat build", ".\\gradlew.bat test",
                "pwsh -File ./bootstrap.ps1", ".\\build.cmd", ".\\legacy.bat",
            } <= set(data["commands"]))
            self.assertEqual(data["languages"]["PowerShell"], 1)
            self.assertEqual(data["languages"]["Windows Batch"], 3)
        self.assertTrue(repo_summary._is_windows_reparse_point(
            argparse.Namespace(st_file_attributes=repo_summary.FILE_ATTRIBUTE_REPARSE_POINT)
        ))

    def test_windows_portable_names_and_paths_are_rejected_on_every_host(self):
        invalid_components = (
            "CON", "con.txt", "AUX.json", "NUL", "COM1.log", "LPT9",
            "has:ads", "has*wildcard", "trailing.", "trailing ", "e\u0301",
            "\uff23\uff2f\uff2e", "\uff0f",
        )
        for value in invalid_components:
            with self.subTest(value=value):
                with self.assertRaises(creator.PluginError):
                    creator.validate_portable_component(value)
                with self.assertRaises(installer.InstallError):
                    installer.validate_portable_component(value)
        with self.assertRaises(creator.PluginError):
            creator.normalize_name("CON")

    def test_zero_link_count_refreshes_and_windows_fallback_is_fail_closed(self):
        incomplete = argparse.Namespace(st_nlink=0)
        refreshed = argparse.Namespace(st_nlink=1)
        path = Path("normal-file")
        with mock.patch.object(creator.os, "stat", return_value=refreshed):
            self.assertEqual(creator._file_link_count(path, incomplete), 1)
        with mock.patch.object(installer.os, "stat", return_value=refreshed):
            self.assertEqual(installer._file_link_count(path, incomplete), 1)

        still_incomplete = argparse.Namespace(st_nlink=0)
        with mock.patch.object(creator.os, "stat", return_value=still_incomplete), mock.patch.object(
            creator, "_windows_file_link_count", return_value=2
        ) as creator_fallback:
            self.assertEqual(creator._file_link_count(path, incomplete, platform="nt"), 2)
            creator_fallback.assert_called_once_with(path)
        with mock.patch.object(installer.os, "stat", return_value=still_incomplete), mock.patch.object(
            installer, "_windows_file_link_count", return_value=2
        ) as installer_fallback:
            self.assertEqual(installer._file_link_count(path, incomplete, platform="nt"), 2)
            installer_fallback.assert_called_once_with(path)

    def test_real_hard_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            original = root / "payload.txt"
            duplicate = root / "payload-copy.txt"
            original.write_text("payload", encoding="utf-8")
            try:
                os.link(original, duplicate)
            except OSError as exc:
                self.skipTest(f"hard-link creation is unavailable: {exc}")
            with self.assertRaisesRegex(creator.PluginError, "hard-linked"):
                creator.inspect_tree(root)
            with self.assertRaisesRegex(installer.InstallError, "hard-linked"):
                installer.inspect_tree(root)

    @unittest.skipIf(os.name == "nt", "Windows cannot create names that this negative test exercises")
    def test_package_tree_rejects_windows_aliases_unicode_collisions_and_long_paths(self):
        for relative in ("CON.txt", "payload:ads", "trailing.", "e\u0301"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = make_plugin(Path(temp))
                (root / relative).write_text("unsafe", encoding="utf-8")
                with self.assertRaises(creator.PluginError):
                    creator.inspect_tree(root)
                with self.assertRaises(installer.InstallError):
                    installer.inspect_tree(root)
        with tempfile.TemporaryDirectory() as temp:
            root = make_plugin(Path(temp))
            (root / "1").write_text("one", encoding="utf-8")
            (root / "\u2460").write_text("two", encoding="utf-8")
            with self.assertRaises(creator.PluginError):
                creator.inspect_tree(root)
            with self.assertRaises(installer.InstallError):
                installer.inspect_tree(root)
        with self.assertRaises(creator.PluginError):
            creator.validate_portable_relative(("x" * 121, "y" * 120))
        with self.assertRaises(installer.InstallError):
            installer.validate_portable_relative(("x" * 121, "y" * 120))

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

    def test_creator_publication_rolls_back_after_adapter_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            args = argparse.Namespace(
                name="rollback-plugin", output=temp, adapters="codex,claude",
                version="0.1.0", description="Demo", author="Team", license="Apache-2.0",
                import_skill=None, ref=None, path=None, import_license="Apache-2.0",
            )
            actual_replace = creator.os.replace
            calls = 0

            def fail_second_publish(source, destination):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("simulated adapter publication failure")
                return actual_replace(source, destination)

            with mock.patch.object(creator.os, "replace", side_effect=fail_second_publish):
                with self.assertRaises(creator.PluginError):
                    creator.create_plugin(args)
            self.assertFalse((output / "rollback-plugin").exists())
            self.assertFalse((output / ".staging/codex").exists())
            self.assertFalse((output / ".staging/claude").exists())
            self.assertFalse(any(path.name.startswith(".rollback-plugin.create-") for path in output.iterdir()))

    def test_creator_rejects_symlinked_projection_staging_root(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / "output"
            outside = base / "outside"
            output.mkdir()
            outside.mkdir()
            try:
                (output / ".staging").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation is unavailable: {exc}")
            args = argparse.Namespace(
                name="safe-plugin", output=str(output), adapters="codex",
                version="0.1.0", description="Demo", author="Team", license="Apache-2.0",
                import_skill=None, ref=None, path=None, import_license="Apache-2.0",
            )
            with self.assertRaises(creator.PluginError):
                creator.create_plugin(args)
            self.assertFalse((output / "safe-plugin").exists())
            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "junction semantics require Windows")
    def test_creator_rejects_junctioned_projection_staging_root_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / "output"
            outside = base / "outside"
            output.mkdir()
            outside.mkdir()
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(output / ".staging"), str(outside)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {result.stdout.strip()}")
            args = argparse.Namespace(
                name="safe-plugin", output=str(output), adapters="codex",
                version="0.1.0", description="Demo", author="Team", license="Apache-2.0",
                import_skill=None, ref=None, path=None, import_license="Apache-2.0",
            )
            with self.assertRaises(creator.PluginError):
                creator.create_plugin(args)
            self.assertFalse((output / "safe-plugin").exists())
            self.assertEqual(list(outside.iterdir()), [])

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

    def test_catalog_file_and_path_component_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root / "external.json"
            external.write_text(json.dumps({"format_version": 1, "plugins": []}), encoding="utf-8")
            try:
                (root / "catalog.json").symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(installer.InstallError):
                installer.read_catalog(root)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            plugin = make_plugin(outside, "linked-plugin")
            digest = installer.tree_digest(plugin)
            try:
                (root / "plugins").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation is unavailable: {exc}")
            (root / "catalog.json").write_text(json.dumps({
                "format_version": 1,
                "plugins": [{
                    "name": "linked-plugin", "path": "plugins/linked-plugin",
                    "content_sha256": digest,
                }],
            }), encoding="utf-8")
            summary = repo_summary.summarize(root, 100, 1024 * 1024)
            self.assertIn("link/reparse directory: plugins", summary["risks"])
            with self.assertRaises(installer.InstallError):
                installer.read_catalog(root)

    @unittest.skipUnless(os.name == "nt", "junction semantics require Windows")
    def test_catalog_junction_is_rejected_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            plugin = make_plugin(outside, "junction-plugin")
            link = root / "plugins"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {result.stdout.strip()}")
            (root / "catalog.json").write_text(json.dumps({
                "format_version": 1,
                "plugins": [{
                    "name": "junction-plugin", "path": "plugins/junction-plugin",
                    "content_sha256": installer.tree_digest(plugin),
                }],
            }), encoding="utf-8")
            summary = repo_summary.summarize(root, 100, 1024 * 1024)
            self.assertIn("link/reparse directory: plugins", summary["risks"])
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

    def test_stale_lock_recovers_but_old_live_lock_does_not(self):
        entries = installer.read_catalog(ROOT)
        entry = installer.select_entry(entries, "engineering-starter")
        with installer.verify_entry(ROOT, entry) as (plugin_root, loaded):
            with tempfile.TemporaryDirectory() as temp:
                parent = Path(temp) / "plugins"
                parent.mkdir()
                lock = parent / ".engineering-starter.install.lock"
                lock.mkdir()
                old = time.time() - installer.LOCK_STALE_SECONDS - 10
                (lock / "owner.json").write_text(json.dumps({
                    "format_version": 1,
                    "pid": 2_147_483_647,
                    "host": socket.gethostname(),
                    "created": old,
                    "token": "dead",
                }), encoding="utf-8")
                args = argparse.Namespace(dest=str(parent), agent_home=None, scope="user", target="portable")
                destination = installer.install(plugin_root, loaded, args)
                self.assertTrue(destination.is_dir())
                self.assertFalse(lock.exists())

            with tempfile.TemporaryDirectory() as temp:
                parent = Path(temp) / "plugins"
                parent.mkdir()
                lock = parent / ".engineering-starter.install.lock"
                lock.mkdir()
                (lock / "owner.json").write_text(json.dumps({
                    "format_version": 1,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "created": time.time() - installer.LOCK_STALE_SECONDS - 10,
                    "token": "live",
                }), encoding="utf-8")
                args = argparse.Namespace(dest=str(parent), agent_home=None, scope="user", target="portable")
                with self.assertRaises(installer.InstallError):
                    installer.install(plugin_root, loaded, args)

            for owner in (
                {"malformed": True},
                {
                    "format_version": 1,
                    "pid": 2_147_483_647,
                    "host": "another-runner.invalid",
                    "created": time.time() - installer.LOCK_STALE_SECONDS - 10,
                    "token": "foreign",
                },
            ):
                with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temp:
                    parent = Path(temp) / "plugins"
                    parent.mkdir()
                    lock = parent / ".engineering-starter.install.lock"
                    lock.mkdir()
                    (lock / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
                    args = argparse.Namespace(dest=str(parent), agent_home=None, scope="user", target="portable")
                    with self.assertRaises(installer.InstallError):
                        installer.install(plugin_root, loaded, args)
                    self.assertTrue(lock.exists())

    def test_stale_recovery_guard_prevents_second_contender_stealing_new_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / ".plugin.install.lock"
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({
                "format_version": 1,
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "created": time.time() - installer.LOCK_STALE_SECONDS - 10,
                "token": "stale-owner",
            }), encoding="utf-8")
            entered_rename = threading.Event()
            allow_rename = threading.Event()
            original_rename = installer.os.rename
            outcomes: list[object] = []

            def blocking_rename(source, destination):
                if Path(source) == lock:
                    entered_rename.set()
                    self.assertTrue(allow_rename.wait(5))
                return original_rename(source, destination)

            def first_recovery():
                try:
                    outcomes.append(installer._recover_stale_lock(lock))
                except Exception as exc:
                    outcomes.append(exc)

            with mock.patch.object(installer.os, "rename", side_effect=blocking_rename):
                thread = threading.Thread(target=first_recovery)
                thread.start()
                self.assertTrue(entered_rename.wait(5))
                # The second contender reads the same stale owner but cannot pass the
                # atomic recovery guard while the first claim is active.
                self.assertFalse(installer._recover_stale_lock(lock))
                allow_rename.set()
                thread.join(5)
            self.assertEqual(outcomes, [True])
            self.assertFalse(lock.exists())
            self.assertFalse(any("recovery-guard" in path.name for path in Path(temp).iterdir()))

            # Simulate the first installer acquiring its new live lock. A contender
            # must retain it rather than treating it as the stale directory.
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({
                "format_version": 1,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created": time.time(),
                "token": "new-live-owner",
            }), encoding="utf-8")
            self.assertFalse(installer._recover_stale_lock(lock))
            self.assertEqual(json.loads((lock / "owner.json").read_text())["token"], "new-live-owner")

    def test_recovery_guard_left_by_crash_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / ".plugin.install.lock"
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({
                "format_version": 1,
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "created": time.time() - installer.LOCK_STALE_SECONDS - 10,
                "token": "stale-owner",
            }), encoding="utf-8")
            guard = lock.with_name(f".{lock.name}.recovery-guard")
            guard.mkdir()
            (guard / "owner.json").write_text(json.dumps({"token": "crashed"}), encoding="utf-8")
            self.assertFalse(installer._recover_stale_lock(lock))
            self.assertTrue(lock.exists())
            self.assertTrue(guard.exists())

    def test_stale_recovery_rechecks_identity_and_owner_token(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / ".plugin.install.lock"
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({
                "format_version": 1,
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "created": time.time() - installer.LOCK_STALE_SECONDS - 10,
                "token": "stale-owner",
            }), encoding="utf-8")
            actual_acquire = installer._acquire_recovery_guard

            def replace_owner_after_claim(path):
                claim = actual_acquire(path)
                (path / "owner.json").write_text(json.dumps({
                    "format_version": 1,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "created": time.time(),
                    "token": "replacement-owner",
                }), encoding="utf-8")
                return claim

            with mock.patch.object(
                installer, "_acquire_recovery_guard", side_effect=replace_owner_after_claim
            ):
                self.assertFalse(installer._recover_stale_lock(lock))
            self.assertTrue(lock.exists())
            self.assertEqual(
                json.loads((lock / "owner.json").read_text())["token"],
                "replacement-owner",
            )

    def test_git_timeouts_fail_closed(self):
        timeout = subprocess.TimeoutExpired(["git", "fetch"], installer.GIT_TIMEOUT_SECONDS)
        with mock.patch.object(installer.subprocess, "run", side_effect=timeout):
            with self.assertRaises(installer.InstallError):
                installer.run_git(["status"])
        timeout = subprocess.TimeoutExpired(["git", "fetch"], creator.GIT_TIMEOUT_SECONDS)
        with mock.patch.object(creator.subprocess, "run", side_effect=timeout):
            with self.assertRaises(creator.PluginError):
                creator._run_git(["status"])

    def test_cross_platform_pid_liveness_uses_sys_executable(self):
        self.assertTrue(installer._windows_exit_code_is_alive(259))
        self.assertTrue(installer._windows_exit_code_is_alive(None))
        self.assertFalse(installer._windows_exit_code_is_alive(0))
        self.assertFalse(installer._windows_exit_code_is_alive(1))
        self.assertTrue(installer._pid_alive(os.getpid()))
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(installer._pid_alive(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=10)
        self.assertFalse(installer._pid_alive(process.pid))

    def test_source_precedence(self):
        with mock.patch.dict(os.environ, {"AGENT_PLUGINS_SOURCE": "/internal/mirror"}):
            self.assertEqual(installer.choose_source(None), "/internal/mirror")
            self.assertEqual(installer.choose_source("/explicit"), "/explicit")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Path(installer.choose_source(None)), ROOT)

    def test_effective_config_precedence_is_per_field(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_path = root / "user.json"
            system_path = root / "system.json"
            system_path.write_text(json.dumps({
                "plugins": {
                    "source": "/system/source",
                    "ref": "1" * 40,
                    "allowMutableRef": True,
                    "defaultTarget": "claude",
                },
                "agentHome": "/system/home",
            }), encoding="utf-8")
            user_path.write_text(json.dumps({
                "skills": {"source": "/shared/skills", "allowMutableRef": False},
                "plugins": {
                    "ref": "2" * 40,
                    "allowMutableRef": False,
                    "defaultTarget": "codex",
                },
                "agentHome": "/user/home",
            }), encoding="utf-8")
            args = installer.parser().parse_args([
                "install", "engineering-starter",
                "--source", "/cli/source",
                "--target", "portable",
                "--scope", "project",
                "--agent-home", "/cli/home",
                "--no-allow-mutable-ref",
            ])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {"AGENT_PLUGINS_REF": "3" * 40}, clear=True),
            ):
                effective = installer.resolve_effective_config(args)
            self.assertEqual(effective.source, "/cli/source")
            self.assertEqual(effective.ref, "3" * 40)
            self.assertEqual(effective.target, "portable")
            self.assertEqual(effective.agent_home, str(Path("/cli/home").resolve()))
            self.assertEqual(effective.scope, "project")
            self.assertFalse(effective.allow_mutable_ref)
            self.assertEqual(effective.provenance["source"], "cli")
            self.assertEqual(effective.provenance["ref"], "environment:AGENT_PLUGINS_REF")
            self.assertEqual(effective.provenance["allowMutableRef"], "cli")

            managed_args = installer.parser().parse_args(["install", "engineering-starter"])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {
                    "AGENT_PLUGINS_SOURCE": "/environment/source",
                    "AGENT_PLUGINS_REF": "3" * 40,
                }, clear=True),
            ):
                managed = installer.resolve_effective_config(managed_args)
            self.assertEqual(managed.source, "/environment/source")
            self.assertEqual(managed.ref, "3" * 40)
            self.assertEqual(managed.target, "codex")
            self.assertEqual(managed.agent_home, str(Path("/user/home").resolve()))
            self.assertFalse(managed.allow_mutable_ref)
            self.assertEqual(managed.provenance["target"], "user-config")

            project_args = installer.parser().parse_args([
                "install", "engineering-starter", "--scope", "project"
            ])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {
                    "AGENT_PLUGINS_SOURCE": "/environment/source",
                    "AGENT_PLUGINS_REF": "3" * 40,
                }, clear=True),
            ):
                project = installer.resolve_effective_config(project_args)
            self.assertEqual(project.agent_home, str((Path.cwd() / ".agents").resolve()))
            self.assertEqual(project.provenance["agentHome"], "project-default:ignored-user-config")

    def test_effective_config_user_system_checkout_and_canonical_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_path = root / "missing-user.json"
            system_path = root / "system.json"
            system_path.write_text(json.dumps({
                "plugins": {"source": "/system/source", "ref": "a" * 40},
                "agentHome": "~/.company-agents",
            }), encoding="utf-8")
            args = installer.parser().parse_args(["install", "engineering-starter"])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                effective = installer.resolve_effective_config(args)
            self.assertEqual(effective.source, "/system/source")
            self.assertEqual(effective.ref, "a" * 40)
            self.assertEqual(effective.agent_home, str((Path.home() / ".company-agents").resolve()))
            self.assertEqual(effective.target, "portable")
            self.assertEqual(effective.scope, "user")

            system_path.unlink()
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.object(installer, "discover_checkout", return_value=ROOT),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                checkout = installer.resolve_effective_config(args)
            self.assertEqual(Path(checkout.source), ROOT)
            self.assertIsNone(checkout.ref)
            self.assertEqual(checkout.agent_home, str((Path.home() / ".agents").resolve()))
            self.assertEqual(checkout.provenance["source"], "current-checkout")
            self.assertEqual(checkout.provenance["ref"], "current-checkout")

            local_args = installer.parser().parse_args([
                "install", "engineering-starter", "--source", str(ROOT)
            ])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                local = installer.resolve_effective_config(local_args)
            self.assertEqual(Path(local.source), ROOT)
            self.assertIsNone(local.ref)
            self.assertEqual(local.provenance["ref"], "local-source")

            remote_args = installer.parser().parse_args([
                "install", "engineering-starter",
                "--source", "https://git.example/internal/plugins.git",
            ])
            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                remote = installer.resolve_effective_config(remote_args)
            self.assertIsNone(remote.ref)
            self.assertEqual(remote.provenance["ref"], "unset")
            with self.assertRaises(installer.InstallError):
                with installer.materialize_source(
                    remote.source, remote.ref, remote.allow_mutable_ref
                ):
                    pass

            with (
                mock.patch.object(installer, "config_paths", return_value=(user_path, system_path)),
                mock.patch.object(installer, "discover_checkout", return_value=None),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                canonical = installer.resolve_effective_config(args)
            self.assertEqual(canonical.source, installer.CANONICAL_SOURCE)
            self.assertEqual(canonical.ref, installer.CANONICAL_REF)

    def test_effective_config_strict_json_and_ref_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            invalid_payloads = (
                '{"plugins": {}, "plugins": {}}',
                '{"unknown": true}',
                '{"plugins": {"source": 42}}',
                '{"plugins": {"allowMutableRef": "false"}}',
                '{"plugins": {"defaultTarget": "other"}}',
                '{"agentHome": " surrounded "}',
                json.dumps({"agentHome": "x\x00y"}),
                json.dumps({"plugins": {"source": "x\x00y"}}),
                '[]',
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(installer.InstallError):
                        installer.load_config(path)

            path.write_text(json.dumps({
                "plugins": {"source": "https://git.example/plugins.git", "ref": "main"}
            }), encoding="utf-8")
            missing = Path(temp) / "missing.json"
            args = installer.parser().parse_args(["list"])
            with (
                mock.patch.object(installer, "config_paths", return_value=(path, missing)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                with self.assertRaises(installer.InstallError):
                    installer.resolve_effective_config(args)

            path.write_text(json.dumps({
                "plugins": {
                    "source": "https://git.example/plugins.git",
                    "ref": "main",
                    "allowMutableRef": True,
                }
            }), encoding="utf-8")
            with (
                mock.patch.object(installer, "config_paths", return_value=(path, missing)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertTrue(installer.resolve_effective_config(args).allow_mutable_ref)

            cli_nul = installer.parser().parse_args([
                "install", "engineering-starter", "--agent-home", "x\x00y"
            ])
            with (
                mock.patch.object(installer, "config_paths", return_value=(missing, missing)),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                with self.assertRaises(installer.InstallError):
                    installer.resolve_effective_config(cli_nul)

    def test_effective_config_rejects_linked_config_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real = root / "real.json"
            real.write_text("{}", encoding="utf-8")
            linked = root / "linked.json"
            try:
                linked.symlink_to(real)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaises(installer.InstallError):
                installer.load_config(linked)

            hard_link = root / "hard-linked.json"
            try:
                os.link(real, hard_link)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")
            with self.assertRaises(installer.InstallError):
                installer.load_config(real)
            with self.assertRaises(installer.InstallError):
                installer.load_config(hard_link)

    def test_effective_config_diagnostic_redacts_url_credentials(self):
        config = installer.EffectiveConfig(
            "https://user:secret@git.example/plugins.git?token=secret#fragment",
            "a" * 40,
            False,
            "portable",
            None,
            "user",
            {"source": "cli"},
        )
        rendered = json.dumps(installer.effective_config_payload(config))
        self.assertNotIn("user:secret", rendered)
        self.assertNotIn("secret", rendered)
        self.assertIn("***@git.example", rendered)
        self.assertIn("REDACTED", rendered)
        self.assertEqual(installer._redact_source("https://git.example:invalid/repo"), "<invalid-url>")

    def test_remote_source_requires_full_sha(self):
        with self.assertRaises(installer.InstallError):
            with installer.materialize_source("https://example.invalid/plugins.git", None, False):
                pass
        with self.assertRaises(installer.InstallError):
            with installer.materialize_source("https://example.invalid/plugins.git", "main", False):
                pass


if __name__ == "__main__":
    unittest.main()
