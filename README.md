# Portable Agent Plugins for closed networks

This repository is the public source for `OkYongChoi/plugins`. It packages
portable [Agent Plugins 1.0](https://github.com/agentplugins/agent-plugins-spec/blob/ff8ab5e392cc87bd88d87c060815a87490e51003/spec/1.0.0.md)
without runtime package-manager or GitHub API dependencies. Python scripts use
only the standard library and Git CLI. The supported runtime baseline is
Python 3.11+ on Linux and Windows.

## Included plugins

| Plugin | Purpose |
| --- | --- |
| `plugin-creator` | Create a canonical plugin and optional isolated Codex/Claude projections. |
| `plugin-installer` | Verify and atomically install catalogued plugins from a local mirror or pinned Git commit. |
| `engineering-starter` | Offline repository orientation; embeds `repo-summary` at build time. |

Every canonical package has `plugins/<name>/plugin.json` and a `skills/`
directory. Optional MCP configuration belongs only at `mcp.json`. Vendor files
such as `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json` are generated
outside the canonical package or during target installation.

## Closed-network bootstrap

On a connected transfer host, create a bare mirror and record the reviewed
commit before moving it through your approved transfer process:

```bash
git clone --mirror https://github.com/OkYongChoi/plugins.git plugins.git
git --git-dir plugins.git rev-parse refs/heads/main
```

Then publish the mirror and clone it from the internal Git service:

```bash
git clone https://git.example.internal/agents/plugins.git
cd plugins
python3 -B scripts/validate_repo.py
python3 -B plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py \
  install plugin-installer --source "$PWD" --target portable --agent-home ~/.agents
```

No network is used when `--source` is a local checkout. For ongoing use, set:

```bash
export AGENT_PLUGINS_SOURCE=/srv/approved-mirrors/plugins
```

On a Windows runner or workstation (PowerShell):

```powershell
git clone https://git.example.internal/agents/plugins.git
Set-Location plugins
python -B scripts/validate_repo.py
python -B plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py `
  install plugin-installer --source $PWD --target portable `
  --agent-home (Join-Path $HOME ".agents")
$env:AGENT_PLUGINS_SOURCE = "D:\approved-mirrors\plugins"
```

Source selection is `--source` → `AGENT_PLUGINS_SOURCE` → the checkout that
contains the running installer → `https://github.com/OkYongChoi/plugins.git`.
The final remote fallback intentionally fails until a full `--ref` is supplied;
it never installs from an implicit moving branch.

## Create plugins

```bash
./bin/create-plugin my-plugin --output ./work --adapters codex,claude
```

Windows can call `bin\create-plugin.cmd` from cmd.exe,
`bin\create-plugin.ps1` from PowerShell, or use the policy-independent direct
form `python -B bin\create-plugin ...`. Equivalent wrappers are provided for
`plugin-installer`.

This creates `work/my-plugin` as the canonical package and writes projections
under `work/.staging/codex` and `work/.staging/claude`. The canonical package is
not mutated by projection generation.

Import a skill from a pinned internal Git source:

```bash
./bin/create-plugin engineering-tools --output ./work \
  --import-skill https://git.example.internal/agents/skills.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --path skills/repo-summary
```

## List and install

```bash
./bin/plugin-installer list --source /srv/approved-mirrors/plugins

./bin/plugin-installer install engineering-starter \
  --source /srv/approved-mirrors/plugins \
  --target portable \
  --agent-home ~/.agents
```

Targets are explicit: `portable`, `codex`, or `claude`. Portable user installs
use `~/.agents/plugins/<name>`. Codex uses its native split layout:
`~/.agents/plugins/marketplace.json` plus `~/plugins/<name>`, so the marketplace
source `./plugins/<name>` resolves correctly. Claude uses a self-contained
marketplace root at `~/.claude/plugins/marketplaces/okyongchoi-portable`, with
`.claude-plugin/marketplace.json` and `plugins/<name>` as siblings. Project
scope uses the equivalent roots below `./.agents` or `./.claude`.

`--agent-home` relocates the native root. `--dest` overrides the portable plugin
parent. For Claude, it must be a directory named exactly `plugins` directly
below the intended marketplace root; arbitrary paths are rejected so
`./plugins/<name>` cannot point elsewhere. For Codex it is accepted only when it
equals the plugin directory implied by the selected agent home. Vendor targets
contain native manifests and a projected `.mcp.json` when portable `mcp.json`
is present. Register a non-default marketplace with the vendor CLI when that
client requires it.

Portable manifests may contain only the required `$schema` and `name`. Native
projections leave that canonical file unchanged and supply deterministic native
defaults when metadata is absent: version `0.1.0`, description
`Portable projection for <name>.`, and author `Unknown`.

Remote Git sources require a full commit SHA:

```bash
./bin/plugin-installer install engineering-starter \
  --source https://git.example.internal/agents/plugins.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --target codex
```

`--allow-mutable-ref` is an explicit development escape hatch for a branch or
tag. Catalog SHA-256 verification still applies.

## Safety model

Before installation, the installer verifies the catalog digest and rejects
absolute/traversing catalog paths, symlinks, hard links, case-colliding paths,
non-regular files, and trees over 2,000 files or 50 MiB. It stages on the
destination filesystem, uses an exclusive lock, atomically renames the staged
plugin, and does not overwrite an existing installation. Marketplace failures
are checked before the plugin becomes visible and trigger rollback. Vendor
marketplace read-modify-write operations share one marketplace-wide lock across
all plugin names.

The portable path profile additionally rejects Windows device names (`CON`,
`NUL`, `COM1`, and related aliases), reserved characters and alternate data
stream syntax, trailing dots/spaces, non-NFC names, NFKC/case collisions,
overlong components, and overlong package-relative paths. Catalog files and
every catalog path component must be real files/directories; Windows reparse
points and junctions are treated like symlinks and fail closed.

Locks record host, PID, creation time, and a random ownership token. An old
lock is recovered only on the same host when its metadata is valid and its PID
is confirmed dead. Foreign-host or malformed locks are retained for an
operator to investigate, which avoids stealing a live lock on shared storage.
Recovery is serialized by an atomic per-lock guard and rechecks directory/file
identity plus the owner token immediately before a non-replacing quarantine
rename. A guard left by a crashed recovery attempt fails closed and requires
operator inspection; it never authorizes deletion of a newer live lock.
Git subprocesses have a 60-second timeout. Plugin creation uses a private
staging directory on the output filesystem and rolls back canonical and adapter
destinations if any publication step fails. An existing `.staging` path must be
a real, non-reparse directory contained by the selected output root.

`.gitattributes` pins recognized text to LF so catalog and vendored SHA-256
values remain stable with `core.autocrlf=true`; common binary types are marked
binary and are never line-ending transformed.

Canonical plugin trees also reject runtime artifacts such as `__pycache__`,
`.pytest_cache`, `*.pyc`, and `*.pyo`. They are neither ignored nor hashed: their
presence fails strict validation, catalog digest generation, and installation.
This keeps the catalog's “every file path and byte” guarantee stable between a
developer checkout and a clean remote clone. Run Python verification with an
external bytecode cache or `PYTHONDONTWRITEBYTECODE=1`.

There are deliberately two validation modes. `plugin-creator` and
`scripts/validate_repo.py` are strict authoring gates: repository content must
fully conform, so unknown manifest fields or any invalid component fail the
check. `plugin-installer` is a resilient client loader: it reports and removes
unknown manifest fields or a non-object `extensions`; skips invalid skills;
disables only MCP for an invalid top-level `mcp.json`; and skips only an invalid
MCP server when other servers are valid. The sanitized installed copy preserves
the valid components. Manifest violations outside the two recovery exceptions
remain fatal.

For MCP, both modes reject command and working-directory traversal, absolute or
shell-like stdio commands, reserved environment overrides, suspicious embedded
secret fields, credential-bearing headers, malformed headers, userinfo or
fragments in URLs, and non-HTTPS remote endpoints. Loopback HTTP remains valid
as allowed by Agent Plugins 1.0.

The catalog digest covers every file path and byte in the canonical plugin.
Git SHA verification authenticates the requested checkout identity; SHA-256
then verifies the selected package against that checkout's catalog. v1 does not
provide artifact signatures.

The installer copies the selected package into a private snapshot before
loading or hashing it. Installation and projection use only that verified
snapshot, so a concurrently changing shared mirror cannot alter bytes between
verification and installation.

## Validation

```bash
python3 -B scripts/validate_repo.py
python3 -B -m unittest discover -s tests -v
python3 -B tests/platform_verify.py --autocrlf true
```

Use `python` instead of `python3` on Windows. `platform_verify.py` creates a
temporary commit from the current tracked and untracked non-ignored working
snapshot, performs an offline clean clone with the requested `core.autocrlf`
setting, and reruns strict validation and tests from that clone.

GitHub Actions runs the same checks on `ubuntu-latest` and `windows-latest`.
For an internal GitLab, `.gitlab-ci.yml` contains separate jobs tagged `linux`
and `windows`; configure self-managed shell runners with those tags, or rename
the tags to match the internal runner inventory. The Windows shell executor is
expected to provide `python` and `git` on `PATH` (PowerShell/pwsh is supported).
No CI job pulls a language package or container image.

The vendored `repo-summary` source commit and tree digest are fixed in
`plugins/engineering-starter/VENDORED_SKILLS.json` and checked on every CI run.

### Validation status

- Public repository: <https://github.com/OkYongChoi/plugins>
- `plugin-creator` pattern source: `openai/skills@e940b8a86138adf03972802b990a1dfc57fcbf09`
- Agent Plugins 1.0 specification and schemas: `ff8ab5e392cc87bd88d87c060815a87490e51003`
- Vendored `repo-summary` source: `OkYongChoi/skills@ede183a13cc033d5a46ef42b6ad3e8d0a7e7530f`

On 2026-08-24, the current cross-platform working snapshot passed strict
repository validation and all 41 tests, with only host-inapplicable capability
tests skipped on macOS. An offline temporary commit and clean clone with
`core.autocrlf=true` also passed. The vendored skill bytes match the pinned
Skills source exactly, with no runtime fetch. Portable installation, the bundled
Codex validator, and Claude plugin and marketplace validation passed. Record the
new Plugins release commit and GitHub/GitLab Windows runner results here after
publishing this snapshot.

Vendored schemas are exact upstream bytes pinned in `UPSTREAM.lock.json`.
Licensing and modification provenance are in `THIRD_PARTY_NOTICES.md`.
