---
name: autonomous-harvest
purpose: Operate AI Prod harvesting systems as persistent autonomous maximum-throughput fleets rather than one-off chat-driven runs.
---

# Autonomous Harvest Skill

Use this skill for hospitality / short-stay lead harvesting, GWS no-website harvesting, tenders, and similar high-volume acquisition systems.

## Core semantic rule

When the user says **run continuously / 24/7 / keep going**, do not merely launch one workflow. Persist the desired state and ensure a scheduler can continue without ChatGPT open.

When the user says **stop**, persist `enabled=false`; do not launch new work. Safe in-flight work may checkpoint and finish.

## Required operating order

Before changing or launching a fleet:

1. Inspect `control/desired_state.json`.
2. Inspect `config/providers.json`, `config/global_fleet.json`, and workload config.
3. Inspect recent workflow runs, active jobs, queued jobs, and global capacity leases.
4. Inspect latest metrics, coverage/checkpoint state and GPT handoff.
5. Confirm canonical persistence and dedupe state.
6. Only then allocate workers.

For hospitality, also treat the AI Property Global Lead Graph Google Sheet `Enriched Leads` as the final commercial canonical lead database. CI persistence is durable acquisition state; GPT may sync qualified new partitions to the Sheet after deterministic dedupe.

## Throughput objective

Optimize **maximum useful end-to-end throughput**, not raw request volume and not conservative idle compute.

Prefer capacity for workloads with the best recent marginal yield, while keeping source-specific health limits. Track at least:

- raw records/minute;
- live/qualified records/minute;
- new unique/minute;
- duplicate rate;
- 429 rate;
- timeout/error rate;
- worker wall time;
- queue/coverage progress.

Do not create paid usage without explicit authorization. Never evade quotas or create extra accounts to obtain more capacity.

## Global capacity broker

GitHub-hosted capacity is account-wide shared infrastructure, not a private pool for one workflow.

Hospitality and GWS must reserve GitHub slots through `tools/global_capacity_broker.py`; planners share the `ai-prod-global-capacity-broker` concurrency group. `config/global_fleet.json` defines workload weights, floors and ceilings. Tender-engine remains external-observed until it consumes leases directly, so its real active/queued jobs are subtracted before allocation.

Rules:

- never assume `20 GitHub slots` means `20 slots for each repo`;
- reserve atomically before building a worker matrix;
- protect useful sibling workload headroom when both have backlog;
- allocate zero slots when a workload has zero useful backlog;
- release the lease after aggregate or let TTL recover from a dead workflow;
- treat CircleCI as a separate provider pool, never as extra GitHub capacity.

## Parallelism

Use two levels:

1. CI/cloud jobs.
2. Bounded intra-runner concurrency appropriate to the workload.

**Empirical hospitality rule as of 2026-08-15:** scale horizontally before increasing HTTP concurrency. The identical-input benchmark on BC/Alberta showed the existing `requests` verifier at 64 threads produced materially more valid live-ready records and Instagram extraction than aiohttp at 128/192 concurrency, with no meaningful speed penalty. Therefore production hospitality stays on `requests + 64 threads` until a later benchmark proves a replacement is both faster and quality-equivalent.

Do not infer that a higher socket count is faster. Benchmark on the same input and compare useful survivor count, social extraction, 429/timeouts and wall time.

Browser workloads must remain much lower. CPU-bound work should respect actual CPU capacity.

Use source-specific throttles. A saturated source must not force the whole fleet to shrink; reallocate capacity to useful healthy work.

## Grid sharding before more sockets

Large geographic bboxes are poor units for a high-concurrency fleet. Use deterministic grid cells via `tools/hospitality_grid_plan.py` so large parents become independent, checkpointable work.

Grid rules:

- cell identities and coverage keys must be deterministic;
- do not double-count child-cell discoveries already known from parent scans;
- grid expansion increases parallelizable coverage, not source novelty;
- if net-new yield collapses after cells overlap already-scanned parents, the next bottleneck is new geographies/sources/releases, not more workers;
- smaller cells should reduce max-row truncation and improve restartability.

Measured proof: a 20-cell hospitality benchmark completed 20/20 workers successfully and produced 78,516 raw rows, 2,317 fast-ready, 1,547 live-ready and 682 official-site Instagram profiles while keeping 429 under 1%. This validates horizontal grid execution, not a promise that those survivors are all canonical net-new.

## Canary autoscaling

When cloud/source concurrency is uncertain, scale through measured steps rather than jumping blindly to maximum. Promote only after healthy measured cycles; demote on material 429/timeouts/errors or collapsing marginal throughput.

Persist the learned recommendation so each cycle does not relearn from zero.

Distinguish:

- worker/job health;
- per-site HTTP failures;
- source throttling;
- canonical marginal yield.

Do not demote cloud concurrency merely because individual websites return 403/404/network errors if worker completion, 429 and timeout health remain acceptable.

## Incremental coverage

Never start from zero unless genuinely necessary. Persist territory/source/cursor/watermark state. Prioritize:

1. never scanned;
2. partial / retryable;
3. stale refresh;
4. exploration;
5. recently complete work last.

Track duplicate economics and lower priority for sources dominated by already-known entities unless refresh value justifies rescanning.

## Persistence and idempotence

Workers are disposable; data is not.

Workers may write isolated partitions/artifacts, but canonicalization must be single-writer or transaction-safe. Re-running a shard must be safe.

Preserve useful identities, URLs, emails, phones, social profiles, source IDs, qualification evidence, timestamps and rejected/ambiguous states needed for future dedupe/change detection.

Temporary CI artifacts are transport/debug only. Persist useful data before artifacts expire.

## Leases, retry and dead letter

Logical work should support queued/running/checkpointed/completed/failed_retryable/failed_terminal states. Retry transient failures with bounded backoff. Repeated deterministic failures go to a dead-letter state; one poison record must never stall a shard.

## Backpressure

Maximum throughput means maximum completed useful records, not infinite raw discovery. If verification/enrichment/review is the bottleneck, allocate more capacity downstream or reduce discovery until queues are healthy.

Likewise, if canonical new-unique yield collapses while raw/live-ready remains high, do not reward the rediscovery loop by adding compute. Expand source universe, geographic cells not previously covered, source releases or operator datasets.

## GPT boundary

Python/SQL handles deterministic bulk work: download, parsing, filtering, normalization, exact matching, dedupe, email/social extraction and simple validation.

GPT receives only semantic/ambiguous/high-value review work and strategic decisions.

Every successful cycle must leave a tiny durable GPT-readable summary and immutable review batches so a later ChatGPT session can answer status immediately without reconstructing chat history.

## Provider model

Harvest logic must be provider-neutral. GitHub Actions, CircleCI and optional self-hosted runners should execute the same worker entrypoints.

### GitHub

GitHub hospitality and GWS consume the account-wide broker rather than each assuming the full hosted concurrency limit.

### CircleCI

CircleCI compute connectivity and static parallel workflows have been proven. Dynamic Config is not required and should not be a critical dependency. The production `.circleci/config.yml` is static and pipeline-parameter driven; ordinary VCS pushes keep `fleet=false` and must not launch harvest waves.

CircleCI is compute/transport, not a second canonical writer. Hospitality bundles go to GitHub Release `harvest-inbox`; `.github/workflows/hospitality-circleci-inbox-aggregate.yml` performs single-writer canonical ingestion on GitHub. GWS uses the corresponding GWS inbox aggregator.

CircleCI fleet execution must preflight persistence before expensive work. `FLEET_GH_TOKEN` must exist in CircleCI with write access needed to upload immutable inbox bundles. If it is absent, refuse the fleet wave rather than harvest into ephemeral containers.

`auto_purchase` must remain false unless the user explicitly authorizes spend. Do not assume the advertised/open-source credit allocation is granted to this project until account/project billing state confirms it.

## Current hospitality implementation

Primary deterministic runtime: `tools/fleet_runtime.py`

Safe provider-neutral worker: `tools/hospitality_worker.py`

Grid planner: `tools/hospitality_grid_plan.py`

Safe domain aggregate wrapper: `tools/hospitality_fleet_aggregate.py`

Cloud canary autoscaler: `tools/hospitality_scheduler.py`

Global GitHub broker: `tools/global_capacity_broker.py`

24/7 workflow: `.github/workflows/hospitality-autonomous-fleet.yml`

CircleCI inbox consumer: `tools/hospitality_circleci_inbox.py`

CircleCI inbox workflow: `.github/workflows/hospitality-circleci-inbox-aggregate.yml`

Desired state: `control/desired_state.json`

Provider config: `config/providers.json`

Global allocation config: `config/global_fleet.json`

Fleet config: `config/fleet.json`

## Status response

When the user asks `status?` / `on en est où?`, read durable state first and report the latest completed cycle, active/queued capacity, current global leases, new unique, qualified/live-ready, errors/429/timeouts, next cloud/local concurrency, backlog/coverage and any persistence/sync issue.

Always distinguish `CI-net-new` from `MASTER-net-new` until the final Google Sheet dedupe/sync has happened.
