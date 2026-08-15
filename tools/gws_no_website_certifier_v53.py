#!/usr/bin/env python3
"""GWS autonomous no-website certifier production entrypoint.

Runtime-hardening layers discovered by live GitHub calibration:
1) fail-closed Overture STAC fallback that still must pass S3/schema smoke;
2) throttled multi-provider search resilient to runner rate limits;
3) conservative provider-family normalization;
4) Belgian national/E.164 phone canonicalization;
5) unresolved identities still receive a web challenge but remain HIGH-ineligible;
6) unresolved Overture guesses never become canonical dedupe keys;
7) unresolved identities stop after one complete adversarial pass because a second
   pass cannot make them HIGH; resolved HIGH candidates still require two passes.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53_core as _core
import gws_search_provider_pool_v54 as _providers
import gws_identity_resolver_v54 as _identity
import gws_worker_v54 as _worker_policy

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-07-22.0").strip()
_original_resolve = _core.resolve_overture_release


def resolve_overture_release() -> str:
    try:
        return _original_resolve()
    except RuntimeError as exc:
        if not str(exc).startswith("OVERTURE_STAC_UNAVAILABLE:"):
            raise
        if not _core.RELEASE_RE.match(FALLBACK_RELEASE):
            raise RuntimeError(f"INVALID_OVERTURE_FALLBACK_RELEASE:{FALLBACK_RELEASE}") from exc
        print(f"OVERTURE_STAC_DOWN_USING_SMOKE_VALIDATED_FALLBACK={FALLBACK_RELEASE}", file=sys.stderr)
        return FALLBACK_RELEASE


_core.resolve_overture_release = resolve_overture_release
_core.v2.indexes = _identity.indexes
_core.v2.resolve = _identity.resolve


def preclassify_hardened(c, p, pe, ovok):
    base = {"r": int(c["r"]), "candidate": c, "place": pe}
    complete, _ = _core.v5.complete_identity(c)
    if not _core.v2.in_scope(c):
        return {**base, "status": "REJECT", "reason": "OUT_OF_SCOPE"}
    if not complete:
        return {**base, "status": "UNCERTAIN", "reason": "SOURCE_IDENTITY_INCOMPLETE"}
    if not ovok:
        return {**base, "status": "ERROR_RETRYABLE", "reason": "OVERTURE_UNAVAILABLE"}
    if p:
        site = _core.v2.owned(p.get("websites"))
        if site:
            return {**base, "status": "REJECT", "reason": "OWNED_SITE_OVERTURE", "owned_site": site}
        if _core.v2.t(p.get("operating_status")).lower() in {"closed", "permanently_closed"}:
            return {**base, "status": "REJECT", "reason": "CLOSED_OVERTURE"}
    # unresolved current identity deliberately continues to web challenge
    return None


_core.v5.preclassify = preclassify_hardened


def canonical_key_hardened(x):
    pe = x.get("place") or {}
    c = x.get("candidate") or {}
    oid = _core.v2.t(pe.get("overture_id"))
    if oid and pe.get("resolved"):
        return "o:" + oid
    pc = _core.v2.t(c.get("p"))[:4]
    addr = " ".join(_core.v2.n(c.get("a")).split()[:10])
    phones = _identity.phone_keys(c.get("ph"))
    if phones:
        ph = next((p for p in sorted(phones) if p.startswith("0")), sorted(phones)[0])
        if addr:
            return "p:" + ph + "|" + pc + "|" + addr
        return "p:" + ph + "|" + pc + "|" + _core.v2.n(c.get("n"))
    return "n:" + _core.v2.n(c.get("n")) + "|" + pc + "|" + addr


_core.v5.canonical_key = canonical_key_hardened


async def provider_webcheck(rows, conc, search_conc):
    ans = await _providers.webcheck(rows, conc, search_conc)
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


_core.webcheck_hardened = provider_webcheck
_core.v4.webcheck = provider_webcheck
_core.worker = lambda a: _worker_policy.worker(a, _core)

for _name, _value in vars(_core).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
globals()["resolve_overture_release"] = resolve_overture_release
globals()["provider_webcheck"] = provider_webcheck
globals()["preclassify_hardened"] = preclassify_hardened
globals()["canonical_key_hardened"] = canonical_key_hardened
globals()["phone_keys"] = _identity.phone_keys


if __name__ == "__main__":
    _core.main()
