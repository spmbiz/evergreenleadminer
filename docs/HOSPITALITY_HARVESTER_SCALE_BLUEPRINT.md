# Hospitality / AI Property Harvester Scale Blueprint

## Purpose

This file documents the scale direction for the separate AI Property / hospitality harvester so the same engineering principles can be reused even though its canonical business state currently lives in Google Sheets rather than this repository.

Canonical lead state remains the Google Sheet **AI Property Global Lead Graph — MASTER**. Do not replace that state with this repository. This document is architectural guidance for future ChatGPT Web / GitHub implementation work.

## Real operating environment

This system does **not** assume OpenAI API agents. ChatGPT Web (currently GPT-5.6 Sol in the user's workflow) acts as high-level orchestrator through native Google Drive/Sheets and GitHub connectivity. Disposable scripts / GitHub Actions / CircleCI workers should perform deterministic bulk execution and leave durable, machine-readable state for ChatGPT Web to inspect and steer.

## North-star objective

Do not optimize raw property count. Optimize:

```text
contactable buying account
× portfolio leverage
× visual/sample quality
× probability of purchase
× expected gross profit
```

The account is the buyer. The property is the proof/sample asset.

## 1. Operator graph expansion

When a credible operator/account is resolved, automatically expand its graph instead of stopping at one property:

```text
ACCOUNT
  -> official domain
  -> properties / villas / rentals inventory
  -> destinations / markets
  -> public team/contact routes
  -> Instagram / Facebook / WhatsApp where published
  -> booking engine / PMS clues
  -> legal/parent operator
  -> exact property pages
  -> sample-priority assets
```

One resolved operator may yield tens or hundreds of usable assets. Account-level resolution should receive more compute than isolated single-property enrichment when commercial leverage is higher.

## 2. Sitemap-first portfolio mapping

For every resolved official domain, cheaply inspect:

```text
/robots.txt
/sitemap.xml
/sitemap_index.xml
```

and linked sitemap files.

Classify URLs likely representing inventory:

```text
/property/
/properties/
/villa/
/villas/
/rental/
/rentals/
/accommodation/
/stay/
```

Use this before manual/search-engine discovery of each asset. Preserve exact source URL and account relationship.

## 3. Booking/PMS technology detection

Detect public technology clues from HTML, scripts, endpoints and booking links. Examples may include hosted booking engines, property-management systems, WordPress plugins or other inventory platforms.

Use technology fingerprints only to improve lawful public discovery, e.g. identify consistent public property URL structures, structured data or sitemap endpoints. Do not bypass authentication or access controls.

Store:

```text
booking_technology
technology_confidence
technology_evidence_url
inventory_route_hint
```

## 4. Aggressive verified contact enrichment

For valuable accounts, selectively inspect:

```text
/contact
/about
/team
/privacy
/legal
/terms
/reservations
/booking
/press
/partners
```

Extract published business contacts from:

- `mailto:`;
- JSON-LD / schema.org Organization;
- footer/header;
- contact/reservations pages;
- public social links;
- public downloadable PDFs where relevant.

Never generate guessed addresses and label them verified.

## 5. Global fetch/content cache

Do not repeatedly download the same operator pages in every pass.

Track:

```text
url
canonical_url
status
etag
last_modified
content_hash
fetched_at
parsed_text_hash
account_id
```

If unchanged, reuse prior extraction/resolution evidence.

## 6. Incremental account/asset crawling

Persist per-account expansion state:

```text
account_id
last_domain_crawl
last_sitemap_hash
known_property_urls
known_market_pages
known_contact_pages
last_enriched_at
```

Future passes should focus on deltas/new properties/new contacts rather than recreating the portfolio.

## 7. Discovery vs intelligence separation

Use broad cheap discovery workers for:

- place/business search;
- directories;
- search-result collection;
- sitemap enumeration;
- page fetching;
- social-link extraction.

Reserve intelligence for:

- ambiguous account/entity resolution;
- operator vs property grouping;
- commercial leverage scoring;
- sample-priority scoring;
- conflicting evidence.

## 8. Source/query/geo yield ledger

Every discovery route should report:

```text
raw
new_accounts
new_assets
duplicates
sites_found
emails_found
socials_found
high_priority
compute_seconds
```

Score:

```text
source × country × market × query_family × lead_type
```

Allocate more compute to routes producing new, contactable, high-leverage accounts. Reserve exploration budget for new markets and query families.

## 9. Queue / work-stealing model

Prefer central or durable queues with bounded leases over static giant shards.

```text
discovery queue
-> workers claim batches
-> persist raw candidates

enrichment queue
-> workers claim valuable unresolved accounts
-> persist evidence

portfolio queue
-> workers expand resolved operators
-> persist new assets
```

Fast workers should be able to claim another batch instead of idling after their assigned geography finishes.

## 10. Adaptive batching

Use different batch sizes for:

- search/discovery;
- HTTP page fetch;
- browser-backed resolution;
- sitemap expansion;
- deep enrichment;
- small local LLM classification if added.

The controller should adjust from measured throughput rather than fixed universal floors.

## 11. Local small-LLM role

Local CPU-friendly models can be tested for ambiguous tasks such as:

- is this candidate domain really the operator's official site?;
- does this business appear to be a vacation-rental/property operator?;
- do these property pages belong to the same account?;
- classify public text into portfolio/contact signals.

Use deterministic phone/domain/address/legal-name evidence first. Use GPT Web only for valuable/ambiguous cases. Persist GPT Web decisions as benchmark labels.

## 12. Commercial account scoring

Separate dimensions instead of one opaque score:

```text
contactability
portfolio leverage
market attractiveness
visual/sample potential
commercial quality
confidence
```

High priority should strongly favor accounts with repeat-purchase potential and enough public evidence to personalize outreach.

## 13. Feedback from outreach and revenue

Once outreach runs, join outcomes back to lead features:

```text
route used
contacted
reply
positive reply
meeting
closed
revenue
```

Learn which geographies, operator sizes, property types, social activity levels and contact routes actually convert. Eventually schedule compute according to expected commercial value, not raw discovery counts.

## 14. Rejection / unresolved reasons

Persist explicit states:

```text
duplicate_account
duplicate_asset
wrong_entity
isolated_property_low_leverage
no_public_contact
weak_visual_fit
low_commercial_fit
conflicting_identity
fetch_failed
unresolved
```

This becomes the dataset for better rules/model evaluation.

## 15. Observability

Expose compact run metrics readable by ChatGPT Web:

```text
raw/min
new accounts/min
new assets/min
dedupe rate
email yield
site yield
social yield
portfolio expansion yield
high-priority / 1k raw
compute seconds / high-priority account
queue backlog and oldest age
```

## 16. Shared Harvester Core

Reuse infrastructure where practical with GWS and Tender systems:

```text
source adapters
queue / leases
normalization
dedupe
fetch cache
retry policy
worker telemetry
yield ledger
confidence routing
provenance
feedback metrics
```

Do not merge business-specific canonical databases or scoring assumptions merely for code reuse.

## Implementation order

1. sitemap-first operator portfolio expansion;
2. persistent account/asset crawl state;
3. source/query/geo yield ledger;
4. global fetch/content cache;
5. queue + work stealing + adaptive batches;
6. booking/PMS technology fingerprinting;
7. selective deep contact-page extraction;
8. local-LLM shadow tests for ambiguous account/site matching;
9. compact observability dashboard/state artifact;
10. outreach/revenue feedback loop and adaptive scheduler.

## Non-negotiable principles

```text
THE ACCOUNT IS THE BUYER.
THE PROPERTY IS THE PROOF ASSET.
EXPAND OPERATORS BEFORE CHASING RANDOM ISOLATED ASSETS.
CODE FOR FACTS; LLM FOR AMBIGUITY.
GPT WEB IS THE HIGH-LEVEL CONTROLLER, NOT AN OPENAI API DEPENDENCY.
CACHE AND CHECKPOINT EVERYTHING REUSABLE.
MEASURE YIELD BY SOURCE/QUERY/GEO.
OPTIMIZE COMMERCIAL VALUE, NOT RAW ROW COUNT.
NEVER INVENT CONTACT DATA.
THE GOOGLE SHEET REMAINS CANONICAL STATE.
```