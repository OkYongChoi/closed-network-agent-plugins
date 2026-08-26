# Third-party notices

## Agent Plugins specification

The vendored files `schemas/1.0.0/plugin.schema.json` and
`schemas/1.0.0/mcp.schema.json` are copied without modification from
`agentplugins/agent-plugins-spec` commit
`ff8ab5e392cc87bd88d87c060815a87490e51003`. They are licensed under the
Apache License 2.0; see `LICENSES/Apache-2.0.txt`.

The Agent Plugins specification text itself is not vendored. Documentation in
this repository summarizes the format and links to the CC-BY-4.0 upstream
specification.

## OpenAI plugin-creator

The design and command-line workflow of `plugin-creator` were informed by
`openai/skills`, `skills/.system/plugin-creator`, commit
`e940b8a86138adf03972802b990a1dfc57fcbf09`. The implementation in this
repository was rewritten for the portable Agent Plugins 1.0 format and closed
networks. The upstream skill is Apache-2.0 licensed.

## Vendored repo-summary skill

`plugins/engineering-starter/skills/repo-summary` is an exact build-time
snapshot of the example skill in `OkYongChoi/air-gapped-agent-skills`, with no local
modifications. Its source revision and matching source/result tree digests are
recorded in `plugins/engineering-starter/VENDORED_SKILLS.json`. Installation
never contacts the skills repository.
