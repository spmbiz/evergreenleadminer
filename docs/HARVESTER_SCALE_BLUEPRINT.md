# GWS Harvester Scale Blueprint

## Operating context

This project is **not designed around OpenAI API agents**. The high-level orchestrator is ChatGPT Web (currently GPT-5.6 Sol in the user's workflow) with native GitHub and Google Drive/Sheets connectivity. GitHub Actions / CircleCI / scripts are the disposable execution layer; ChatGPT Web is the launcher, editor, debugger, reviewer and strategic controller.

Do not redesign this system assuming continuous OpenAI API access. Prefer machine-readable artifacts, queues, logs, checkpoints and deterministic workers that ChatGPT Web can inspect and steer through native connectors.

## North-star objective

Do not optimize for raw leads. Optimize for:

```text
Expected Value per compute hour
= P(valid business)
× P(real opportunity)
× P(contactable)
× P(reply)
× P(close)
× expected gross profit
```

Until downstream outreach/close data exists, use proxy scores and record the features so the scoring can later become closed-loop.

## Common architecture

```text
SOURCE ADAPTERS
  -> RAW OBJECT STORE
  -> NORMALIZATION
  -> DEDUPE
  -> GLOBAL FETCH/CACHE LAYER
  -> CHEAP DETERMINISTIC FILTERS
  -> SEMANTIC AMBIGUITY LAYER (small local LLM optional)
  -> DEEP VERIFICATION
  -> OPPORTUNITY SCORING
  -> CANONICAL DATABASE
  -> ACTION / REVIEW QUEUES
  -> METRICS + FEEDBACK
```

The pipeline must separate **discovery workers** from **intelligence workers**. Discovery should be broad, cheap and fast. Expensive browser or semantic work should be reserved for candidates that survive earlier stages.

## 1. Website-state taxonomy

Stop treating GWS as a binary `NO WEBSITE / HAS WEBSITE` problem. Classify candidates into:

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

The commercial universe is therefore "businesses with meaningful website opportunity", not only businesses with no domain.

## 2. Official-site identity resolution

Resolve whether a candidate URL truly belongs to the target business.

Use deterministic evidence first:

```text
same normalized phone      strong positive
same street address        strong positive
same legal/business name   positive
same city/market           positive
same niche/services        supporting
conflicting phone/address  strong negative
unrelated company/niche    strong negative
directory/listicle         reject as official site
```

Only ambiguous cases should reach the local-LLM classifier described in `AGENTS.md`. Browser rendering should be after lightweight HTTP/HTML checks, not before.

## 3. Global fetch cache

Add a reusable cache contract:

```text
url
canonical_url
status_code
content_hash
etag
last_modified
fetched_at
parsed_text_hash
content_type
fetch_method
```

Do not repeatedly fetch and re-parse unchanged pages across runs. Treat CI cache as an acceleration layer only; canonical lead state must remain durable elsewhere.

## 4. Incremental crawling

Persist what has already been explored by geography/source/query. Future runs should prioritize deltas rather than restarting the same search universe.

Suggested state:

```text
source
country
market
vertical
query_family
last_cursor_or_page
last_success_at
known_candidate_fingerprints
known_domains
known_business_ids
```

## 5. Source/query yield tracking

Every discovery route must report its yield:

```text
raw_candidates
new_unique_businesses
verified_opportunities
contactable_businesses
high_priority
fetch_failures
browser_escalations
llm_escalations
compute_seconds
```

Maintain a score for each:

```text
source × country × market × vertical × query_family
```

The scheduler should progressively allocate more compute to high-yield combinations and reduce repetitive low-yield routes.

## 6. Bandit-style exploration

Once enough observations exist, implement a simple explore/exploit scheduler. It does not require machine learning.

- exploit proven high-yield source/query/geography combinations;
- reserve a bounded percentage of capacity for new combinations;
- update yields after every run;
- avoid permanently starving new markets.

Later, optimize against downstream revenue/reply outcomes rather than lead count.

## 7. Long-lived CI workers + work stealing

Avoid tiny jobs that repeatedly pay setup cost.

Preferred worker lifecycle:

```text
start
-> install/load once
-> claim bounded batch from queue
-> process
-> persist results
-> claim another batch
-> repeat until safe runtime budget
-> checkpoint and exit cleanly
```

Use work stealing rather than fixed large geographic shards when possible so fast workers can consume more backlog.

## 8. Adaptive batching

Use different batch sizes by workload:

- HTTP-only fetch: large batches;
- browser rendering: small batches;
- local LLM: medium batches sized to RAM/context;
- enrichment: small high-value batches.

Never force one universal shard size.

## 9. Browser as expensive fallback

Preferred sequence:

```text
DNS / URL normalization
-> HEAD/GET
-> HTML + structured-data parsing
-> deterministic identity checks
-> small LLM if semantically ambiguous
-> browser only when rendering is actually needed
```

## 10. Screenshot / visual opportunity scoring

For candidates that survive initial verification, optionally capture a homepage screenshot and score visible opportunity signals:

- broken layout;
- severe visual age;
- poor mobile behavior;
- missing CTA;
- weak navigation;
- poor branding consistency;
- obvious placeholder content.

Do not run visual analysis on the entire raw universe. Route only promising leads.

## 11. Structured public-contact enrichment

Extract only published business data from already-fetched public sources:

```text
mailto links
phones
JSON-LD / schema.org Organization
contact/about/legal/privacy/footer pages
social profile links
public business email
```

Never generate guessed emails and mark them verified.

## 12. Rejection reason ledger

Every reject should carry an explicit reason, for example:

```text
duplicate
not_a_business
wrong_business_site
directory_only
already_good_website
inactive_business
bad_geo
no_commercial_fit
unreachable_fetch
uncertain_manual_review
```

This rejection corpus becomes training/evaluation data for rule tuning and small-model benchmarking.

## 13. Active learning

Use stronger GPT Web judgment primarily for uncertain cases. Persist its final classification as a labeled example. Over time:

```text
uncertain case
-> GPT Web review
-> label stored
-> rules/prompts/thresholds improved
-> fewer future escalations
```

The system should become less dependent on high-level reasoning for repetitive decisions.

## 14. Business feedback loop

When outreach exists, feed back:

```text
contacted
opened/reached
replied
positive_reply
meeting
closed
revenue
```

Join these outcomes to the original discovery features. Then optimize source allocation and opportunity scoring by actual commercial results.

## 15. Observability

At minimum expose:

```text
raw/min
new/min
dedupe rate
HTTP success rate
verified opportunity rate
contact yield
browser escalation rate
LLM escalation rate
compute seconds / verified opportunity
high-priority / 1k raw
```

ChatGPT Web should be able to read compact machine-generated summaries and decide what to change next without parsing giant logs.

## 16. Shared Harvester Core direction

Where practical, reuse abstractions also useful to the hospitality and tender systems:

```text
queue / lease semantics
source adapters
normalization
dedupe
fetch cache
retry policy
worker telemetry
yield tracking
confidence routing
provenance
feedback metrics
```

Do not force identical business logic across products. Share infrastructure, not assumptions.

## Implementation order

1. website-state taxonomy + explicit rejection reasons;
2. deterministic site-identity score;
3. URL/content cache;
4. source/query yield ledger;
5. queue + long-lived worker loop + adaptive batches;
6. shadow small-LLM identity classifier and benchmark;
7. browser-only-on-demand routing;
8. incremental crawl checkpoints;
9. screenshot opportunity score for shortlisted leads;
10. downstream outreach/revenue feedback loop and adaptive scheduler.

## Non-negotiable principles

```text
CODE FOR FACTS.
LLM FOR AMBIGUITY.
GPT WEB FOR HIGH-LEVEL CONTROL AND HARD CASES.
CACHE WHAT HAS ALREADY BEEN LEARNED.
MEASURE YIELD BY SOURCE/QUERY/GEO.
OPTIMIZE VERIFIED COMMERCIAL OPPORTUNITIES, NOT RAW ROWS.
PERSIST EVIDENCE AND REJECTION REASONS.
NEVER INVENT CONTACT OR BUSINESS IDENTITY DATA.
```