---
name: "repo-summary"
description: "Summarize a local source repository's structure, likely build and test commands, Git state, and review risks. Use for offline repository orientation or engineering handoff."
license: "Apache-2.0"
compatibility: "Requires Python 3.11+; Git metadata is optional and network access is never used."
---

# Repository Summary

Run `scripts/repo_summary.py PATH` to produce a bounded Markdown overview, or add
`--format json` for structured output. The script reads filesystem metadata and a
small set of project manifests, and invokes only local Git commands.
It recognizes common Linux and Windows entry points, including POSIX and batch
Gradle wrappers, PowerShell/batch scripts, and .NET solution/project or
`Directory.Build.*` files. Candidate commands are suggestions only.

Use the output as orientation, not as proof that a command is safe or that every
risk was found. Confirm detected commands against project documentation before
running them. The risk section reports observable signals such as a dirty
worktree, symlinks, Windows junctions/reparse points, large files, and
credential-shaped filenames; it does not inspect file contents or claim to
perform a security scan. Linked/reparse directories are pruned before traversal.

Do not enable network access for this workflow. Respect the default file and byte
limits when examining an unfamiliar repository; increase them only when the user
has placed the larger tree in scope.
