# Agents, MCP, and Impact

cfgit is designed for humans and AI coding agents to use the same operation
surface.

## Agent rule

Do not write directly to the database when cfgit can perform the mutation. Use
cfgit so drift, history, and rollback remain coherent.

Recommended agent flow:

1. `cfg_status`
2. `cfg_diff` if there is drift or a planned edit
3. `cfg_impact` before committing behavior-affecting records
4. `cfg_commit` or `cfg_adopt`
5. `cfg_status` again

If status is `changed_outside_cfgit`, do not commit over it. Diff it, explain it,
then ask whether to adopt it or merge manually.

## MCP server

Install the MCP extra:

```bash
pip install -e '.[mcp]'
```

Run:

```bash
cfg-mcp
```

The MCP tools return a uniform envelope:

```json
{
  "status": "ok",
  "code": 0,
  "message": "",
  "data": {}
}
```

Tool list:

- `cfg_whoami`
- `cfg_init`
- `cfg_status`
- `cfg_import`
- `cfg_diff`
- `cfg_impact`
- `cfg_commit`
- `cfg_log`
- `cfg_show`
- `cfg_adopt`
- `cfg_restore`
- `cfg_tag`
- `cfg_fsck`

## Portable skill

The skill file is:

```text
skills/cfgit/SKILL.md
```

It is intentionally plain. It tells a coding agent how to inspect first, branch on
drift, avoid raw database writes, and use JSON output.

## Impact plugin

Install:

```bash
pip install -e plugins/cfg_impact
```

Deterministic impact:

```bash
cfg impact agent_configs:agent_planner --json
```

LLM narration:

```bash
cfg impact agent_configs:agent_planner --llm --json
```

Provider config:

```toml
[connections]
ai_provider = "claude"
```

Supported providers:

- `claude`, using `ANTHROPIC_API_KEY`
- `openai`, using `OPENAI_API_KEY`

The impact engine calls a provider-agnostic `narrate()` or `complete()` method.
Provider selection is done by `cfg_impact.providers.factory.ImpactProviderFactory`.
No vendor provider code lives in `src/cfg/core`.
