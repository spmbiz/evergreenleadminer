---
name: search-semantic-fleet
description: Operate GWS and adjacent hospitality lead resolution as an incremental high-recall search-and-semantic fleet using OpenSERP/DDGS/SearXNG, durable seen/change ledgers, parallel Qwen ~4B GitHub workers, GPT Web for difficult semantic review, and strict public-evidence provenance.
---

# Search + Semantic Fleet — GWS / Hospitality V2

Use this skill when designing, implementing, tuning, or operating the search-resolution and semantic intelligence layer for GWS business harvesting and adjacent hospitality/account-resolution work in this repository.

This skill complements `skills/autonomous-harvest/SKILL.md` and `docs/HARVESTER_SCALE_BLUEPRINT.md`. Those continue to own fleet persistence, geographic/source planning, canonicalization and throughput doctrine. This skill defines the newer search + local-model architecture agreed after those files were written.

## 1. Real operating environment

This stack does **not** assume OpenAI API agents.

The control plane is:

```text
GPT Web / GPT-5.6 Sol
  -> reads GitHub + Google Drive/Sheets
  -> launches / edits / debugs workflows
  -> reviews ambiguous / high-value cases

GitHub Actions / scripts
  -> bulk discovery
  -> search adapters
  -> local open-weight inference
  -> durable queues / ledgers / metrics
```

Do not add an OpenAI API dependency unless explicitly requested.

## 2. Current architecture decision: simple first

Do not build a many-model cascade before measurement proves it is necessary.

Preferred architecture:

```text
BROAD PUBLIC DISCOVERY
        ↓
NORMALIZE + EXACT DEDUPE
        ↓
SEEN / CHANGE LEDGER
        ↓
NEW / UNRESOLVED / MATERIALLY CHANGED ONLY
        ↓
SEARCH FABRIC
OpenSERP + DDGS (+ optional SearXNG)
        ↓
DETERMINISTIC IDENTITY EVIDENCE
        ↓
PARALLEL QWEN ~4B GITHUB WORKERS
ambiguous semantic resolution only
        ↓
GPT WEB
valuable / uncertain / contradictory / unusual cases
        ↓
CANONICAL BUSINESS / ACCOUNT STATE
```

## 3. GWS target is broader than NO WEBSITE

Useful website opportunity states include:

```text
NO_SITE
DEAD_SITE
BROKEN_SITE
PARKED_DOMAIN
FACEBOOK_ONLY
DIRECTORY_ONLY
ANCIENT_SITE
NON_MOBILE_SITE
NO_SSL
ONE_PAGE_BAD_SITE
BAD_CONVERSION_SITE
GOOD_SITE
UNCERTAIN
```

The commercial question is whether a real business has a meaningful website opportunity worth contacting.

## 4. Backfill vs incremental live work

Do not repeatedly resolve the same business/site universe from scratch. A large first pass may be expensive; persist it. Subsequent passes should prioritize:

```text
NEW BUSINESSES
NEW CANDIDATE DOMAINS
UNRESOLVED IDENTITIES
MATERIALLY CHANGED WEBSITES
STALE RECORDS WHOSE REVISIT WINDOW IS DUE
```

Previously resolved unchanged businesses should consume near-zero repeated SERP/LLM work.

## 5. Business / Site Intelligence Ledger

Persist durable state per business candidate:

```text
business_id
source
source_record_id
normalized_name
country
market
address
public_phone
first_seen_at
last_seen_at
candidate_domain
official_domain
domain_identity_status
website_state
site_content_hash
site_material_hash
last_site_check_at
search_fingerprint
search_last_run_at
classifier_model
classifier_prompt_version
classification
confidence
classified_at
needs_reclassification
canonical_status
rejection_reason
```

For hospitality/account graph work also preserve account/operator identity, portfolio/listing relationships, known property URLs, market pages and public contact/social routes.

Do not replace the hospitality Google Sheet as final commercial canonical state where that Sheet remains authoritative.

## 6. OpenSERP is a first-class GWS resolver

Use OpenSERP for query families such as:

```text
"Exact Business Name" City
"Exact Business Name" address
"Exact Business Name" phone
"Exact Business Name" official website
"Exact Business Name" contact
"Exact Business Name" Instagram
"Exact Business Name" Facebook
"exact public phone"
site:instagram.com "Business Name"
site:facebook.com "Business Name"
```

Use multiple engines and normalized output.

Search absence is **negative evidence, not proof of no website**. CAPTCHA, throttling, partial results and engine failures must remain explicit states.

## 7. DDGS and SearXNG

Use DDGS as a lightweight embedded search adapter/fallback for short-lived workers. Use SearXNG only when a persistent multi-engine service is beneficial, especially on a private/home machine or future persistent server.

Do not make production dependent on the user's PC being online.

Normalize search output as:

```text
query
query_family
engine/provider
region/language
rank
title
url
snippet
retrieved_at
status
captcha/throttle/error state
```

## 8. Search yield measurement

Track by:

```text
source × country × market × vertical × query_family × engine
```

Metrics:

```text
queries issued
successful responses
captcha/throttle/error rate
new candidate domains
official domains resolved
social profiles resolved
public emails/contact pages recovered
wrong-site candidates rejected
useful result yield
wall time
```

Build basic yield tracking before sophisticated bandit scheduling.

## 9. Deterministic identity evidence before LLM

Use exact evidence first:

- normalized public phone;
- street address;
- legal/business name;
- email/domain identity;
- city/market;
- Organization structured data;
- footer/contact/about/legal text;
- explicit contradictions.

Strong contradictory identity evidence should outweigh weak semantic resemblance. Directory/listicle results are not official sites.

Only ambiguous cases need Qwen.

## 10. Primary semantic model: Qwen ~4B on parallel GitHub workers

The first implementation target is a Qwen 3/3.5-class ~4B instruct model, CPU-friendly GGUF via `llama.cpp` or equivalent.

A matrix of 10–20 independent GitHub-hosted jobs means approximately 10–20 independent model instances processing shards concurrently, subject to the global capacity broker and real account concurrency.

Prefer horizontal model parallelism across hosted runners over making the user's PC the central production inference bottleneck.

## 11. Do not commit weights; cache them

Never put multi-GB GGUF weights into normal Git history.

Worker pattern:

```text
start runner
restore pinned model/runtime cache
or download if absent
load once
process substantial shard
persist outputs
exit
```

Cache is acceleration only. Classifications/evidence must be durable elsewhere.

Pin exact model/quant/checksum once productionized.

## 12. Qwen tasks

Good tasks:

- official site vs wrong business site;
- official site vs directory/aggregator/social-only page;
- semantic business niche fit;
- website-state interpretation from compact extracted text;
- GOOD_SITE vs meaningful website-opportunity review;
- conflicting identity evidence;
- ambiguous SERP candidate ranking;
- structured extraction from already-fetched first-party public text.

For hospitality/account graphs:

- operator vs isolated property distinction;
- short-stay/vacation-rental operator fit;
- pages plausibly belong to same operator;
- portfolio/contact signals.

## 13. Qwen must be permissive for commercial novelty

For identity it may reject strong contradictions. For opportunity classification:

```text
uncertain = KEEP FOR REVIEW
unusual = KEEP FOR REVIEW
potentially valuable = KEEP
insufficient evidence = UNKNOWN
```

Never invent phone, email, address, social profile or company identity.

Recommended output:

```json
{
  "business_id": "...",
  "candidate_url": "...",
  "decision": "MATCH|PROBABLE|WRONG|UNCERTAIN",
  "confidence": 0.0,
  "matching_evidence": [],
  "contradictions": [],
  "website_state": "NO_SITE|DEAD_SITE|BROKEN_SITE|PARKED_DOMAIN|FACEBOOK_ONLY|DIRECTORY_ONLY|ANCIENT_SITE|NON_MOBILE_SITE|NO_SSL|ONE_PAGE_BAD_SITE|BAD_CONVERSION_SITE|GOOD_SITE|UNCERTAIN",
  "needs_gpt_review": false,
  "reason": "short source-grounded explanation"
}
```

## 14. No mandatory tiny-model tier yet

Earlier exploration considered a ~0.5–0.8B prefilter. Current decision: **do not add one to the first production architecture unless Qwen ~4B throughput is empirically insufficient.**

Code is faster/safer for obvious deterministic rejects. A tiny model adds another false-negative and maintenance surface.

Likewise, do not add GLM/MiniCPM/Mistral/Qwen9B ensembles until a measured deficiency exists. Keep the interface model-agnostic so alternatives can be benchmarked later.

## 15. Batching

Do not assume one request per candidate. Benchmark compact batch sizes such as 8, 16 and 32 while auditing accuracy.

Per candidate include compact evidence only:

```text
business_id
name/city/address/phone
candidate URL
page title
short homepage/contact/about extracts
structured Organization fields
search snippets
```

Require strict machine-readable output keyed by candidate ID.

## 16. GPT Web boundary

GPT Web should focus on:

- conflicting evidence;
- valuable unresolved business;
- unusual website opportunity;
- parent/operator ambiguity;
- high-value hospitality operator graph;
- new source/query strategy;
- false-positive/false-negative review;
- architecture and fleet decisions.

Persist GPT-reviewed hard cases as reusable labels.

## 17. Website fetch/cache

Persist where useful:

```text
url
canonical_url
status_code
etag
last_modified
content_hash
parsed_text_hash
fetched_at
content_type
fetch_method
```

Do not repeatedly fetch unchanged pages. Browser rendering is an expensive fallback after lightweight HTTP/structured extraction/semantic evidence.

## 18. Hospitality operator expansion

For AI Property/hospitality use, prioritize account leverage over isolated property count.

After resolving an operator domain inspect public:

```text
robots.txt
sitemap.xml / sitemap indexes
inventory/property URL patterns
booking/PMS technology clues
contact/about/team/reservations pages
Instagram/Facebook/public WhatsApp
operator/parent identity
portfolio destinations
exact property/listing URLs
```

One operator can unlock many sample assets. Do not bypass authentication/access controls/private APIs.

## 19. Personal PC / self-hosted security

The user's PC may later host OpenSERP, SearXNG/DDGS, llama.cpp, a stronger local model, browser automation and warm caches, but it is optional bonus compute.

This repository is public. **Do not attach arbitrary public-repo workflow execution directly to the user's personal self-hosted runner.**

Preferred pattern:

```text
PUBLIC harvesting repo
       ↓ durable task/state
PRIVATE CONTROL REPO
       ↓ self-hosted runner
USER PC
```

If the private control repo is not established, do not expand public-repo self-hosted execution.

## 20. Rollout order

1. Seen/change ledger for business/site resolution.
2. Search-result cache + normalized Search Fabric interface.
3. OpenSERP adapter for entity/site/social resolution.
4. DDGS fallback adapter.
5. Search/query/engine yield metrics.
6. Deterministic official-site evidence scorer.
7. Qwen ~4B single-runner shadow smoke.
8. Real benchmark on 200–500+ trusted business/site pairs.
9. Model/runtime cache.
10. Parallel 10–20 worker semantic fleet respecting global capacity.
11. Persist model/prompt/version provenance.
12. GPT Web review queue for hard/high-value cases.
13. Browser-only-on-demand route.
14. SearXNG/private-PC lane only if useful.
15. Only after measurement: alternate/larger model or tiny-model tier.

## 21. Required benchmark

Track:

```text
MATCH precision
MATCH recall
WRONG-site false acceptance rate
valid-site false rejection rate
UNCERTAIN rate
website-state accuracy
public-contact hallucination count = ZERO
JSON/schema validity
candidates/minute per runner
aggregate fleet throughput
cache/model startup overhead
peak RAM
```

Also audit whether unusual but valuable businesses survive opportunity routing.

## 22. Live health metrics

Report separately:

```text
raw discovered
new unique businesses
unchanged skipped
new candidate domains
unresolved businesses
search queries issued
OpenSERP success/captcha/error
DDGS fallback usage
Qwen classified
Qwen backlog
GPT review backlog
official sites resolved
website-opportunity states
public email/contact/social yield
canonical net-new
```

Key capacity comparison:

```text
NEW + UNRESOLVED + MATERIALLY CHANGED ARRIVAL RATE
versus
SEARCH + QWEN RESOLUTION CAPACITY
```

If capacity is comfortably above arrival rate, do not add model layers merely because they are possible.

## 23. Non-negotiable summary

```text
GPT WEB IS THE ORCHESTRATOR, NOT AN OPENAI API DEPENDENCY.
OPEN SERP / DDGS ARE SEARCH RESOLVERS, NOT ORACLES.
SEARCH ABSENCE NEVER PROVES NO WEBSITE.
DETERMINISTIC IDENTITY EVIDENCE BEFORE LLM.
BACKFILL ONCE; RESOLVE DELTAS / UNRESOLVED WORK THEREAFTER.
QWEN ~4B ON PARALLEL GITHUB RUNNERS FIRST.
NO TINY-MODEL TIER WITHOUT A MEASURED NEED.
NO HALLUCINATED CONTACT OR BUSINESS IDENTITY DATA.
CACHE FETCHES AND SEARCH RESULTS.
MEASURE YIELD BY SOURCE / QUERY / ENGINE / GEO.
PC IS OPTIONAL BONUS COMPUTE.
NO PERSONAL SELF-HOSTED RUNNER DIRECTLY FROM PUBLIC-REPO WORKFLOWS.
DO NOT ADD COMPLEXITY WITHOUT A MEASURED BOTTLENECK.
```