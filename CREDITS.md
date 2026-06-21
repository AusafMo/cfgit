# Credits & Attribution

`cfg` borrows ideas and design vocabulary from prior work. This file records what
was taken, from where, and under what license, so reuse is attributed and
license-respecting. **Process rule:** whenever code or a non-obvious design is taken
from an external project, add a row here in the same change, after actually reading
that project's LICENSE. Anything copyleft is isolated and labeled; by default `cfg`
reuses *concepts*, not source.

Legend for "Taken": **idea** = concept/design/vocabulary only (no source copied;
needs citation, not a license grant) · **code** = source incorporated (license
applies; must be isolated/labeled).

| Project | What we took | License | Taken | Obligation honored |
|---|---|---|---|---|
| [Git](https://git-scm.com) | Command vocabulary and UX (`commit`/`log`/`diff`/`show`/`restore`/`revert`/`reflog`/`tag`), the **porcelain/plumbing** split (stable machine layer vs human layer), content-addressable object identity (sha256 of canonical content, "blob"-style). | GPL-2.0 | **idea** | Attribution + design credit here. **No Git source is copied.** GPL-2.0 copyleft binds copied code, not ideas/command-names/UX; Git's own docs explicitly invite building alternative "porcelains" on its interface. If a Git source snippet is ever vendored, that file becomes GPL-2.0, is isolated under `third_party/`, and labeled: current plan: none. |
| [llm-prompt-semantic-diff](https://github.com/aatakansalar/llm-prompt-semantic-diff) | The framing that prompt changes need a *meaning-level* signal, not text diff; CLI + CI-exit-code shape. | Check repo LICENSE at vendor time (appears permissive) | **idea** | Cited as prior art for §7. Reusing only the framing; no code reused. If code is reused later, read + record its LICENSE first. |
| [llm-behavior-diff](https://dev.to/nilofer_tweets/llm-behavior-diff-model-update-detector-3e7b) | The idea of severity-classified divergence + shipping an **MCP server** so agents can run the analysis. We deliberately do NOT adopt its output-generation/eval approach (we eval reasoning/payloads, not generations). | Check repo LICENSE at vendor time | **idea** | Cited as prior art for §7 (MCP-first, severity tiers). No code reused. |
| "Prompting in the Wild: An Empirical Study of Prompt Evolution in Software Repositories" ([arXiv:2412.17298](https://arxiv.org/abs/2412.17298)) | The chain-of-thought inconsistency method: read old + new prompt → identify changed parts → for each change decide whether it introduced an inconsistency. Adopted for §7's intra-config `self_inconsistency` dimension. | Academic paper (method) | **idea** | Cited. Method adopted, not code. |

## Prior art we position against (not reused; credited for honesty)
cfgit's framing (`SPEC_CORE.md` §3) is defined by contrast with the "git for data" category. We reuse none of their code; we credit them as the prior art that defines the niche cfgit fills (the non-custodial / in-place case they explicitly do not serve).
- **[Dolt](https://github.com/dolthub/dolt)** (Apache-2.0): the most literal "git for data" (Git semantics on table rows, Prolly-tree storage). cfgit differs by being non-custodial: Dolt *is* the database; cfgit versions a database you keep. Dolt's own docs note it "won't help you if you wish to keep your data in place," which is precisely cfgit's case.
- **[TerminusDB](https://github.com/terminusdb/terminusdb)**, **[lakeFS](https://github.com/treeverse/lakeFS)**: same custodial pattern for graph/document data and object stores respectively.
- **Temporal tables** (SQL:2011 standard): built-in time-travel/rollback for a single store; cfgit adds cross-store, in-place, and drift reconciliation on top of stores that may lack it.

## Standard building blocks (influences, no license obligation)
- **sha256 / canonical-JSON hashing** for content identity: common technique (git blob, content-addressable stores).
- **Bitemporal data modeling** (`recorded_at` transaction time vs `valid_from`/`valid_to` valid time): from the data-warehousing / temporal-database literature (e.g. Snodgrass, "Developing Time-Oriented Database Applications").
- **Optimistic concurrency control / compare-and-swap** on a HEAD pointer: standard concurrency-control technique.

## `cfg`'s own license
**Apache-2.0** (see `LICENSE`). Permissive + explicit patent grant, so the company
and others can adopt freely. Apache-2.0 can depend on MIT/Apache libraries; it
**cannot** link GPL-2.0 source into the core: a second reason the Git borrowing
stays concept-only. The optional `cfg-impact` plugin keeps any model-provider SDK
out of the core package, so a differently-licensed SDK can't taint the core license.
