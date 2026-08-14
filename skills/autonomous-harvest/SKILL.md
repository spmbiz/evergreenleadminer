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
2. Inspect `config/providers.json` and workload config.
3. Inspect recent workflow runs, active jobs and queued jobs.
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

## Parallelism

Use two levels:

1. CI/cloud jobs.
2. Bounded intra-runner concurrency appropriate to the workload.

Network-heavy verification can use high async/thread concurrency when healthy. Browser workloads must remain much lower. CPU-bound work should respect actual CPU capacity.

Use source-specific throttles. A saturated source must not force the whole fleet to shrink; reallocate capacity to useful healthy work.

## Canary autoscaling

When cloud/source concurrency is uncertain, scale through measured steps rather than jumping blindly to maximum. Promote only after healthy measured cycles; demote on material 429/timeouts/errors or collapsing marginal throughput.

Persist the learned recommendation so each cycle does not relearn from zero.

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

## GPT boundary

Python/SQL handles deterministic bulk work: download, parsing, filtering, normalization, exact matching, dedupe, email/social extraction and simple validation.

GPT receives only semantic/ambiguous/high-value review work and strategic decisions.

Every successful cycle must leave a tiny durable GPT-readable summary and immutable review batches so a later ChatGPT session can answer status immediately without reconstructing chat history.

## Provider model

Harvest logic must be provider-neutral. GitHub Actions, CircleCI and optional self-hosted runners should execute the same worker entrypoints.

CircleCI remains disabled until real project eligibility/credits/authentication are verified. `auto_purchase` must remain false unless the user explicitly authorizes spend.

## Current hospitality implementation

Primary runtime: `tools/fleet_runtime.py`

Safe domain aggregate wrapper: `tools/hospitality_fleet_aggregate.py`

Cloud canary autoscaler: `tools/hospitality_scheduler.py`

24/7 workflow: `.github/workflows/hospitality-autonomous-fleet.yml`

Desired state: `control/desired_state.json`

Provider config: `config/providers.json`

Fleet config: `config/fleet.json`

## Status response

When the user asks `status?` / `on en est où?`, read durable state first and report the latest completed cycle, active/queued capacity, new unique, qualified/live-ready, errors/429/timeouts, next cloud/local concurrency, backlog/coverage and any persistence/sync issue.
