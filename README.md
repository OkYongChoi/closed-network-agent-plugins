# Portable Agent Plugins for closed networks

This repository is the public source for
`OkYongChoi/air-gapped-agent-plugins`. It packages
portable [Agent Plugins 1.0](https://github.com/agentplugins/agent-plugins-spec/blob/ff8ab5e392cc87bd88d87c060815a87490e51003/spec/1.0.0.md)
without runtime package-manager or GitHub API dependencies. Python scripts use
only the standard library and Git CLI. The supported runtime baseline is
Python 3.11+ on Linux, macOS, and Windows.

## Included plugins

| Plugin | Purpose |
| --- | --- |
| `plugin-creator` | Create a canonical portable plugin package. |
| `plugin-installer` | Install or update plugins from the latest approved release or a pinned Git commit. |
| `engineering-starter` | Offline repository orientation; embeds `repo-summary` at build time. |

Every package has `plugins/<name>/plugin.json` and a `skills/` directory.
Optional MCP configuration belongs only at `mcp.json`.

## Get and use this repository

Clone the connected-side source, validate it, and install the plugin target you
need. Installation from the checkout is fully local:

```bash
git clone https://github.com/OkYongChoi/air-gapped-agent-plugins.git
cd air-gapped-agent-plugins
python3 -B scripts/validate_repo.py
./bin/plugin-installer list
./bin/plugin-installer install engineering-starter
```

On Windows PowerShell:

```powershell
git clone https://github.com/OkYongChoi/air-gapped-agent-plugins.git
Set-Location air-gapped-agent-plugins
python -B scripts/validate_repo.py
bin\plugin-installer.ps1 install engineering-starter
```

Installations default to `~/.agents/plugins` on Linux/macOS and
`%USERPROFILE%\.agents\plugins` on Windows. For a closed network, mirror the
repository as shown below and centrally configure the internal GitLab source;
end users then run only `install NAME` or `update NAME`.

### Bring both repositories into an internal GitLab

Mirror and transfer both repositories so plugin builds can refer to the
companion Skills source without using the public network:

```bash
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-skills.git
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-plugins.git
git --git-dir air-gapped-agent-skills.git push --mirror \
  https://gitlab.company.local/ai/air-gapped-agent-skills.git
git --git-dir air-gapped-agent-plugins.git push --mirror \
  https://gitlab.company.local/ai/air-gapped-agent-plugins.git
```

Deploy this source-only configuration to `/etc/agent-tools/config.json` on
Linux/macOS or `%ProgramData%\AgentTools\config.json` on Windows:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-skills.git",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-plugins.git",
    "allowMutableRef": false
  },
  "agentHome": "~/.agents"
}
```

With `ref` omitted, the installers resolve `latest-approved` to an immutable
commit. After the one-time portable installer bootstrap, Linux/macOS users run:

```bash
plugin_installer=~/.agents/plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py
python3 -B "$plugin_installer" list
python3 -B "$plugin_installer" install engineering-starter
python3 -B "$plugin_installer" update engineering-starter
```

On Windows PowerShell:

```powershell
$installer = "$env:USERPROFILE\.agents\plugins\plugin-installer\skills\plugin-installer\scripts\plugin_installer.py"
python -B $installer list
python -B $installer install engineering-starter
python -B $installer update engineering-starter
```

The Skills repository contains the matching `repo-summary` install/update
commands. Repository-level `bin/` wrappers remain available when operating from
a checkout, but are not part of the portable `plugin-installer` package.

## Closed-network bootstrap

On a connected transfer host, create a bare mirror and record the reviewed
commit before moving it through your approved transfer process:

```bash
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-plugins.git air-gapped-agent-plugins.git
git --git-dir air-gapped-agent-plugins.git rev-parse refs/heads/main
```

Then publish the mirror and clone it from the internal Git service:

```bash
git clone https://git.example.internal/agents/air-gapped-agent-plugins.git
cd air-gapped-agent-plugins
python3 -B scripts/validate_repo.py
python3 -B plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py \
  install plugin-installer --source "$PWD" --agent-home ~/.agents
```

No network is used when `--source` is a local checkout. For a temporary
operator override, set both approved source and snapshot:

```bash
export AGENT_PLUGINS_SOURCE=/srv/approved-mirrors/plugins
export AGENT_PLUGINS_REF=e0fbb53a8d04a26fd6f14051ed4ca855edb31070
```

On a Windows runner or workstation (PowerShell):

```powershell
git clone https://git.example.internal/agents/air-gapped-agent-plugins.git
Set-Location air-gapped-agent-plugins
python -B scripts/validate_repo.py
python -B plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py `
  install plugin-installer --source $PWD `
  --agent-home (Join-Path $HOME ".agents")
$env:AGENT_PLUGINS_SOURCE = "D:\approved-mirrors\plugins"
$env:AGENT_PLUGINS_REF = "e0fbb53a8d04a26fd6f14051ed4ca855edb31070"
```

For normal managed use, deploy only the approved GitLab source in JSON config.
Users do not export or maintain refs. For a centrally configured source without
a ref, the installer reads the manifest-only `latest-approved` branch, extracts
its full commit SHA, and fetches that immutable payload. A complete embedded
checkout remains usable without Git or a ref, and the public emergency fallback
remains pinned rather than depending on a mutable pointer.

### Optional: make an internal mirror the embedded fallback

The central configuration above is the normal deployment model. It keeps the
public upstream metadata intact while making every managed client use the
internal GitLab source. Use this procedure only when this repository will be
rebuilt and distributed as an internal-only product, and an installer with no
configuration must never fall back to the public GitHub URL.

Changing `CANONICAL_SOURCE` alone does **not** make clients automatically follow
newly approved releases. It is only the source fallback used when no CLI option,
environment variable, central configuration, or local checkout is available.
Keep the centrally deployed `plugins.source` set to the internal URL and omit
`plugins.ref`; that is what makes clients resolve `latest-approved` and the
immutable SHA in its release manifest.

For an internal-only rebuild, make one reviewed release change that keeps these
values aligned:

1. Change `CANONICAL_SOURCE` in
   `plugins/plugin-installer/skills/plugin-installer/scripts/plugin_installer.py`
   to the credential-free internal Git URL.
2. Change the top-level `repository` value in `catalog.json` to the same
   internal URL so the catalog describes the distributed product.
3. Keep `CANONICAL_REF` at a reviewed, reachable 40-character commit SHA. If
   the internal product has diverged from the imported history, update it to
   that product's reviewed release commit.
4. Ensure the GitLab `publish:latest-approved` job continues to pass
   `${CI_PROJECT_URL}.git` to `promote_release.py`; it writes the matching
   internal source and approved SHA to each release manifest.
5. Validate the release before publishing it:

   ```bash
   python3 -B scripts/validate_repo.py
   python3 -B -m unittest discover -s tests -v
   python3 -B tests/platform_verify.py --autocrlf true
   ```

Do not put credentials in `CANONICAL_SOURCE`, `catalog.json`, or a release
manifest. Give runtime clients read-only repository access through the approved
internal authentication mechanism.

## Central effective config

The installer resolves each field independently, with this precedence:

1. CLI (`--source`, `--ref`, `--agent-home`, `--scope`)
2. `AGENT_PLUGINS_SOURCE` and `AGENT_PLUGINS_REF`
3. user config: `~/.agents/config.json` on Linux/macOS or
   `%USERPROFILE%\.agents\config.json` on Windows
4. system config: `/etc/agent-tools/config.json` on Linux/macOS or
   `%ProgramData%\AgentTools\config.json` on Windows
5. the checkout containing the running installer
6. the embedded canonical source and pinned emergency fallback SHA

Only JSON is accepted. A shared Skills/Plugins configuration looks like:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-skills.git",
    "ref": "0123456789abcdef0123456789abcdef01234567",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-plugins.git",
    "allowMutableRef": false
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
./bin/create-plugin my-plugin --output ./work
```

Windows can call `bin\create-plugin.cmd` from cmd.exe,
`bin\create-plugin.ps1` from PowerShell, or use the policy-independent direct
form `python -B bin\create-plugin ...`. Equivalent wrappers are provided for
`plugin-installer`.

This creates `work/my-plugin` as a portable package.

Import a skill from a pinned internal Git source:

```bash
./bin/create-plugin engineering-tools --output ./work \
  --import-skill https://git.example.internal/agents/air-gapped-agent-skills.git \
  --ref 0123456789abcdef0123456789abcdef01234567 \
  --path skills/repo-summary
```

## Contribute a new plugin

Create new work on a topic branch; do not push directly to the protected
`main` branch:

```bash
git switch -c feat/add-my-plugin
./bin/create-plugin my-plugin --output ./plugins
```

Complete the generated package, then refresh the catalog and run the repository
checks. Do not calculate or edit the digest by hand:

```bash
python3 -B scripts/refresh_catalog.py
python3 -B scripts/refresh_catalog.py --check
python3 -B scripts/validate_repo.py
python3 -B -m unittest discover -s tests -v
git diff --check
```

Review the new `plugins/my-plugin` tree and the generated `catalog.json` entry,
then include both in the same commit and pull request:

```bash
git add plugins/my-plugin catalog.json
git commit -m "feat: add my-plugin"
git push -u origin feat/add-my-plugin
```

Open a pull request into `main` and merge it only after the required review and
CI checks pass. For an existing plugin change, skip the creation command and run
the same catalog-refresh and validation steps before committing. On Windows,
use `bin\create-plugin.ps1` and `python` instead of `python3`.

## List and install

```bash
./bin/plugin-installer list

./bin/plugin-installer install engineering-starter

./bin/plugin-installer update engineering-starter
```

`list` and `install` use the approved catalog. `update` compares an external
installation-state sidecar with the approved source, ref, release version, and
package digest. An unchanged installation is reported as current. A changed
installation is validated in staging before the plugin directory and state are
replaced as one rollback-protected transaction. A legacy installation without a
sidecar can be updated once and then participates in normal comparisons.

Scope defaults to `user`. User installs use `~/.agents/plugins/<name>`.
`--agent-home` relocates that root and `--dest` overrides the plugin parent.
Project scope ignores a user/system-configured `agentHome` and uses the current
project's `.agents` root. Supplying both CLI `--scope project` and CLI
`--agent-home` is an explicit override and uses that CLI home.

An explicit ref overrides `latest-approved` and must be a full commit SHA. This
is useful for audit reproduction or development diagnostics:

```bash
./bin/plugin-installer install engineering-starter \
  --source https://git.example.internal/agents/air-gapped-agent-plugins.git \
  --ref 0123456789abcdef0123456789abcdef01234567
```

`--allow-mutable-ref` is an explicit development escape hatch for a branch or
tag. Catalog SHA-256 verification still applies.

## GitLab approval publishing and rollback

Merging to the protected default branch is the approval event. The included GitLab CI
pipeline validates Linux and Windows behavior, then serializes publication with
`resource_group: latest-approved`. The publish job creates a manifest-only
`latest-approved` branch containing the credential-free GitLab project source,
the immutable merge commit SHA, an `approved-<pipeline IID>` release version, the SHA-256 of
`catalog.json`, every approved plugin name and package digest, and the monotonic
GitLab pipeline IID as `sequence`. If resource-group scheduling presents an
older automatic pipeline after a newer one, the publisher compares `sequence`
and skips the stale promotion. Scheduled, web, API, and manual pipelines do not
automatically publish a release.

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

Protect the default branch as well: disable direct pushes while retaining merge
permission for authorized Merge Request reviewers. This makes a default-branch
`push` pipeline an MR merge result rather than an ad-hoc push.

For rollback, start a default-branch pipeline with `ROLLBACK_REF` set to a
previously approved full payload SHA and run the manual
`rollback:latest-approved` job. The SHA must occur in the existing approval
history. Rollback receives a sequence greater than the current pointer even if
its job came from an older pipeline, and is recorded as a new manifest commit. The next
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

After intentionally changing a plugin, refresh its catalog digest and review
the resulting `catalog.json` diff before committing the change:

```bash
python3 -B scripts/refresh_catalog.py
python3 -B scripts/refresh_catalog.py --check
```

Use `python` instead of `python3` on Windows. The refresh command preserves the
catalog's repository value and deterministically rebuilds the plugin entries.

The installer copies the selected package into a private snapshot before
loading or hashing it. Installation and projection use only that verified
snapshot, so a concurrently changing shared mirror cannot alter bytes between
verification and installation.

## Validation

```bash
python3 -B scripts/refresh_catalog.py --check
python3 -B scripts/validate_repo.py
python3 -B -m unittest discover -s tests -v
python3 -B tests/platform_verify.py --autocrlf true
```

Use `python` instead of `python3` on Windows. `platform_verify.py` creates a
temporary commit from the current tracked and untracked non-ignored working
snapshot, performs an offline clean clone with the requested `core.autocrlf`
setting, and reruns strict validation and tests from that clone.

GitHub Actions runs the same checks on `ubuntu-latest`, `macos-latest`, and
`windows-latest`. For an internal GitLab, `.gitlab-ci.yml` contains required
jobs tagged `linux` and `windows`, plus an optional manual `validate:macos` job
tagged `macos`; configure self-managed shell runners with those tags, or rename
the tags to match the internal runner inventory. The Windows shell executor is
expected to provide `python` and `git` on `PATH` (PowerShell/pwsh is supported).
No CI job pulls a language package or container image.

The vendored `repo-summary` source commit and tree digest are fixed in
`plugins/engineering-starter/VENDORED_SKILLS.json` and checked on every CI run.

Current public CI results are available in
[GitHub Actions](https://github.com/OkYongChoi/air-gapped-agent-plugins/actions).
Approved release identity, version, catalog digest, and package digests are
recorded in the immutable release manifest referenced by `latest-approved`.
Internal GitLab runner execution remains an environment-specific acceptance
step after mirror import.

Vendored schemas are exact upstream bytes pinned in `UPSTREAM.lock.json`.
Licensing and modification provenance are in `THIRD_PARTY_NOTICES.md`.
