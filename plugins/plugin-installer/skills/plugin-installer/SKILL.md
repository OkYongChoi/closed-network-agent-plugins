---
name: plugin-installer
description: List and install catalogued Agent Plugins from local paths, internal Git mirrors, or a full-SHA remote checkout with content-digest verification. Use for portable, Codex, or Claude installations in closed networks.
---

# Plugin Installer

Prefer a local checkout or approved internal mirror. The source precedence is:
`--source`, `AGENT_PLUGINS_SOURCE`, the checkout containing this skill, then the
canonical repository URL. A remote source requires a full 40-character commit
SHA unless `--allow-mutable-ref` is explicitly supplied.

## List

```bash
python3 scripts/plugin_installer.py list --source /srv/mirrors/plugins
```

## Install

```bash
python3 scripts/plugin_installer.py install engineering-starter \
  --source /srv/mirrors/plugins \
  --target portable \
  --agent-home ~/.agents
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
