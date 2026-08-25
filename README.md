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
| `plugin-installer` | Install or update plugins from the latest approved release or a pinned Git commit. |
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

No network is used when `--source` is a local checkout. For a temporary
operator override, set both approved source and snapshot:

```bash
export AGENT_PLUGINS_SOURCE=/srv/approved-mirrors/plugins
export AGENT_PLUGINS_REF=67a89145f3878ed277e1c8e86d73fc9f7d69edf0
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
$env:AGENT_PLUGINS_REF = "67a89145f3878ed277e1c8e86d73fc9f7d69edf0"
```

For normal managed use, deploy only the approved GitLab source in JSON config.
Users do not export or maintain refs. For a centrally configured source without
a ref, the installer reads the manifest-only `latest-approved` branch, extracts
its full commit SHA, and fetches that immutable payload. A complete embedded
checkout remains usable without Git or a ref, and the public emergency fallback
remains pinned rather than depending on a mutable pointer.

## Central effective config

The installer resolves each field independently, with this precedence:

1. CLI (`--source`, `--ref`, `--target`, `--agent-home`, `--scope`)
2. `AGENT_PLUGINS_SOURCE` and `AGENT_PLUGINS_REF`
3. user config: `~/.agents/config.json` on Linux/macOS or
   `%USERPROFILE%\.agents\config.json` on Windows
4. system config: `/etc/agent-tools/config.json` on Linux or
   `%ProgramData%\AgentTools\config.json` on Windows
5. the checkout containing the running installer
6. the embedded canonical source and pinned emergency fallback SHA

Only JSON is accepted. A shared Skills/Plugins configuration looks like:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/skills.git",
    "ref": "0123456789abcdef0123456789abcdef01234567",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/plugins.git",
    "allowMutableRef": false,
    "defaultTarget": "portable"
  },
  "agentHome": "~/.agents"
}
```

Unknown or duplicate keys, wrong types, empty source/ref values, invalid
targets, and a non-full explicit ref without `allowMutableRef: true` fail
closed. Branch and tag refs remain an explicit development-only exception. An
installer running from a complete local checkout uses that embedded checkout
without a ref. A configured remote source, or a local Git mirror that contains
`refs/heads/latest-approved`, resolves that pointer, validates its strict release
manifest and source binding, then checks out only the full SHA recorded there.
An ordinary local working checkout without that ref remains a direct development
source for backward compatibility. Config files
must be real, singly-linked regular files; symlinks, hard links, and Windows
reparse points are rejected.

Administrators can verify the effective non-secret values and provenance:

```bash
./bin/plugin-installer effective-config
```

```powershell
bin\plugin-installer.ps1 effective-config
```

The diagnostic redacts URL userinfo and query values and removes fragments.
It prints the expanded effective agent-home path, including the target/scope
default when no path was configured.
Environment variables exist for controlled process-level overrides, not as a
required end-user setup step.

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
./bin/plugin-installer list

./bin/plugin-installer install engineering-starter

./bin/plugin-installer update engineering-starter
```

`list` and `install` use the approved catalog. `update` compares an external
installation-state sidecar with the approved source, ref, release version, and
package digest. An unchanged installation is reported as current. A changed
portable, Codex, or Claude installation is projected and validated in staging
before the plugin directory, marketplace entry, and state are replaced as one
rollback-protected transaction. A legacy installation without a sidecar can be
updated once and then participates in normal comparisons.

Targets are `portable`, `codex`, or `claude`; central config can select one and
the default is `portable`. Scope defaults to `user`. Portable user installs
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

Project scope ignores a user/system-configured `agentHome` and uses the current
project's `.agents` or `.claude` root. Supplying both CLI `--scope project` and
CLI `--agent-home` is an explicit override and uses that CLI home.

Portable manifests may contain only the required `$schema` and `name`. Native
projections leave that canonical file unchanged and supply deterministic native
defaults when metadata is absent: version `0.1.0`, description
`Portable projection for <name>.`, and author `Unknown`.

An explicit ref overrides `latest-approved` and must be a full commit SHA. This
is useful for audit reproduction or development diagnostics:

```bash
./bin/plugin-installer install engineering-starter \
  --source https://git.example.internal/agents/plugins.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --target codex
```

`--allow-mutable-ref` is an explicit development escape hatch for a branch or
tag. Catalog SHA-256 verification still applies.

## GitLab approval publishing and rollback

Merging to the default branch is the approval event. The included GitLab CI
pipeline validates Linux and Windows behavior, then serializes publication with
`resource_group: latest-approved`. The publish job creates a manifest-only
`latest-approved` branch containing the credential-free GitLab project source,
the immutable merge commit SHA, an `approved-<pipeline IID>` release version, the SHA-256 of
`catalog.json`, every approved plugin name and package digest, and the monotonic
GitLab pipeline IID as `sequence`. If resource-group scheduling presents an
older automatic pipeline after a newer one, the publisher compares `sequence`
and skips the stale promotion.

The manifest is never a redirect to another repository. The configured source
must match its source binding, and the payload is fetched from that same source
by full SHA. A local bare mirror works when it includes `latest-approved` and
its `origin` identifies the approved GitLab project.

For same-project CI publishing, enable **Settings > CI/CD > Job token
permissions > Allow Git push requests to the repository**. It is disabled by
default. Protect `latest-approved` and allow the CI job token's triggering role
to push; normal users should not update it. A job-token push does not trigger a
new pipeline, so publication does not loop. If policy disallows job-token push,
use an equivalent protected internal bot credential for the job's Git remote;
never store it in the manifest or logs.

For rollback, start a default-branch pipeline with `ROLLBACK_REF` set to a
previously approved full payload SHA and run the manual
`rollback:latest-approved` job. Rollback is the only explicit bypass of the
stale-sequence guard and is recorded as a new manifest commit. The next
`update engineering-starter` installs that earlier immutable snapshot. The
manifest branch history remains the approval and rollback audit trail.

## Safety model

Before installation, the installer verifies the catalog digest and rejects
absolute/traversing catalog paths, symlinks, hard links, case-colliding paths,
non-regular files, and trees over 2,000 files or 50 MiB. It stages on the
destination filesystem, uses an exclusive lock, atomically renames the staged
plugin, and does not overwrite an existing installation during `install`.
`update` backs up the prior plugin, marketplace, and external state sidecar
under the same lock and restores all three after a catchable in-process
publication failure, including `KeyboardInterrupt`. An uncatchable process or
host termination can leave hidden transaction backups because these separate
paths cannot be committed by one rename; rerun `update` when the target exists,
otherwise stop for operator inspection and restoration. Vendor
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
- Verified implementation commit: `67a89145f3878ed277e1c8e86d73fc9f7d69edf0`
- `plugin-creator` pattern source: `openai/skills@e940b8a86138adf03972802b990a1dfc57fcbf09`
- Agent Plugins 1.0 specification and schemas: `ff8ab5e392cc87bd88d87c060815a87490e51003`
- Vendored `repo-summary` source: `OkYongChoi/skills@ede183a13cc033d5a46ef42b6ad3e8d0a7e7530f`

On 2026-08-24, strict repository validation, all 41 tests, and an offline
`core.autocrlf=true` clean-clone check passed. The corresponding
[GitHub Actions run](https://github.com/OkYongChoi/plugins/actions/runs/32706958783)
passed on Ubuntu and Windows, including native Windows launchers, hard links,
junctions, and process-handle checks. The vendored skill bytes match the pinned
Skills source exactly, with no runtime fetch. Portable installation, the bundled
Codex validator, and Claude plugin and marketplace validation passed. Internal
GitLab runner execution remains an environment-specific acceptance step after
mirror import.

Vendored schemas are exact upstream bytes pinned in `UPSTREAM.lock.json`.
Licensing and modification provenance are in `THIRD_PARTY_NOTICES.md`.
