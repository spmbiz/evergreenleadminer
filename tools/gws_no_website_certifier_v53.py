#!/usr/bin/env python3
"""GWS autonomous no-website certifier production entrypoint.

Production hardening is deliberately fail-closed. Broad/high-ineligible rows get
bounded discovery; only strongly corroborated current identities can enter the
strict two-pass path. Deterministic brand-domain probes are first-class evidence,
while social networks and business directories never count as owned websites.
"""
from __future__ import annotations

import os
import sys

import gws_no_website_certifier_v53_core as _core
import gws_search_provider_pool_v54 as _providers
import gws_identity_resolver_v54 as _identity
import gws_worker_v55 as _worker_policy

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-07-22.0").strip()
_original_resolve = _core.resolve_overture_release
_original_guesses = _core.v4.guesses
_original_web_identity = _core.v4.identity

# Known directory/aggregation surfaces must never be promoted to owned websites.
_extra_platforms = ("creditsafe.", "numero-pro.", "busibee.")
_core.v2.PLAT = tuple(dict.fromkeys(tuple(_core.v2.PLAT) + _extra_platforms))


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


def guesses_hardened(c):
    """Put short-token brand domains early enough to survive bounded direct checks.

    Legacy roots dropped <=2-char brand tokens, so `ID.CITE ARCHITECTS` could never
    generate `idcite.be`. The first two normalized brand tokens are now probed
    before the broader legacy lattice.
    """
    name_tokens = [
        x for x in _core.v2.n(c.get("n")).split()
        if len(x) >= 2 and x not in _core.v2.STOP
    ]
    extra = []
    if len(name_tokens) >= 2:
        pair = name_tokens[:2]
        for root in ("".join(pair), "-".join(pair)):
            if 5 <= len(root) <= 45:
                for suffix in (".be", ".com", ".eu"):
                    extra.append("https://" + root + suffix + "/")
    merged = []
    seen = set()
    for u in extra + list(_original_guesses(c)):
        h = _core.v2.host(u)
        if h and h not in seen and not _core.v2.platform(u):
            seen.add(h); merged.append("https://" + h + "/")
        if len(merged) >= 20:
            break
    return merged


_core.v4.guesses = guesses_hardened


def web_identity_hardened(c, body, url=""):
    ev = dict(_original_web_identity(c, body, url))
    if ev.get("matched"):
        ev.setdefault("match_mode", "legacy_identity")
        return ev

    tx = _core.v2.textish(body)
    h = _core.v2.host(url)
    root = (h.split(".", 1)[0] if h else "").replace("-", "")
    toks = [x for x in _core.v2.n(c.get("n")).split() if len(x) >= 2 and x not in _core.v2.STOP]
    pair = "".join(toks[:2]) if len(toks) >= 2 else ""
    full = "".join(toks[:4])
    domain_brand = bool(
        len(root) >= 6 and (
            root == pair or root == full or
            (len(pair) >= 6 and root.startswith(pair) and len(root) <= len(pair) + 12)
        )
    )
    name_on_page = _core.v2.ov(c.get("n"), tx)
    brussels_geo = any(x in tx for x in ("brussels", "bruxelles", "brussel"))

    # This catches a current owned brand site even when the legacy source phone or
    # address is stale after a move. Brand-domain + page-name + Brussels are all
    # required; a directory/generic domain cannot satisfy this route.
    if domain_brand and name_on_page >= 0.60 and brussels_geo and not _core.v2.platform(url):
        ev["matched"] = True
        ev["match_mode"] = "brand_domain_page_name_brussels"
        ev["brand_domain"] = True
        ev["page_name_overlap"] = round(name_on_page, 3)
        ev["brussels_geo"] = True
    return ev


_core.v4.identity = web_identity_hardened


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
    family = {"bing": "bing", "yahoo": "bing", "exa": "exa"}
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
globals()["guesses_hardened"] = guesses_hardened
globals()["web_identity_hardened"] = web_identity_hardened
globals()["phone_keys"] = _identity.phone_keys


if __name__ == "__main__":
    _core.main()
