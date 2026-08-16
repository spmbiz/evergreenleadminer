# GWS search verification continuity contract

The autonomous GWS fleet uses a provider-neutral verification stage after entity resolution and canonical shard dedupe.

## Order

1. Deterministic Hub/OSM/Overture identity and owned-site screen.
2. GitHub-hosted OpenSERP strict two-pass verification when healthy.
3. DDGS fallback, then optional SearXNG, when OpenSERP is unavailable.
4. GPT review queue for valuable/uncertain/retryable/precanonical-HIGH cases.
5. Canonical MASTER dedupe, persistence and readback remain downstream authority.

## Hard guards

- `VERIFIED_NO_WEBSITE` is the only strict HIGH reason.
- Search absence is never proof of no website.
- DDGS/SearXNG fallback may confirm an owned site and REJECT a candidate, but may not certify HIGH.
- OpenSERP HIGH requires the existing certificate gates: complete source identity, strong current entity identity, two-pass multi-family search coverage, no owned site in either pass, and no unresolved plausible domain.
- Social/directory/platform pages do not count as owned websites.
- Personal residential compute is never targeted directly by the public harvesting repository.
- The public GitHub-hosted lane remains independently capable of harvesting and verification when the residential PC is offline.

## Residential lane

`walidgdg1-ai/runnerlocal` is the private control boundary for residential OpenSERP. Residential evidence is additive/high-value verification evidence; it must not become a hard dependency of the public autonomous fleet. Cross-repository result ingress must use a separately scoped credential or another explicit durable transport and is independently re-evaluated before canonical persistence.
