#!/usr/bin/env python3
"""GWS v5.3 production entrypoint.

Core verifier logic is kept in gws_no_website_certifier_v53_core. This entrypoint
adds runtime-hardening layers discovered by live GitHub calibration:
1) fail-closed Overture STAC fallback that still must pass S3/schema smoke;
2) a throttled multi-provider search pool resilient to GitHub-runner rate limits;
3) conservative provider-family normalization so transport fallbacks cannot
   masquerade as independent evidence;
4) Belgian national/E.164 phone canonicalization for current-identity resolution;
5) unresolved Overture identities are still adversarially web-challenged, while
   remaining categorically ineligible for HIGH until current identity is strong.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53_core as _core
import gws_search_provider_pool_v54 as _providers
import gws_identity_resolver_v54 as _identity

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-06-17.0").strip()
_original_resolve = _core.resolve_overture_release


def resolve_overture_release() -> str:
    try:
        return _original_resolve()
    except RuntimeError as exc:
        # Never mask a bad explicit pin or another programming/configuration error.
        if not str(exc).startswith("OVERTURE_STAC_UNAVAILABLE:"):
            raise
        if not _core.RELEASE_RE.match(FALLBACK_RELEASE):
            raise RuntimeError(f"INVALID_OVERTURE_FALLBACK_RELEASE:{FALLBACK_RELEASE}") from exc
        print(
            f"OVERTURE_STAC_DOWN_USING_SMOKE_VALIDATED_FALLBACK={FALLBACK_RELEASE}",
            file=sys.stderr,
        )
        return FALLBACK_RELEASE


_core.resolve_overture_release = resolve_overture_release

# Fix equivalent Belgian national/E.164 phones before the Overture resolver
# decides whether a business has a current strongly-resolved identity.
_core.v2.indexes = _identity.indexes
_core.v2.resolve = _identity.resolve


def preclassify_hardened(c, p, pe, ovok):
    """Only stop before web when the verdict is already safely terminal.

    A missing Overture resolution is NOT evidence that a website search should be
    skipped. Such candidates may be disqualified by the web challenger, but the
    certificate still requires a strong current identity, so they cannot become
    HIGH merely by surviving search.
    """
    base = {"r": int(c["r"]), "candidate": c, "place": pe}
    complete, _ = _core.v5.complete_identity(c)
    if not _core.v2.in_scope(c):
        return {**base, "status": "REJECT", "reason": "OUT_OF_SCOPE"}
    if not complete:
        return {**base, "status": "UNCERTAIN", "reason": "SOURCE_IDENTITY_INCOMPLETE"}
    if not ovok:
        return {**base, "status": "ERROR_RETRYABLE", "reason": "OVERTURE_UNAVAILABLE"}
    # Only trust Overture website/closure evidence when that Overture entity was
    # actually resolved to the source candidate. `p` is None for unresolved best guesses.
    if p:
        site = _core.v2.owned(p.get("websites"))
        if site:
            return {**base, "status": "REJECT", "reason": "OWNED_SITE_OVERTURE", "owned_site": site}
        if _core.v2.t(p.get("operating_status")).lower() in {"closed", "permanently_closed"}:
            return {**base, "status": "REJECT", "reason": "CLOSED_OVERTURE"}
    return None


_core.v5.preclassify = preclassify_hardened


async def provider_webcheck(rows, conc, search_conc):
    ans = await _providers.webcheck(rows, conc, search_conc)
    # DDG HTML/Lite are one family. Yahoo is conservatively grouped with Bing
    # because its web index can be Bing-backed; this prevents transport redundancy
    # from being misrepresented as independent search evidence.
    family = {
        "ddg_html": "ddg", "ddg_lite": "ddg", "ddg": "ddg",
        "bing": "bing", "yahoo": "bing",
    }
    for ev in ans.values():
        normalized = []
        for p in ev.get("healthy_providers") or []:
            f = family.get(str(p), str(p))
            if f not in normalized:
                normalized.append(f)
        ev["healthy_provider_transports"] = list(ev.get("healthy_providers") or [])
        ev["healthy_providers"] = normalized
    return ans


# v5.run_web delegates to v4.webcheck, so switch production runtime to the
# live-smoke-validated provider pool with conservative evidence-family semantics.
_core.webcheck_hardened = provider_webcheck
_core.v4.webcheck = provider_webcheck

# Re-export the implementation surface so deterministic tests test the same
# production entrypoint, including underscored parser/DNS helpers.
for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
globals()["resolve_overture_release"] = resolve_overture_release
globals()["provider_webcheck"] = provider_webcheck
globals()["preclassify_hardened"] = preclassify_hardened
globals()["phone_keys"] = _identity.phone_keys


if __name__ == "__main__":
    _core.main()
