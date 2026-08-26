---
name: plugin-creator
description: Create and validate portable Agent Plugins 1.0 packages, optionally importing a skill at a pinned Git commit. Use when scaffolding a plugin for an offline or mirrored environment.
---

# Plugin Creator

Creation stages the canonical package on the selected output filesystem before
publishing it. This supports Windows drive-letter layouts and POSIX
(Linux/macOS) mounts without cross-filesystem rename failures.

## Create a plugin

```bash
python3 scripts/create_plugin.py create-plugin my-plugin \
  --output ./plugins
```

The canonical package is written to `./plugins/my-plugin`.

To import an existing Agent Skill, pin remote Git sources to a full commit SHA:

```bash
python3 scripts/create_plugin.py create-plugin engineering-tools \
  --output ./plugins \
  --import-skill https://git.example.internal/agents/air-gapped-agent-skills.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --path skills/repo-summary
```

Local paths are accepted without a Git ref. The importer rejects symlinks,
path escapes, case-colliding names, oversized trees, and invalid skill
frontmatter before copying anything. With `--ref`, even a local Git source is
materialized from that exact commit, so dirty and untracked working-tree files
cannot enter the package. Without `--ref`, provenance records a local snapshot
with a null commit rather than claiming the current `HEAD` is authoritative.
Portable names and paths reject Windows device aliases, ADS/reserved
characters, trailing dots/spaces, non-NFC names, Unicode-normalized collisions,
junctions/reparse points, and path-length violations on Linux, macOS, and Windows.
Git operations time out after 60 seconds.

## Validate

```bash
python3 scripts/create_plugin.py validate ./plugins/my-plugin
```

The validator implements the portable manifest rules needed by this creator.
It deliberately does not claim to be a general JSON Schema implementation.
It is an authoring gate, so it rejects unknown manifest fields, non-object
`extensions`, invalid skills, an invalid `mcp.json`, or any invalid MCP server.
That strict policy is intentionally stronger than the Agent Plugins client
loading failure boundaries used by `plugin-installer`.
