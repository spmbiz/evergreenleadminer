# AGENTS.md — GWS Harvester Agent Contract

## Read first

Before making architectural or performance changes, read:

1. `skills/autonomous-harvest/SKILL.md` for canonical fleet/persistence/throughput rules;
2. `skills/search-semantic-fleet/SKILL.md` for the **current V2 search + Qwen + incremental-intelligence architecture**;
3. `docs/HARVESTER_SCALE_BLUEPRINT.md` for broader scaling direction.

Where older local-LLM suggestions conflict with `skills/search-semantic-fleet/SKILL.md`, the newer skill wins. In particular, the current first implementation target is **parallel Qwen ~4B on GitHub-hosted runners after a seen/change ledger**, with no mandatory 0.5–0.8B tier unless measurement later proves it necessary.

### Real operating environment

This project does **not** assume OpenAI API agents. The high-level controller is ChatGPT Web (currently GPT-5.6 Sol in the user's workflow) using native GitHub and Google Drive/Sheets connectivity to launch, inspect, edit, debug and steer work. GitHub Actions / scripts are execution workers.

Design outputs for that reality: durable queues, checkpoints, JSON/CSV summaries, compact logs, deterministic executors and source-backed state that ChatGPT Web can inspect through native connectors. Do not introduce an OpenAI API dependency unless the user explicitly requests one.

## Strategic direction

Use deterministic bulk discovery and exact evidence first; use OpenSERP/DDGS/SearXNG as search-resolution tools; use Qwen ~4B only for semantic ambiguity; use GPT Web for valuable hard cases.

Target architecture:

```text
broad public discovery
  -> normalize / exact dedupe
  -> seen/change ledger
  -> new / unresolved / materially changed only
  -> OpenSERP + DDGS search fabric
  -> deterministic identity evidence
  -> parallel Qwen ~4B GitHub workers for ambiguity
  -> GPT Web for valuable / uncertain / contradictory cases
  -> canonical state
```

## Primary GWS use case

Resolve whether a candidate website truly belongs to the target business, then classify website opportunity state. Exact phone/domain/address/name evidence belongs in code; semantic ambiguity belongs in the local model.

Useful states include `NO_SITE`, `DEAD_SITE`, `BROKEN_SITE`, `PARKED_DOMAIN`, `FACEBOOK_ONLY`, `DIRECTORY_ONLY`, `ANCIENT_SITE`, `NON_MOBILE_SITE`, `NO_SSL`, `ONE_PAGE_BAD_SITE`, `BAD_CONVERSION_SITE`, `GOOD_SITE`, and `UNCERTAIN`.

Search absence never proves no website. CAPTCHA/throttle/search failure must remain explicit.

## Model/runtime direction

First benchmark a Qwen 3/3.5-class ~4B instruct GGUF under `llama.cpp` on actual GitHub-hosted runners. Cache model/runtime; do not commit model weights to Git. Process substantial shards and support compact batching. Keep the classifier interface model-agnostic for later alternatives.

Do not add a tiny-model prefilter, 9B/24B ensemble, GLM/MiniCPM voting layer, or other complexity until a measured bottleneck or recall deficiency justifies it.

## Security

This repository is public. Do not route arbitrary public-repo workflow execution to the user's personal self-hosted PC. Any personal runner should live behind a private control repository. The public repo may emit durable tasks/state for that private plane, but public PR/fork code must never gain arbitrary execution on the user's machine.

## Core philosophy

1. Code first for facts.
2. Search fabric for broad resolution.
3. Qwen ~4B for semantic ambiguity.
4. GPT Web for high-value hard cases and strategy.
5. Backfill once; process new/unresolved/changed deltas thereafter.
6. Cache what has already been learned.
7. Persist model/prompt/version provenance.
8. No hallucinated contacts or identities.
9. Measure source/query/engine/geo yield.
10. Optimize useful commercial opportunities, not raw rows.
