#!/usr/bin/env python3
"""GWS autonomous no-website certifier production entrypoint.

Production hardening is deliberately fail-closed. Broad/high-ineligible rows get
bounded discovery; only strongly corroborated current identities can enter the
strict two-pass path. Deterministic brand-domain probes are first-class evidence,
while social networks, directories, and obvious public institutions never become
independent local-business HIGHs.
"""
from __future__ import annotations

import html
import os
import re
import sys
import urllib.parse

import gws_no_website_certifier_v53_core as _core
import gws_search_provider_pool_v56_free as _providers
import gws_reference_mesh_v57 as _ref
import gws_identity_resolver_v54 as _identity
import gws_worker_v55 as _worker_policy

FALLBACK_RELEASE = os.getenv("OVERTURE_FALLBACK_RELEASE", "2026-07-22.0").strip()
_original_resolve = _core.resolve_overture_release
_original_guesses = _core.v4.guesses
_original_web_identity = _core.v4.identity

_extra_platforms = ("creditsafe.", "numero-pro.", "busibee.")
_core.v2.PLAT = tuple(dict.fromkeys(tuple(_core.v2.PLAT) + _extra_platforms + tuple(_ref.REFERENCE_MARKERS)))

_PUBLIC_ENTITY_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\bcentre\s+scolaire\b", r"\becole\b", r"\bécole\b", r"\bschool\b",
    r"\bathenee\b", r"\bathénée\b", r"\badministration\s+communale\b",
    r"\bcommune\s+d['’ ]", r"\bgemeente\b", r"\bcpas\b", r"\bocmw\b",
    r"\bservice\s+public\b", r"\bpolice\s+zone\b", r"\bambassade\b", r"\bconsulat\b",
))


def obvious_non_independent_entity(c) -> bool:
    name = _core.v2.t(c.get("n"))
    return any(rx.search(name) for rx in _PUBLIC_ENTITY_PATTERNS)


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
    name_tokens = [x for x in _core.v2.n(c.get("n")).split() if len(x) >= 2 and x not in _core.v2.STOP]
    extra = []
    if len(name_tokens) >= 2:
        pair = name_tokens[:2]
        for root in ("".join(pair), "-".join(pair)):
            if 5 <= len(root) <= 45:
                for suffix in (".be", ".com", ".eu"):
                    extra.append("https://" + root + suffix + "/")
    merged = []; seen = set()
    for u in extra + list(_original_guesses(c)):
        h = _core.v2.host(u)
        if h and h not in seen and not _core.v2.platform(u):
            seen.add(h); merged.append("https://" + h + "/")
        if len(merged) >= 20: break
    return merged


_core.v4.guesses = guesses_hardened


def web_identity_hardened(c, body, url=""):
    ev = dict(_original_web_identity(c, body, url))
    if ev.get("matched"):
        ev.setdefault("match_mode", "legacy_identity")
        return ev
    tx = _core.v2.textish(body); h = _core.v2.host(url)
    root = (h.split(".", 1)[0] if h else "").replace("-", "")
    toks = [x for x in _core.v2.n(c.get("n")).split() if len(x) >= 2 and x not in _core.v2.STOP]
    pair = "".join(toks[:2]) if len(toks) >= 2 else ""; full = "".join(toks[:4])
    domain_brand = bool(len(root) >= 6 and (root == pair or root == full or (len(pair) >= 6 and root.startswith(pair) and len(root) <= len(pair) + 12)))
    name_on_page = _core.v2.ov(c.get("n"), tx)
    brussels_geo = any(x in tx for x in ("brussels", "bruxelles", "brussel"))
    if domain_brand and name_on_page >= 0.60 and brussels_geo and not _core.v2.platform(url):
        ev.update(matched=True, match_mode="brand_domain_page_name_brussels", brand_domain=True, page_name_overlap=round(name_on_page, 3), brussels_geo=True)
    return ev


_core.v4.identity = web_identity_hardened


def coverage_hardened(w):
    healthy = len(set(w.get("healthy_providers") or []))
    searched = int(w.get("search_queries") or 0)
    usable = int(w.get("search_usable_queries") or 0)
    resultful = int(w.get("search_resultful_queries") or sum(1 for q in (w.get("search_health") or []) if q.get("raw_resultful") or int(q.get("external_domains") or 0) > 0))
    checked = int(w.get("direct_checked") or 0)
    return {
        "healthy_engines": healthy,
        "searched_queries": searched,
        "usable_query_families": usable,
        "resultful_queries": resultful,
        "direct_domains_checked": checked,
        "ok": healthy >= 2 and searched >= 3 and usable >= 3 and resultful >= 1 and checked >= 5,
    }


_core.v5.coverage = coverage_hardened


def preclassify_hardened(c, p, pe, ovok):
    base = {"r": int(c["r"]), "candidate": c, "place": pe}
    complete, _ = _core.v5.complete_identity(c)
    if not _core.v2.in_scope(c): return {**base, "status": "REJECT", "reason": "OUT_OF_SCOPE"}
    if obvious_non_independent_entity(c): return {**base, "status": "REJECT", "reason": "OUT_OF_SCOPE_NON_INDEPENDENT_PUBLIC_ENTITY"}
    if not complete: return {**base, "status": "UNCERTAIN", "reason": "SOURCE_IDENTITY_INCOMPLETE"}
    if not ovok: return {**base, "status": "ERROR_RETRYABLE", "reason": "OVERTURE_UNAVAILABLE"}
    if p:
        site = _core.v2.owned(p.get("websites"))
        if site: return {**base, "status": "REJECT", "reason": "OWNED_SITE_OVERTURE", "owned_site": site}
        if _core.v2.t(p.get("operating_status")).lower() in {"closed", "permanently_closed"}:
            return {**base, "status": "REJECT", "reason": "CLOSED_OVERTURE"}
    return None


_core.v5.preclassify = preclassify_hardened


def canonical_key_hardened(x):
    pe = x.get("place") or {}; c = x.get("candidate") or {}
    oid = _core.v2.t(pe.get("overture_id"))
    if oid and pe.get("resolved"): return "o:" + oid
    pc = _core.v2.t(c.get("p"))[:4]; addr = " ".join(_core.v2.n(c.get("a")).split()[:10]); phones = _identity.phone_keys(c.get("ph"))
    if phones:
        ph = next((p for p in sorted(phones) if p.startswith("0")), sorted(phones)[0])
        if addr: return "p:" + ph + "|" + pc + "|" + addr
        return "p:" + ph + "|" + pc + "|" + _core.v2.n(c.get("n"))
    return "n:" + _core.v2.n(c.get("n")) + "|" + pc + "|" + addr


_core.v5.canonical_key = canonical_key_hardened


def bing_href_inventory_hardened(body, base):
    """Decode Bing /ck/a targets using the already-tested legacy unwrap logic."""
    normal=[]; references=[]; raw_hosts=set(); seen=set(); seen_ref=set(); base_host=_core.v2.host(base)
    for raw in re.findall(r'''href\s*=\s*["']([^"'#]+)''', body or "", re.I):
        joined=urllib.parse.urljoin(base, html.unescape(raw.strip()))
        u=_core.v4._unwrap(joined)
        h=_core.v2.host(u)
        if not u.startswith("http") or not h or h==base_host or any(x in h for x in ("bing.com","microsoft.com","msn.com")):
            continue
        raw_hosts.add(h)
        if _ref.is_reference(u):
            if h not in seen_ref: seen_ref.add(h); references.append(u)
        elif not _core.v2.platform(u) and h not in seen:
            seen.add(h); normal.append(u)
    return normal,references,raw_hosts


# webcheck resolves this global at call time; patching it here keeps the provider
# fail-closed while reusing the mature Bing redirect decoder from v4.
_providers._href_inventory = bing_href_inventory_hardened


async def provider_webcheck(rows, conc, search_conc):
    ans = await _providers.webcheck(rows, conc, search_conc)
    family = {"bing": "bing", "yahoo": "bing", "yep": "yep", "ghostery": "discovery_only"}
    for ev in ans.values():
        normalized = []; raw = list(ev.get("healthy_providers") or [])
        for p in raw:
            f = family.get(str(p), str(p))
            if f == "discovery_only": continue
            if f not in normalized: normalized.append(f)
        ev["healthy_provider_transports"] = raw; ev["healthy_providers"] = normalized; ev["zero_paid_api"] = True
    return ans


_core.webcheck_hardened = provider_webcheck
_core.v4.webcheck = provider_webcheck
_core.worker = lambda a: _worker_policy.worker(a, _core)

for _name, _value in vars(_core).items():
    if not _name.startswith("__"): globals()[_name] = _value
globals()["resolve_overture_release"] = resolve_overture_release
globals()["provider_webcheck"] = provider_webcheck
globals()["preclassify_hardened"] = preclassify_hardened
globals()["canonical_key_hardened"] = canonical_key_hardened
globals()["guesses_hardened"] = guesses_hardened
globals()["web_identity_hardened"] = web_identity_hardened
globals()["coverage_hardened"] = coverage_hardened
globals()["obvious_non_independent_entity"] = obvious_non_independent_entity
globals()["bing_href_inventory_hardened"] = bing_href_inventory_hardened
globals()["phone_keys"] = _identity.phone_keys

if __name__ == "__main__": _core.main()
