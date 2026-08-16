# Rental + GWS Source Expansion Skill

## Purpose

This skill is an implementation backlog and architecture guide for expanding the **Rental** and **GWS / no-website** harvesters with reusable open-source/public-data bricks.

The goal is **not** to bolt every scraper onto the repo. The goal is to increase coverage, speed, resilience, entity resolution, and evidence quality while keeping one canonical pipeline.

Use this skill when asked to improve discovery, enrichment, property/operator matching, no-website verification, website-quality scoring, direct-booking discovery, source coverage, or harvesting throughput.

---

## Mandatory rule: inspect first, extend instead of duplicate

Before implementing any item below:

1. Inspect the current repo tree, configs, workflows, adapters/providers, tests, storage schema, and relevant existing skills.
2. Search for the source/vendor/tool by multiple spellings and inspect runtime configuration, not only code search.
3. If equivalent coverage already exists, **do not add a second parallel rail by default**. Improve the existing adapter, normalization, evidence, retry behavior, tests, or coverage instead.
4. Benchmark a new source against the current source before making it part of the default fleet.
5. Prefer public/official APIs, bulk datasets, downloadable exports, public HTML, and stable machine-readable sources over browser automation.
6. Use browser automation only where the public source genuinely requires rendered HTML or interaction.
7. Keep rate limiting, backoff, circuit breakers, cursors/checkpoints, and source health visible.
8. Never fabricate a missing field. Unknown stays unknown.
9. Every important enriched field should carry provenance/evidence.
10. Re-check the upstream project's current license, access model, ToS/robots constraints, and maintenance state before copying code. If the license is unclear or incompatible, use the project only as an architectural/reference implementation.

### Current known coverage to preserve

The existing autonomous-harvest material already references **Overture**, **OpenStreetMap / OSM**, and **AllThePlaces**. Treat these as existing concepts and inspect their actual implementation before adding overlapping discovery rails.

The user has indicated that **Foursquare may already be used by Rental**, but repository search did not conclusively prove the current integration. Therefore:

- inspect Foursquare-specific code, secrets/config, workflow inputs, data tables, and run logs first;
- if Foursquare is present, extend or harden it rather than creating another Foursquare implementation;
- if it is not present, only add it after comparing expected incremental coverage against existing Rental discovery sources.

---

## Shared architecture target

Both Rental and GWS should converge on the same harvesting kernel where practical:

```text
Source Registry
  -> incremental state / cursors / leases
  -> raw observations
  -> canonical normalization
  -> evidence/provenance
  -> entity resolution
  -> fast HTTP probe
  -> browser fallback when necessary
  -> asset/image/site fingerprints
  -> scoring/qualification
  -> durable canonical records
```

A source adapter is disposable. The canonical model, evidence contract, state, and tests are not.

### Recommended adapter result contract

```yaml
source:
  provider: example
  source_id: provider-native-id
  source_url: https://...
  observed_at: 2026-08-16T00:00:00Z
record:
  canonical_fields: {}
evidence:
  field_name:
    value: ...
    confidence: 0.0
    observations:
      - source_url: https://...
        source_type: api|html|directory|registry|derived
        observed_at: ...
        snippet_or_hash: ...
```

Every adapter should support, where applicable:

- deterministic source IDs;
- normalized URLs/domains/phones/addresses;
- idempotent upserts;
- checkpoint/cursor persistence;
- retries with bounded backoff;
- source health metrics;
- fixtures and regression tests;
- raw-response or content-hash evidence for debugging.

---

# RENTAL expansion backlog

## P0 — highest-value bricks

### 1. `RealEstateWebTools/property_web_scraper`

Why it matters:

- mature mapping-driven real-estate listing extractor;
- takes listing URL or already-rendered HTML and emits normalized property data;
- broad multi-country portal registry plus generic fallback;
- extraction logic is config/mapping oriented rather than hard-wiring every portal into application code;
- current upstream is MIT, but re-check before code reuse.

Implementation idea:

```text
portal URL / rendered HTML
  -> source registry
  -> portal mapping
  -> canonical RentalListing
  -> entity resolution
  -> CanonicalProperty / CanonicalOperator
```

Do **not** blindly import all mappings. First compare them with current Rental sources and prioritize portals that add real luxury/high-end/property-manager coverage in target geographies.

Useful pattern to borrow: mapping files with CSS selectors, JSON-LD paths, embedded-script paths, regex/post-processing, support tiers, fixtures.

### 2. `tamnd/airbnb-cli`

Why it matters:

- Apache-2.0 public/logged-out Airbnb data tooling;
- exposes search/listing/review/calendar/host/host-listing concepts as structured records;
- its graph/edge model is especially valuable for going from one property to a host portfolio and neighboring public entities;
- CLI/HTTP/MCP surfaces demonstrate how one domain model can power multiple interfaces.

Implementation idea:

- inspect current Airbnb acquisition first;
- use the upstream primarily as a reference or adapter candidate for public data reachable without authentication;
- capture host -> listings edges into the entity graph;
- keep edge-blocking / anti-bot failures explicit rather than pretending a fetch succeeded;
- never attempt to bypass private account controls.

### 3. `cermak-petr/actor-booking-scraper`

Why it matters:

- Apache-2.0 Booking.com reference implementation;
- supports list/detail extraction, dates/rooms, filters, concurrency;
- documents the important **query partitioning** pattern when one broad search is capped.

Pattern to borrow:

```text
one huge search with a coverage ceiling
  -> split by geography / price / property type / rating / date buckets
  -> dedupe listing IDs
  -> detail only for qualified candidates
```

This is analogous to geo-tiling in Maps harvesting: partition to improve recall, then entity-resolve.

### 4. Image-twin / property-twin matching

Interesting references:

- `knjcode/imgdupes`
- `mk-fg/image-deduplication-tool`
- `idealo/imagededup`

Goal:

Match the same physical property across Airbnb, Booking, real-estate portals, direct-booking sites, and property-manager sites even when title/address wording differs.

Recommended architecture:

```text
listing images
  -> normalize / resize
  -> perceptual hashes + optional image embeddings
  -> ANN candidate retrieval
  -> geometric / metadata confirmation
  -> property-twin confidence
  -> CanonicalProperty
```

Do not make image similarity alone sufficient for a hard merge. Combine it with location, amenities, capacity, textual clues, host/operator clues, and shared contact/domain evidence.

### 5. Public PMS / direct-booking fingerprint engine

Useful architecture/reference repos:

- `TelivityAI/haip`
- `minical/wordpress-minical-integration`
- `Kamra-PMS/kamra-pms`

These are **not** primarily harvest sources. Their value is understanding how public booking engines/PMS products expose recognizable public artifacts.

Build a fingerprint catalog from lawful public signals such as:

- HTML/meta tags;
- JS/CSS asset names;
- public script URLs;
- favicon hashes;
- page paths and canonical URLs;
- DNS/CNAME patterns;
- JSON-LD/schema markup;
- public booking-widget signatures.

Potential vendor families to fingerprint include Lodgify, Guesty, Hostaway, Smoobu, OwnerRez, WordPress PMS ecosystems, and custom engines. Validate each fingerprint empirically.

**Never probe admin/private endpoints or use this as a route into authenticated systems.** The goal is public-site attribution and direct-booking discovery.

---

## P1 — useful Rental references / conditional integrations

### `openbnb-org/mcp-server-airbnb`

Inspect as another public Airbnb/MCP implementation. Compare coverage, maintenance, failure modes, and data model with current Airbnb tooling before adopting anything.

### `keithah/hospitable-python`

Useful only when the data owner supplies a legitimate Hospitable API token. Treat it as an authenticated first-party integration, **not** a third-party harvesting technique.

### `airbnb-pp-cli`

If present/upstream still maintained, inspect for property-twin/direct-site/host-portfolio concepts. Re-verify its current repository, license, and supported portals before depending on it; do not assume historical VRBO support is still current.

### `dimitryzub/hotels-scraper-js`

Reference only if still unmaintained. Mine parser/normalization ideas, not production dependency assumptions.

### `airbert-vln/bnb-dataset`

Older project, but potentially useful for image acquisition/cache/data-packaging ideas. Reference only unless current maintenance justifies more.

---

# GWS / NO-WEBSITE expansion backlog

## P0 — highest-value bricks

### 1. `projectdiscovery/httpx`

This should be evaluated as a **tier-0 domain/site probe** before expensive browsers.

Current upstream is a fast Go HTTP toolkit with status/title/content length/type, redirects, TLS, CNAME, server/CDN, response timing, body/header hashes, favicon hash, and other probes. It supports high-throughput operation and structured output. Current upstream is MIT; re-check release behavior before pinning.

Target GWS flow:

```text
candidate business/domain
  -> httpx fast probe
      -> clearly healthy website
      -> clearly dead/no usable web presence
      -> ambiguous
  -> only ambiguous / deep-quality candidates go to Playwright/Lighthouse/visual audit
```

Benchmark this against the current verifier. The objective is to reduce browser cost while **improving** evidence, not merely to add another tool.

Also useful for Rental direct-booking candidates and PMS fingerprints.

### 2. Tech/fingerprint corpus

`enthec/webappanalyzer`

Useful as a maintained Wappalyzer-style fingerprint corpus. License is GPL-3.0 at the time it was researched, so inspect license compatibility before copying data/code into this repo. It may be better used as an external/reference dataset depending on repo licensing.

Remember that network-response fingerprinting and rendered-DOM/browser fingerprinting have different recall. Use the latter only when it adds material value.

### 3. `sensepost/gowitness`

Useful for screenshot/headless evidence and visual triage. Compare with any screenshot functionality already available through the chosen HTTP/browser stack before adding another dependency.

### 4. Independent business discovery rails

Candidates to benchmark, not automatically adopt:

- `gosom/google-maps-scraper`
- `hannesegi/tools-google-maps`
- `noworneverev/google-maps-scraper`
- `worldscraping/google-maps-scraper`
- `nick-choudhary/gmaps-scraper`
- `NoumanZahid-85/no-website-lead-finder`
- `ghostmap`
- `adil6572/YP-business-scraper`
- `cermak-petr/actor-yellowpages-scraper`

Why multiple independent discovery rails can help:

- a business missing from one source may appear in another;
- directories can surface different categories/geographies;
- independent observations improve confidence in address/phone/entity matching.

But do not let them create duplicate canonical leads. Everything should pass through entity resolution and source-aware dedupe.

For Maps-specific tools, benchmark coverage, speed, proxy/browser requirements, stability, and fields against the current Maps/discovery stack. Keep the best-performing rail(s), not every rail.

### 5. Website quality / “bad website” scoring

Candidates:

- `GoogleChrome/lighthouse`
- `NovaCrawl`
- `viasite/site-audit-seo`
- inspect `the-ai-entrepreneur-ai-hub/google-maps-leads-scraper` for its website audit scoring ideas if still available/current.

Recommended two-stage score:

```text
Tier 0: cheap HTTP/DNS/site existence and obvious failure checks
Tier 1: only promising/ambiguous domains -> Lighthouse/crawler/rendered checks
```

Possible signals:

- no valid domain / parked domain / hard failure;
- persistent redirects or broken TLS;
- mobile/basic performance issues;
- missing title/meta/structured business identity;
- severe content thinness;
- stale copyright/date clues as weak evidence only;
- broken contact path;
- no HTTPS / mixed-content or obvious fatal technical issues;
- obvious template/placeholder pages.

Avoid treating one weak SEO metric as proof that a business needs a new website. Keep evidence inspectable.

---

# Shared bricks for Rental + GWS

## Entity resolution — `moj-analytical-services/splink`

Strong candidate for probabilistic entity resolution when there is no universal shared ID. MIT upstream at research time.

Use cases:

- Rental: Airbnb listing + Booking listing + direct site + property manager + company -> canonical operator/property.
- GWS: Maps + OSM + Overture + directory + website + registry -> canonical business.

Candidate features:

- normalized name;
- phone;
- email/domain;
- street/postcode/geocoordinates;
- social handles;
- registration IDs;
- image twin score for Rental;
- source-specific IDs.

Do not auto-merge uncertain entities without a reviewable score/evidence trail.

## `dedupeio/dedupe`

Alternative/companion for learned fuzzy matching. Compare operational fit with Splink rather than adopting both by default.

## Evidence/provenance — borrow the model from `brightdata/open-enrich`

Open Enrich's valuable idea is not its paid infrastructure; it is the **evidence-per-field** model: source URL, supporting observation/snippet, confidence, and freshness.

Implement that concept using the repo's own free/owned sources where possible. Do not add Bright Data as a default dependency merely because the reference project uses it.

## Email verification — `AfterShip/email-verifier`

MIT Go library at research time. Useful for syntax, DNS/MX, disposable/free/role-account checks and optional SMTP reachability.

Important operational caveat: SMTP verification can be unavailable or inconclusive because outbound port 25 is commonly blocked and many mail servers use catch-all/anti-enumeration behavior. Treat `unknown` as unknown, not invalid.

## Company registries

`sophymarine/openregistry` was identified as a multi-registry enrichment reference. Before integrating, inspect its current access model, quotas, data provenance, and whether first-party national registries are preferable for the target country.

---

# Source selection rules

For every proposed source, score it before integration:

```text
incremental_coverage
x field_quality
x stability
x legal/access confidence
x throughput
x maintainability
x provenance_quality
------------------------------------------------
latency + infra cost + anti-bot fragility + duplicate overlap
```

A flashy scraper with 95% overlap and high browser/proxy cost can be worse than a boring public dataset that adds 10% unique coverage reliably.

Prefer this hierarchy:

1. official/public bulk data;
2. official/public API;
3. stable public downloadable files/feeds;
4. public HTML with HTTP parser;
5. rendered browser fallback;
6. manual review for hard edge cases.

---

# Implementation sequence for an agent

When asked to implement one or more items from this skill:

1. Read `AGENTS.md` and the existing relevant skills first.
2. Inspect the current source/provider registry and canonical schemas.
3. Prove whether equivalent coverage already exists.
4. Choose the smallest P0 change that increases unique coverage or reduces cost materially.
5. Add adapter + normalization + evidence + state/checkpoint + tests together.
6. Run a bounded benchmark against the incumbent source/flow.
7. Report unique records gained, overlap, latency/cost, errors, and field completeness.
8. Only then wire it into autonomous/default production flow.

---

# Definition of done

A source expansion is not done because a scraper returned rows. It is done when:

- no redundant provider was accidentally created;
- canonical IDs/dedupe/entity resolution work;
- important fields have provenance;
- retries/rate limits/circuit breaker are bounded;
- state/checkpoint makes reruns incremental where possible;
- fixtures/regression tests cover representative records and failures;
- source health is observable;
- public/auth boundaries are respected;
- unknown/missing data remains explicit;
- benchmark shows why the new brick deserves to stay.

Keep this skill as a living backlog: when an item is implemented, update this file to mark the current integration path and what remains, rather than leaving future agents to rediscover it.