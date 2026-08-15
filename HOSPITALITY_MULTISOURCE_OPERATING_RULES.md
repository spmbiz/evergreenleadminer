# Hospitality Multi-Source Operating Rules

These rules are production invariants for the AI Prod hospitality lead fleet. They exist so future changes do not undo empirically validated behavior.

## Canonical hierarchy

1. Google Sheet `Enriched Leads` is the final commercial MASTER.
2. GitHub release SQLite is durable acquisition/canonicalization state for autonomous workers.
3. Workers and transport artifacts are disposable.
4. Final MASTER dedupe remains: registrable domain, then normalized public phone, then normalized business/property name + market + country.
5. `CI-new` must never be reported as `MASTER-new` until live Sheet dedupe + write/readback succeeds.

## Single-writer rule

Every discovery source normalizes into the same hospitality row contract and passes through the same live qualification gates and the same single canonical aggregate. No source may create an independent commercial database.

## Pre-HTTP dedupe

Before expensive website verification or contact recovery, ship a read-only snapshot of known canonical domains to workers and remove already-known domains. The snapshot is an optimization only; the final writer remains authoritative. A stale or unavailable snapshot may cause extra work but must never suppress a final writer check.

## Overture

### Fast-email lane

Use Overture Places for deterministic geographic coverage. Fast-email requires a usable public website + public email before live verification. Keep `requests + 64` as the production verifier until a same-input benchmark proves a replacement is both faster and quality-equivalent.

### Site-recovery lane

A separate coverage key is mandatory. Website-first records without a usable Overture email are deduped by domain before HTTP, then a bounded first-party crawler may inspect at most a few public pages. No login, forms, authentication, CAPTCHA bypass, JS automation or email inference. Reject private/link-local/loopback/reserved network destinations before redirects. Stay on the registrable first-party domain.

### Property-management quality gate

Generic long-term property management is not hospitality. If identity is generic property management, current first-party content must prove a vacation/holiday/short-term/nightly/serviced-apartment/aparthotel/villa/cabin/chalet accommodation business. Otherwise reject before canonicalization.

## AllThePlaces

Use only the documented per-spider endpoint:
`https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson`

Do not use the giant weekly `output.zip` in production; upstream did not support HTTP Range in the 2026-08-15 probe. Do not rely on an advertised Parquet URL without a live transport canary; the probed URL returned 404.

The per-spider endpoint can redirect to a spider's most recent successful run, which may be older than the global ATP run. Persist the actual redirected run as the source release.

ATP provenance modes:

- `first_party`: published property/operator site is first-party; explicit public email is optional and the site still passes live verification.
- `trusted_directory_contact`: a public directory publishes a member email. The directory domain is provenance only. A non-free published email domain may become a candidate first-party site but MUST pass current live hospitality identity verification before canonicalization.
- `roster_only`: useful premium/member evidence without a safe direct domain/contact. Never append directly to canonical. Keep as resolver evidence.

Directory root domains must never become the canonical domain for all member properties. Large brand spiders should collapse to operator/account level when appropriate rather than adding hundreds of properties behind one corporate domain.

ATP source workers are bounded exploration. They must not monopolize the 20-slot fleet; current policy caps approved ATP source work to two slots per cycle and only when a spider release is new/due/retryable.

## OpenStreetMap / Geofabrik

Use the official Geofabrik JSON index to resolve small country/subregion PBF extracts. Never schedule continent-scale downloads just because they exist. Every configured extract has an explicit byte cap and must pass a real canary before production enablement.

Read only public OSM hospitality/contact tags. A published website is required. Compatible explicit public email goes to the fast live gate; website-only goes through the bounded first-party recovery gate. Never infer contact information.

Version source work from stable upstream HTTP metadata (ETag / Last-Modified / Content-Length) and checkpoint independently from Overture. Current source-worker cap is one OSM/Geofabrik task per cycle until more extracts are canaried.

## External-source allocation

Overture geographic coverage is the backbone. External source tasks are exploration slots, not a replacement for map coverage. `tools/hospitality_master_plan.py` combines source tasks and geographic cells, and external tasks are capped so they cannot starve the World Atlas.

A source task is useful when upstream release/version changed, it has never completed, it is retryable, or its revisit interval is due. A source-version lookup failure must degrade to geographic work rather than block the fleet.

## Source promotion protocol

Never enable an unproven source globally.

1. Static/syntax validation.
2. Transport/schema probe against the real upstream.
3. Isolated source canary with no canonical writes.
4. Canonical-domain prefilter.
5. Standard live qualification.
6. Inspect provenance/quality and health metrics.
7. Production-enable only the proven source class / spider / extract.
8. Run a unified production smoke through the real single writer.
9. Persist source release + coverage + metrics.

If an architectural assumption fails, remove the rejected transport/design rather than leaving dead paths that a future maintainer could accidentally reactivate.

## Proven canaries as of 2026-08-15

- Overture Site Recovery, Florida Panhandle: 2,427 website records → 123 recovery candidates → 28 public emails recovered → 27 live-ready. Crawler health remained acceptable.
- Canonical pre-HTTP filter: a production US cell removed 176 of 357 already-known domains before HTTP.
- ATP Wanderhotels: 56 directory members → 55 unknown after canonical prefilter → 51 current live hospitality sites; 48 official-site Instagram profiles. Approved as `trusted_directory_contact`.
- ATP LHW: 471 premium properties, but no safe direct member contact/domain in sampled structure. Keep `roster_only`; never canonicalize the directory domain as the hotel domain.
- Geofabrik Monaco: 689,377-byte PBF → 15 hospitality domains → 6 direct-email candidates → 5 unknown after canonical prefilter → 5/5 live-ready. Website-only recovery added no emails in the canary.

## Scheduling

GitHub cron and workflow-run continuation are both safety mechanisms. The continuation watchdog must use the unified master planner so Overture Pass A, Overture Pass B and due external sources all count as useful backlog.

## Metrics that matter

Primary business KPI: `MASTER-new useful accounts per worker-minute`.

Also retain source/lane-specific:
- raw/materialized candidates;
- canonical pre-HTTP rejects;
- public email recovery rate;
- live HIGH/MEDIUM/live-ready;
- Instagram/Facebook/contact-page recovery;
- CI-new / changed / duplicate counts;
- MASTER-new after Sheet sync;
- 429, timeout and network/site error rates;
- worker wall time;
- actual upstream source release/version;
- coverage/revisit state.

Do not increase sockets or shorten revisit cadences to compensate for exhausted source novelty. Add/canary a genuinely new source or geography instead.
