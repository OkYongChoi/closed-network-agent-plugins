---
name: plugin-installer
description: List and install catalogued Agent Plugins from local paths, internal Git mirrors, or a full-SHA remote checkout with content-digest verification. Use for portable, Codex, or Claude installations in closed networks.
---

# Plugin Installer

Prefer an approved internal mirror. Effective settings are resolved per field
in this order: CLI, `AGENT_PLUGINS_SOURCE`/`AGENT_PLUGINS_REF`, user JSON config,
system JSON config, the checkout containing this skill, then the pinned
canonical fallback. User config is `~/.agents/config.json` on Linux/macOS and
`%USERPROFILE%\.agents\config.json` on Windows. System config is
`/etc/agent-tools/config.json` or `%ProgramData%\AgentTools\config.json`.
A remote source requires a full 40-character commit SHA unless mutable refs are
explicitly enabled by CLI or trusted config.

## List

```bash
python3 scripts/plugin_installer.py list --source /srv/mirrors/plugins
```

## Install

```bash
python3 scripts/plugin_installer.py install engineering-starter \
  --source /srv/mirrors/plugins
```

For an internal Git server:

```bash
python3 scripts/plugin_installer.py install engineering-starter \
  --source https://git.example.internal/agents/plugins.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --target codex
```

Portable installs default to `~/.agents/plugins`. Codex installs the marketplace
at `~/.agents/plugins/marketplace.json` and plugin content at `~/plugins`; this
split is required by Codex marketplace path resolution. Claude installs a
self-contained marketplace below
`~/.claude/plugins/marketplaces/okyongchoi-portable`, containing both
`.claude-plugin/marketplace.json` and `plugins/`. Project scope uses equivalent
roots below the current project. A Claude `--dest` must itself be named
`plugins` and sit directly below the marketplace root. Existing installs are
never overwritten.

On Windows use `python` instead of `python3`; paths may be native Windows paths:

```powershell
python scripts/plugin_installer.py install engineering-starter `
  --source D:\approved-mirrors\plugins
```

With centrally deployed config, the normal command is simply:

```bash
python3 scripts/plugin_installer.py install engineering-starter
```

`target` defaults to `portable` and `scope` defaults to `user`. Inspect the
resolved non-secret settings and per-field provenance with
`effective-config`; URL userinfo, query values, and fragments are not printed:

```bash
python3 scripts/plugin_installer.py effective-config
```

Only JSON config is supported. Unknown keys, duplicate keys, empty values,
wrong types, and invalid targets fail closed. The shared config may contain
`skills`, `plugins`, and `agentHome`; plugin settings are `source`, `ref`,
`allowMutableRef`, and `defaultTarget`. Config files must be real, singly-linked
regular files. Project scope ignores config-derived `agentHome`; an explicit
CLI `--agent-home` still wins when combined with `--scope project`.

## Runtime loading boundaries

The installer follows Agent Plugins 1.0 client-loading recovery rules after
the source checkout and catalog digest are verified:

- Unknown top-level manifest fields and a non-object `extensions` value are
  reported and omitted; other manifest schema violations remain fatal.
- Invalid skills are reported and omitted without invalidating valid skills.
- An invalid top-level `mcp.json` disables MCP only.
- An invalid MCP server is reported and omitted without disabling valid
  servers.

The installed copy contains only accepted components. Unsafe package entries
(including symlinks, hard links, path escapes, and case collisions) remain a
fatal installer policy even when a component-level client could otherwise skip
them.

The portable path policy also rejects Windows device aliases, ADS/reserved
characters, trailing dots/spaces, non-NFC names, Unicode-normalized collisions,
and path-length violations. Junctions and other Windows reparse points are
rejected. A stale lock is recovered only when it has valid ownership metadata,
was created on the current host, and its PID is confirmed dead; malformed and
foreign-host locks fail closed. Recovery uses an atomic guard, identity and
owner-token rechecks, and a non-replacing quarantine rename. A crash-left guard
requires operator inspection rather than risking deletion of a newer live lock.
Git operations time out after 60 seconds.
