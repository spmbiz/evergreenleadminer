# Hospitality World Harvest Roadmap

## Mission

Keep the hospitality fleet productively harvesting for hours/days without ChatGPT creating one-off regions. The system should progressively cover the commercial map, prioritize markets likely to buy AI property-video production, preserve every useful result, and revisit only when refresh value justifies it.

The target is not maximum HTTP requests. The target is maximum MASTER-net-new useful hospitality accounts per unit of fleet time.

## What a geographic job actually is

The primary discovery source is Overture Places. A worker receives a geographic bounding box (bbox), runs a bulk DuckDB query against Overture parquet, cheap-screens hospitality/operator candidates, then live-verifies the surviving official sites in bounded parallelism.

This is not primarily a Google keyword-query harvester. Search vocabulary exists inside the Overture taxonomy/category/name predicate. Geographic coverage is therefore prepared as cells, not chat-generated queries.

The fast lane currently requires a public website + public email already present in Overture before live verification. This is intentionally high-throughput and high-confidence, but it is not the final recall ceiling.

## Coverage architecture now implemented

### Layer 0 — legacy premium shards

Existing manually curated premium bboxes remain highest priority (`priority=100`). They cover proven high-yield hospitality markets such as US vacation belts, Canada resort regions, UK/Europe, Mediterranean destinations and other earlier tested zones.

These are subdivided into deterministic child cells and revisit on the legacy cadence (currently 168 hours unless overridden).

### Layer 1 — generated World Atlas

`config/hospitality_world_atlas.json` defines a global autonomous atlas.

The planner generates roughly 5-degree cells over land-market macro masks instead of waiting for GPT to invent territories. Cells are deterministic and independently checkpointed.

Atlas priorities:

- **P0** — premium/high-conversion surfaces: Mediterranean, US/Mexico/Caribbean vacation belt and similar overlays.
- **P1** — Western core: Europe, North America, Australia/New Zealand.
- **P2** — commercially interesting secondary hospitality markets: Gulf/MENA, Southeast Asia, Japan/Korea, Southern Africa/islands, South American coasts.
- **P3** — long-tail global coverage.

Unseen cells receive a very large first-pass bonus. Within that, geographic priority controls order. Recently completed cells are withheld until their tier revisit window expires.

### Layer 2 — deterministic country identity

World Atlas jobs pass `country=AUTO`. `tools/overture_v6_fastlane.py` reads `addresses[0].country` from the Overture place itself and maps common ISO alpha-2 codes to the MASTER's display conventions. This lets one atlas cell cross a border without deliberately mislabelling every result with the bbox's nominal country.

## Discovery predicate / query vocabulary

The fast lane searches Overture lodging taxonomy plus category/name signals including:

- hotel / resort / lodging;
- vacation rental / vacation home;
- holiday rental / holiday home / holiday let;
- property management / rental management;
- short-term rental / short stay;
- serviced apartment / serviced accommodation / aparthotel;
- villa rental / chalet rental / cabin rental;
- boutique hotel / luxury stay;
- operator and premium semantic terms used in scoring.

Recall is intentionally broader than final qualification. Cheap-screen scoring and live verification remain downstream so broader discovery does not weaken the final standard.

## Fleet execution order

For every planner cycle:

1. Read desired state, broker capacity and durable coverage.
2. Materialize legacy premium child cells + World Atlas cells.
3. Mark a cell useful when it is unseen, source-release changed, retryable/partial or past its tier revisit window.
4. Rank using unseen bonus + geographic priority + overdue age + recent useful yield - repeated-failure penalty.
5. Reserve GitHub slots through the global capacity broker so Hospitality does not steal active GWS/Tender capacity.
6. Launch up to the allocated matrix size (GitHub max currently 20).
7. Each hospitality VM uses `requests + 64` bounded HTTP workers. Do not increase socket concurrency merely because capacity exists.
8. Aggregate with one canonical writer, dedupe by registrable domain, persist SQLite/history/review state, release the account-wide lease.
9. Materialize Sheet-sync deltas. The connected MASTER sync performs live MASTER dedupe/readback before counting commercial net-new.
10. The continuation watchdog redispatches the next cycle while useful backlog exists.

## First-pass objective

The first autonomous campaign should exhaust all currently unseen Atlas cells in priority order before spending large compute refreshing recent low-yield cells.

Expected behavior over a long unattended window:

1. P0 unseen premium cells.
2. P1 unseen Western-core cells.
3. P2 unseen secondary commercial cells.
4. P3 unseen long-tail world cells.
5. Retryable failures.
6. Revisit cells only when their configured cadence becomes due.

A high-priority cell can be revisited before a very low-priority cell only after its revisit window has actually elapsed.

## Next recall lanes after Atlas fast-lane first pass

The World Atlas solves geographic backlog, not total source recall. After fast-lane coverage is healthy, add these lanes in order.

### Lane B — website-first, email-missing

Query the same geography for relevant hospitality/operator places with an official website even when Overture has no email. Deduplicate domains before HTTP. Crawl the official site/contact page with bounded concurrency to recover public business email and official Instagram/Facebook links. Never guess emails.

This should be a separate queue because it is materially more expensive than the zero-HTTP fast lane.

### Lane C — operator-first / multi-property resolution

Prefer vacation-rental managers, villa managers, serviced accommodation groups, boutique hotel groups and other multi-property operators. Collapse property discoveries to operator accounts where supported while retaining exact property/listing evidence. This is likely higher commercial value than endlessly adding individual low-ticket properties.

### Lane D — public source expansion

When Overture marginal new-unique falls, add source families instead of simply revisiting the same Atlas faster:

- OpenStreetMap / Geofabrik hospitality and tourism objects;
- official tourism/accommodation registries where bulk/public access is permitted;
- national/regional licensed short-stay or hotel registries;
- public vacation-rental/property-manager directories;
- official association/member directories;
- other public bulk datasets with stable entity identifiers.

Each new source needs its own source state, cursor/release, quality metrics and dedupe before expensive enrichment.

## Adaptive subdivision roadmap

The fixed 5-degree Atlas is the durable baseline. Dense cells should later self-split when evidence shows truncation or poor restartability.

Split a completed cell into four deterministic children when one or more of these persist:

- raw query hits approach `max_rows_per_cell`;
- wall time materially exceeds normal cell runtime;
- high live-ready yield indicates useful density worth finer coverage;
- source query is partial/retryable due cell size.

Do not split sparse zero-yield cells. Child completion should supersede the saturated parent's refresh priority.

## Yield-aware exploration / exploitation

After the first global pass, use marginal yield to allocate revisits:

- high new-unique/minute and healthy source -> revisit sooner;
- high raw/live but near-100% canonical duplicate -> revisit later and seek new sources;
- high 429/timeout local to a source -> reduce that source's pressure without shrinking unrelated workloads;
- repeated zero-yield Atlas cells -> very long revisit cadence;
- multi-property/operator-rich cells -> exploration bonus for adjacent cells and source lanes.

Never let historical yield completely suppress never-scanned coverage.

## Metrics that matter

Per cell and per cycle retain at least:

- raw Overture rows;
- fast-ready candidates;
- live HIGH/MEDIUM/live-ready;
- public emails/sites/official Instagram;
- worker wall time;
- 429 / timeout / error rates;
- CI-new domains;
- MASTER-new accounts after Sheet sync;
- duplicate rate;
- source release;
- first/last success and consecutive failures.

The primary business KPI is **MASTER-new useful accounts per worker-minute**, not raw rows.

## Persistence invariants

- Workers are disposable; partitions are not.
- Coverage cell keys are deterministic.
- Canonicalization is single-writer.
- A cell may safely rerun without creating duplicate canonical leads.
- The Google Sheet `Enriched Leads` remains the final commercial canonical database.
- CI-new is never reported as MASTER-new until Sheet dedupe/readback succeeds.
- Queue files are deleted only after their records are conclusively appended+verified or rejected as already-known/invalid.

## Current operating defaults

- GitHub account-wide hosted capacity: brokered, up to 20 jobs when actually available.
- Hospitality intra-VM verifier: `requests`, 64 threads.
- World Atlas base resolution: 5 degrees.
- Fast-lane max rows per Atlas cell: 125,000.
- Legacy premium revisit: ~168 h.
- Atlas P0 revisit: ~240 h.
- Atlas P1 revisit: ~336 h.
- Atlas P2 revisit: ~504-600 h.
- Atlas P3 revisit: ~720 h.
- CircleCI is not part of the active fleet unless explicitly re-enabled later.

## Definition of success for the unattended campaign

A multi-hour unattended run is working correctly when:

- the planner consistently has many more useful cells than available GitHub slots;
- each completed wave advances durable Atlas coverage;
- the continuation watchdog starts the next wave without GPT;
- no sibling GWS/Tender work is starved by a blind 20-slot claim;
- canonical duplicate rate is measured rather than hidden;
- MASTER sync continuously consumes verified queue chunks;
- once geographic new-unique yield falls, the system moves to new source/recall lanes rather than simply hammering the same map faster.
