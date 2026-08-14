# Autonomous Harvest Fleet

This repository treats harvesting as durable desired state rather than a chat-driven action.

## Control

`control/desired_state.json`

- `enabled=true` means scheduled GitHub cycles may create new work.
- `enabled=false` means no new scheduled harvest work is planned.
- workers are bounded and restartable; no eternal runner is required.

## GitHub pool

- verified Free-plan hosted concurrency: 20
- default worker target: 18
- 2 slots remain available for planner/aggregator/control
- schedule: every 15 minutes
- each worker reuses the existing Overture V6 + live verifier
- live website verification keeps adaptive local concurrency (32–96, initial 64)

## CircleCI pool

The repo contains a provider-neutral CircleCI configuration prepared for 28 parallel workers.
It is intentionally disabled in `config/providers.json` until the actual CircleCI project/org is
verified and `FLEET_GH_TOKEN` is configured. CircleCI workers never become a second canonical
writer: they publish immutable inbox bundles to the `harvest-inbox` release; GitHub remains the
single canonical writer.

## Persistence

Large durable state is stored as GitHub Release assets under `harvest-state`:

- `canonical.sqlite.gz`
- immutable `partition-<cycle>.jsonl.gz`
- GPT review assets when produced

Small state is committed to Git:

- `state/coverage.json`
- `state/checkpoints.json`
- `state/source_state.json`
- `state/provider_capacity.json`
- `metrics/latest.json`
- `metrics/history.jsonl`
- `gpt/latest_summary.json`
- `gpt/pending_batches.json`

Worker transfer artifacts use one-day retention and are never the only surviving copy after
successful aggregation.

## Incremental behavior

The planner inventories the repository's existing shard trigger/wave files, deduplicates territory,
and prioritizes never-scanned, failed/retryable, release-changed, then stale shards. Recently
successful shards are not repeatedly downloaded until stale.

## GPT handoff

GPT is not required for harvesting. On return, read `gpt/latest_summary.json`,
`gpt/pending_batches.json`, and the durable release state instead of reconstructing from chat.
