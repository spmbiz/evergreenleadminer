# Autonomous Harvest Fleet

This repository treats harvesting as durable desired state rather than a chat-driven action. GPT is a controller/reviewer, not a required worker process.

The current fleet has two workload families:

- **Hospitality / short-stay** via `tools/fleet_runtime.py` and the existing Overture V6 + live verifier.
- **GWS Brussels no-website** via `tools/gws_fleet_plan.py`, `tools/gws_fleet_worker.py`, and `tools/gws_fleet_aggregate.py`.

## Control

`control/desired_state.json` is the durable global switch.

- `enabled=true` and `continuous=true`: scheduled providers may create new useful work.
- `enabled=false`: no new fleet work should be launched.
- `mode=maximum`: use healthy available capacity aggressively rather than a permanent low worker cap.
- workers are bounded/restartable; no eternal runner or open ChatGPT tab is required.

Provider limits and activation gates live in `config/providers.json`.

## GitHub pool

Verified current Free-plan standard hosted concurrency is **20 jobs**. On this public repository, standard GitHub-hosted runners are free/unlimited.

The GWS fleet uses a source canary rather than blindly starting at maximum:

`8 -> 12 -> 16 -> 20` on GitHub (and potentially higher on a larger provider) when net-new post-dedupe review yield remains healthy and errors/429s stay low.

The planner subtracts visible active/queued work from this repository and configured peer repositories. Planner and aggregator are dependency-serialized with the worker phase, so they do not require two permanently idle slots; when the account is genuinely free the configured worker target is 20.

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

The hospitality runtime keeps large durable state in GitHub Release assets under `harvest-state` (canonical SQLite + immutable partitions/review bundles) and small checkpoint/coverage/GPT state in Git. CircleCI uploads immutable provider bundles to `harvest-inbox` rather than acting as a second canonical writer.

## CircleCI second pool

Current CircleCI Free plan reference points are:

- **30 concurrent cloud jobs**;
- up to **400,000 Linux open-source credits/month** for a public project on the Free plan;
- Docker `medium`: **10 credits/minute** (current price list).

The fleet is configured for a potential **30-worker burst**, not 28. There is no permanent two-slot reserve because the inbox/control job runs after the parallel worker job completes.

The CircleCI config is dynamic (`setup: true`): it plans first and continues with exactly the selected parallelism. It does **not** start 30 empty containers when only 8 useful tasks exist.

Credit policy:

- `auto_purchase=false`;
- budget = 400,000 OSS Linux credits/month;
- guard = 95% of that budget;
- runtime estimation is persisted because CircleCI documents that the OSS credit allocation is not exposed like a normal balance.

CircleCI remains disabled until the real organization/project is verified. Activation requires:

1. connect `walidgdg1-ai/evergreenleadminer` to the intended CircleCI Free organization;
2. confirm the public repo receives the OSS allocation;
3. enable Dynamic Config / setup workflows in CircleCI project settings;
4. set `FLEET_GH_TOKEN` so CircleCI can persist its immutable inbox before containers disappear;
5. create an explicit scheduled/API pipeline with `fleet=true` and workload `hospitality` or `gws`.

Normal VCS pushes have `fleet=false`, preventing recursive harvest loops from state commits.

CircleCI is deliberately **not a canonical writer**. Each CircleCI GWS wave uploads one immutable `circleci-gws-inbox-*.tar.gz` bundle to the `harvest-inbox` GitHub Release. `.github/workflows/gws-circleci-inbox-aggregate.yml` serializes with the GitHub GWS writer, ingests those bundles, performs the same deterministic dedupe/change detection/GPT handoff, then deletes the temporary inbox asset only after successful canonical persistence.

## Backpressure / self-healing

- GWS tasks are incremental by territory/category and refresh watermark.
- retryable failures remain eligible for a later cycle.
- missing/failed workers do not erase prior progress.
- review-queue soft/hard limits throttle discovery before GPT becomes the bottleneck.
- source-specific concurrency scales down on errors/429s rather than shrinking unrelated workloads.
- temporary CI artifacts are one-day transport only; useful data is persisted before they expire.

## GPT handoff

When GPT returns, it should read the small workload summaries and pending-batch indexes first. It should review only new semantic/strict batches, not replay deterministic scraping/parsing/dedupe work.
