# cfg-impact

Optional system-impact analysis for cfgit.

This package is deliberately outside `src/cfg/core/`. The cfgit core stays LLM-free and provider-free; this plugin owns both deterministic impact analysis and optional LLM narration.

## Boundary

- `src/cfg/core/` never imports this plugin.
- `src/cfg/core/` never imports vendor SDKs or provider clients.
- The main action layer imports `cfg_impact.overview` only when `cfg impact` or the UI/MCP impact action is called.
- Provider selection comes from `[connections].ai_provider` unless a caller passes `provider`.

## Provider Pattern

The provider layer uses a small factory pattern:

- `cfg_impact.providers.base.BaseImpactProvider`
- `cfg_impact.providers.factory.ImpactProviderFactory`
- `cfg_impact.providers.claude.ClaudeProvider`
- `cfg_impact.providers.openai_provider.OpenAIProvider`
- `cfg_impact.providers.gemini.GeminiProvider`

The impact engine calls `narrate()` or `complete()`. It never imports a vendor module directly.

## Commands

Deterministic local analysis:

`cfg impact agent_configs:agent_planner =HEAD =live --json`

Opt-in LLM narration:

`cfg impact agent_configs:agent_planner =HEAD =live --llm --json`

Scope narration to selected records instead of the whole system:

`cfg impact agent_configs:agent_planner --against agent_configs:critic --against modelgarden_models:openai/gpt-4o-mini --llm --json`

LLM narration is refused unless the record is listed in
`[connections].share_with_ai`. The payload sent to the provider is bounded and
secret-stripped. It includes real before/after field diffs for the changed record,
plus only allowlisted text from related records. In scoped mode, only selected
records are included in the system map.

The plugin uses `ANTHROPIC_API_KEY` for `claude`, `OPENAI_API_KEY` for `openai`,
and `GEMINI_API_KEY` or `GOOGLE_API_KEY` for `gemini`.
