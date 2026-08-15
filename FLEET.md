# Autonomous Harvest Fleet

This repository treats harvesting as durable desired state rather than a chat-driven action. GPT is a controller/reviewer, not a required worker process.

## Project skill / source of operating doctrine

The reusable project operating skill is:

`skills/autonomous-harvest/SKILL.md`

Treat it as a first-class source before planning, launching, scaling, stopping, debugging, or reporting any high-volume harvester in this project. It captures the persistent 24/7 semantics, incremental coverage, provider-neutral workers, canary autoscaling, single-writer persistence, backpressure, retry/dead-letter rules, and GPT handoff contract.

The current fleet has two workload families:

- **Hospitality / short-stay** via `tools/fleet_runtime.py`, `tools/hospitality_worker.py`, `tools/hospitality_scheduler.py`, and the existing Overture V6 + live verifier.
- **GWS Brussels no-website** via `tools/gws_fleet_plan.py`, `tools/gws_fleet_worker.py`, and `tools/gws_fleet_aggregate.py`.

## Control

`control/desired_state.json` is the durable global switch.

- `enabled=true` and `continuous=true`: scheduled providers may create new useful work.
- `enabled=false`: no new fleet work should be launched.
- `mode=maximum`: use healthy available capacity aggressively rather than a permanent low worker cap.
- workers are bounded/restartable; no eternal runner or open ChatGPT tab is required.

Provider limits and activation gates live in `config/providers.json`.

## GitHub pool

The hospitality autonomous workflow is `.github/workflows/hospitality-autonomous-fleet.yml`. It wakes every 15 minutes, uses a durable cloud canary ladder `4 -> 8 -> 12 -> 16 -> 20`, and never cancels productive in-flight work.

Cloud scaling and local HTTP pressure are intentionally separate. Cloud jobs cover disjoint geographic shards; source/site 429s and timeouts can tune intra-runner concurrency without unnecessarily idling unrelated cloud shards.

The GWS fleet uses its own measured source canary rather than blindly starting at maximum:

`8 -> 12 -> 16 -> 20` when net-new post-dedupe review yield remains healthy and errors/429s stay low.

The planner subtracts visible active/queued work from this repository and configured peer repositories. Planner and aggregator are dependency-serialized with the worker phase, so they do not require permanently idle slots.

`.github/workflows/gws-autonomous-fleet.yml` wakes every 15 minutes (offset from the hour), uses `queue: max` and never cancels productive in-flight work.

## GWS source efficiency

The Hub Brussels source snapshot is downloaded **once per cycle**, integrity-checked and shared with all shards. Workers do not each redownload the same source universe.

Each GWS shard is `territory x business-family` and queries only a tight Overture bbox around its Hub targets. South Brussels is intentionally prioritized first: Uccle, Ixelles, Saint-Gilles, Forest, Auderghem and Watermael-Boitsfort.

Workers do deterministic work at scale:

- materialize identities;
- resolve current Overture entities;
- identify obvious owned websites and chains;
- normalize evidence;
- produce deterministic record keys;
- checkpoint metrics/status.

**CI does not equate “no website in Overture” with strict `VERIFIED_NO_WEBSITE`.** Current resolved businesses without an owned-domain signal go to an immutable GWS review batch. This preserves the strict HIGH standard already used in the Google Sheet MASTER.

Final strict GWS canonical store:

`https://docs.google.com/spreadsheets/d/1pYzNHoEepjmZ1GK0YpkMV3fG5wMteZ0JECx1fUzKEm8/edit`

## GWS durable state

Small durable state is committed:

- `state/gws_coverage.json` — territory/category coverage and measured net-new yield.
- `state/gws_entity_index.json` — deterministic fingerprints for dedupe/change detection.
- `state/gws_source_profiles.json` — learned source concurrency and health.
- `state/gws_source_state.json` — Hub source hash/watermark.
- `metrics/gws_latest.json` and `metrics/gws_history.jsonl` — throughput/health/autoscaling.
- `gpt/gws_latest_summary.json` — tiny status file for GPT.
- `gpt/gws_pending_batches.json` — immutable review-batch index.
- `gpt/gws_review/*.jsonl` — only new/changed semantic review work.

Useful harvested observations are append-only/change-aware:

- `data/gws/observations/YYYY-MM-DD.jsonl`
- `data/gws/changes/YYYY-MM-DD.jsonl`
- `data/gws/source/*.jsonl.gz` only when the Hub snapshot content hash changes.

Unchanged duplicates do not bloat observation files and do not cause the autoscaler to scale up. Autoscaling uses **net-new post-dedupe review yield**, not rediscovered raw volume.

## Hospitality persistence

The hospitality runtime keeps large durable state in GitHub Release assets under `harvest-state` (canonical SQLite) and daily `harvest-history-*` cycle bundles (canonical delta, observations, GPT review, metrics). Small checkpoint/coverage/GPT state is committed to Git.

The final commercial hospitality canonical remains the Google Sheet `Enriched Leads`; CI acquisition state is intentionally separate until a trusted Sheet credential or explicit sync step is configured.

## CircleCI second pool

CircleCI is prepared as a secondary provider using the same provider-neutral worker code, but remains disabled until the real organization/project eligibility, free allocation and authentication are verified.

Credit policy:

- `auto_purchase=false`;
- no paid compute is authorized implicitly;
- if the verified free allocation is exhausted, CircleCI pauses while GitHub remains correct.

CircleCI is deliberately **not a canonical writer**. Provider bundles must converge through the single canonicalization path.

## Backpressure / self-healing

- hospitality and GWS tasks are incremental by territory/source coverage and refresh watermark;
- retryable failures remain eligible for a later cycle;
- missing/failed workers do not erase prior progress;
- review-queue soft/hard limits throttle discovery before GPT becomes the bottleneck;
- source-specific concurrency scales down on errors/429s rather than shrinking unrelated workloads;
- temporary CI artifacts are one-day transport only; useful data is persisted before they expire.

## GPT handoff

When GPT returns, it should read the small workload summaries and pending-batch indexes first. It should review only new semantic/strict batches, not replay deterministic scraping/parsing/dedupe work.
