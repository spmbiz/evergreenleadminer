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

For hospitality, also treat the AI Property Global Lead Graph Google Sheet `Enriched Leads` as the final commercial canonical lead database. CI persistence is durable acquisition state; the Sheet sync consumes durable queue chunks and must perform final MASTER dedupe/readback before a record is counted as commercial net-new.

## Throughput objective

Optimize **maximum useful end-to-end throughput**, not raw request volume and not conservative idle compute.

Prefer capacity for workloads with the best recent marginal yield, while keeping source-specific health limits. Track at least:

- raw records/minute;
- live/qualified records/minute;
- new unique/minute;
- duplicate rate;
- canonical pre-HTTP rejects;
- public contact recovery rate;
- 429 rate;
- timeout/error rate;
- worker wall time;
- queue/coverage progress;
- actual upstream source release/version.

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
- disabled providers must not be counted as usable capacity.

## Parallelism

Use two levels:

1. CI/cloud jobs.
2. Bounded intra-runner concurrency appropriate to the workload.

**Empirical hospitality rule as of 2026-08-15:** scale horizontally before increasing HTTP concurrency. The identical-input benchmark on BC/Alberta showed the existing `requests` verifier at 64 threads produced materially more valid live-ready records and Instagram extraction than aiohttp at 128/192 concurrency, with no meaningful speed penalty. Therefore production hospitality stays on `requests + 64 threads` until a later benchmark proves a replacement is both faster and quality-equivalent.

Do not infer that a higher socket count is faster. Benchmark on the same input and compare useful survivor count, social extraction, 429/timeouts and wall time.

Browser workloads must remain much lower. CPU-bound work should respect actual CPU capacity.

Use source-specific throttles. A saturated source must not force the whole fleet to shrink; reallocate capacity to useful healthy work.

## World Atlas coverage doctrine

Hospitality must not depend on GPT inventing the next city/region. The durable geographic backlog is generated from two layers:

1. existing hand-picked premium parent bboxes, kept as highest-priority legacy coverage;
2. `config/hospitality_world_atlas.json`, which generates a global ~5-degree Atlas over land-market macro masks.

Atlas jobs use `country=AUTO`; Overture discovery reads the actual country from the place address and maps common ISO alpha-2 codes to commercial display names.

Priority order for an unattended first pass:

1. P0 unseen premium cells;
2. P1 unseen Western-core cells;
3. P2 unseen secondary commercial cells;
4. P3 unseen global long-tail cells;
5. retryable failures;
6. stale refresh only after the cell's configured revisit cadence.

The Atlas exists to create a very large deterministic backlog so a 20-worker fleet can run for hours/days without rescanning the same few regions. Never reduce this to one-off chat-created shards again.

`HOSPITALITY_WORLD_HARVEST_ROADMAP.md` is the geographic roadmap. `HOSPITALITY_MULTISOURCE_OPERATING_RULES.md` is the current source/provenance doctrine and overrides older roadmap wording where implementation has advanced.

## Grid sharding before more sockets

Large geographic bboxes are poor units for a high-concurrency fleet. Use deterministic grid cells via `tools/hospitality_grid_plan.py` so large parents become independent, checkpointable work.

Grid rules:

- cell identities and coverage keys must be deterministic;
- do not double-count child-cell discoveries already known from parent scans;
- grid expansion increases parallelizable coverage, not source novelty;
- if net-new yield collapses after cells overlap already-scanned parents, the next bottleneck is new geographies/sources/releases, not more workers;
- smaller cells should reduce max-row truncation and improve restartability;
- dense Atlas cells should eventually self-split when max-row pressure, wall time or useful density proves the fixed cell is too coarse;
- sparse zero-yield cells should get a longer revisit cadence, not more subdivisions.

Measured proof: a 20-cell hospitality benchmark completed 20/20 workers successfully and produced 78,516 raw rows, 2,317 fast-ready, 1,547 live-ready and 682 official-site Instagram profiles while keeping 429 under 1%. This validates horizontal grid execution, not a promise that those survivors are all canonical net-new.

## Discovery and recall doctrine

### Overture Fast Email

The Overture fast lane is deliberately cheap: relevant public website + usable public email in Overture before live verification. Discovery vocabulary includes lodging taxonomy plus vacation/holiday rentals, holiday homes/lets, property/rental management, short-term stays, serviced apartments/accommodation, aparthotels, villa/chalet/cabin rentals, boutique hotels and luxury stays.

Broader recall must not weaken final qualification. Cheap-screen and live verification remain downstream.

### Overture Site Recovery

The website-first/email-missing lane is implemented and production-proven. It MUST have independent coverage keys from Fast Email.

`tools/overture_v6_site_recovery.py` discovers relevant Overture businesses with a public website but no usable Overture email. Before HTTP, filter already-canonical domains with `tools/filter_canonical_domains.py`. Then `tools/v6_public_contact_enrich.py` may inspect only a bounded set of public first-party pages.

Site-recovery invariants:

- no login, form submission, authentication or CAPTCHA bypass;
- no browser/JS automation for contact recovery unless separately reviewed;
- reject private/link-local/loopback/reserved network destinations before redirects;
- remain on the candidate registrable first-party domain;
- cap pages and bytes per domain;
- persist only explicitly published business emails/socials/contact pages;
- never infer email patterns.

Florida Panhandle canary proof: 2,427 website records -> 123 recovery candidates -> 28 public emails recovered -> 27 live-ready, with acceptable crawler health.

### Hospitality-only property-management gate

Generic long-term property management is not a valid hospitality lead merely because a site says `rental` or `management`.

If current identity is generic property management, `tools/v6_live_verify.py` must find explicit current short-stay evidence such as vacation/holiday/short-term/nightly/serviced-apartment/aparthotel/villa/cabin/chalet rental. Otherwise reject as `GENERIC_PROPERTY_MANAGEMENT_NOT_SHORT_STAY` before canonicalization.

## Canonical pre-HTTP filtering

Every expensive HTTP lane should receive a read-only snapshot of currently canonical domains and reject known domains before website verification/contact crawl.

The snapshot is an optimization only. A stale or missing snapshot may cause extra work, but it must never be trusted as the final append authority. The single canonical writer still dedupes every survivor.

Measured proof: one production US cell removed 176 of 357 known domains before HTTP, eliminating roughly half of otherwise wasted site checks.

## Multi-source hospitality doctrine

All sources normalize into the same row contract, pass the same qualification gates, and end at the same single SQLite canonical writer. **Never create an independent commercial database per source.**

The unified planner is `tools/hospitality_master_plan.py`. It combines:

- Overture geographic Fast Email;
- Overture geographic Site Recovery;
- approved AllThePlaces source tasks;
- approved OSM/Geofabrik source tasks;
- future source adapters only after canary promotion.

External source tasks are exploration slots. Overture World Atlas remains the coverage backbone. External-source caps must prevent source experiments from starving geographic work.

A source task becomes useful when its upstream release/version changed, it has never completed, it is retryable, or its revisit window is due. Source-version lookup failure must degrade to geographic work instead of stopping the fleet.

### AllThePlaces

Use the official per-spider endpoint:

`https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson`

The endpoint redirects to that spider's latest successful output. Persist the **actual redirected run** because a spider may lag the global ATP run.

Do NOT reintroduce the rejected bulk transports without a new canary:

- giant weekly `output.zip`: the upstream did not support HTTP Range in the 2026-08-15 probe;
- advertised Parquet URL: the live 2026-08-15 probe returned 404.

ATP provenance classes:

1. `first_party` — published property/operator site is first-party; explicit public contact may be used but still passes live qualification.
2. `trusted_directory_contact` — a public directory publishes a member email. The directory domain is provenance only. A non-free published email domain may become a candidate first-party site, but MUST pass current live hospitality identity before canonicalization.
3. `roster_only` — valuable property/member evidence without a safe direct contact/domain. Never append directly; preserve as resolver evidence.

A directory root domain must never become the canonical domain for every member property.

Wanderhotels canary proof: 56 members -> 55 canonical-unknown after prefilter -> 51 current live hospitality sites; 48 official-site Instagram profiles. This spider is production-enabled as `trusted_directory_contact`.

Leading Hotels of the World canary proof: 471 premium properties but no safe direct member domain/contact in the sampled structure. Keep LHW `roster_only`; do not canonicalize `lhw.com` as individual hotel domains.

Current ATP source cap is bounded (`config/atp_hospitality_spiders.json`) and must stay small unless multiple source classes independently prove high marginal MASTER-new yield.

### OpenStreetMap / Geofabrik

Use the official Geofabrik JSON index and small country/subregion PBF extracts. Never schedule continent-scale downloads simply because they exist.

Each production extract must have:

- a configured byte cap;
- a real canary;
- an independently checkpointed source version;
- canonical pre-HTTP filtering;
- the same live/recovery qualification gates.

Read only public OSM hospitality/contact tags. Require a published website. Explicit compatible public email goes to the fast live gate; website-only goes to bounded first-party recovery. Never infer contacts.

Version Geofabrik work from stable HTTP metadata such as normalized ETag + Last-Modified + Content-Length. Normalize quoted/weak ETags before storing/comparing them so shell/workflow quoting cannot make an unchanged extract look like a new release.

Monaco canary proof: 689,377-byte PBF -> 15 hospitality domains -> 6 direct-email candidates -> 5 canonical-unknown -> 5/5 live-ready. Website-only recovery added no emails in that canary. Monaco is production-enabled; additional extracts remain test-then-scale.

## Source promotion protocol

Never production-enable a new source/spider/extract merely because the endpoint exists.

Required sequence:

1. static/syntax validation;
2. real transport/schema probe;
3. isolated source canary with no canonical writes;
4. canonical-domain prefilter;
5. standard live qualification;
6. provenance and health inspection;
7. production-enable only the proven class/spider/extract;
8. unified production smoke through the real single writer;
9. persist actual source release + coverage + metrics.

If an architectural assumption fails, remove the rejected transport/design rather than leaving dead production paths that a future maintainer could reactivate accidentally.

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

## Sheet sync

`Enriched Leads` in the MASTER Google Sheet is the final commercial canonical database.

Hospitality CI must materialize durable Sheet-sync queue chunks after canonical updates. The Sheet consumer must:

- read live MASTER state first;
- dedupe domain strongest, then normalized phone, then name+market+country;
- append only genuinely new useful rows;
- update `Dedupe Index`;
- read back every appended row and dedupe entry;
- log the sync pass;
- delete a queue chunk only after every record is conclusively appended+verified or rejected as already-known/invalid.

Always distinguish `CI-net-new` from `MASTER-net-new` until this final sync has succeeded.

The direct Sheet write path is empirically proven with a full 36-column append + Dedupe Index write + exact readback. Do not infer from that proof that an hourly scheduler has run; inspect the automation's `last_run_time` and the actual Sheet tail before claiming continuous sync health.

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

Harvest logic must be provider-neutral where practical, but only providers explicitly enabled and persistence-safe may receive work.

### GitHub

GitHub hospitality and GWS consume the account-wide broker rather than each assuming the full hosted concurrency limit.

### CircleCI

CircleCI is currently disabled/ignored for production scheduling. Do not count it in available capacity or launch work there unless the user explicitly re-enables that provider and persistence is verified again.

## Current hospitality implementation

Primary deterministic runtime: `tools/fleet_runtime.py`

Unified source + geography planner: `tools/hospitality_master_plan.py`

Phase-aware Overture multi-lane planner: `tools/hospitality_multilane_plan.py`

World/grid planner: `tools/hospitality_grid_plan.py`

Provider-neutral Overture worker: `tools/hospitality_worker.py`

Fast Overture discovery: `tools/overture_v6_fastlane.py`

Overture website-first discovery: `tools/overture_v6_site_recovery.py`

Bounded first-party contact crawler: `tools/v6_public_contact_enrich.py`

Current live hospitality verifier: `tools/v6_live_verify.py`

Pre-HTTP canonical-domain filter: `tools/filter_canonical_domains.py`

ATP source policy: `config/atp_hospitality_spiders.json`

ATP per-spider adapter: `tools/atp_spider_hospitality.py`

ATP production worker: `tools/hospitality_atp_worker.py`

Geofabrik source policy: `config/osm_geofabrik_sources.json`

OSM/Geofabrik adapter: `tools/geofabrik_osm_hospitality.py`

OSM/Geofabrik production worker: `tools/hospitality_osm_worker.py`

World Atlas config: `config/hospitality_world_atlas.json`

World roadmap: `HOSPITALITY_WORLD_HARVEST_ROADMAP.md`

Multi-source doctrine: `HOSPITALITY_MULTISOURCE_OPERATING_RULES.md`

Safe domain aggregate wrapper: `tools/hospitality_fleet_aggregate.py`

Cloud canary autoscaler: `tools/hospitality_scheduler.py`

Global GitHub broker: `tools/global_capacity_broker.py`

24/7 workflow: `.github/workflows/hospitality-autonomous-fleet.yml`

Continuation watchdog: `.github/workflows/autonomous-fleet-continuation.yml`

Sheet queue bridge: `.github/workflows/hospitality-sheet-queue-bridge.yml`

Sheet queue materializer: `tools/hospitality_sheet_queue.py`

Desired state: `control/desired_state.json`

Provider config: `config/providers.json`

Global allocation config: `config/global_fleet.json`

Fleet config: `config/fleet.json`

## Status response

When the user asks `status?` / `on en est où?`, read durable state first and report the latest completed cycle, active/queued capacity, current global leases, source/lane mix, new unique, qualified/live-ready, canonical pre-HTTP rejects, errors/429/timeouts, next cloud/local concurrency, backlog/coverage and any persistence/sync issue.

Always distinguish `CI-net-new` from `MASTER-net-new` until the final Google Sheet dedupe/sync has happened.
