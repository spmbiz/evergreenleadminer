#!/usr/bin/env python3
"""Source-website-safe wrapper for the ownership-safe GWS verifier.

P0 invariants:
- source-provided first-party website evidence can never be silently ignored;
- third-party listings never count as owned, but their URL slugs may provide
  identity aliases for the final search challenge;
- a would-be HIGH gets a bounded direct-domain challenge derived from trusted
  business/alias tokens so search-engine misses cannot silently certify absence.

None of these accelerators can create HIGH. They can only disprove or withhold it.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

import gws_search_verify_ownership_safe as safe

_ORIGINAL_CLASSIFY = safe.classify_strict_safe
_ORIGINAL_CHALLENGE_QUERIES = safe._challenge_queries

_ALIAS_STOP = {
    "fr", "nl", "en", "be", "belgium", "belgique", "bruxelles", "brussels", "brussel",
    "ixelles", "elsene", "uccle", "ukkel", "forest", "vorst", "auderghem", "oudergem",
    "saint", "gilles", "josse", "sint", "jose", "watermael", "boitsfort",
    "page", "profile", "profil", "booking", "reserve", "reservation", "rdv",
}
_GENERIC_ALIAS = {
    "salon", "coiffure", "coiffeur", "barber", "barbershop", "hair", "hairdresser",
    "restaurant", "cafe", "shop", "store", "boutique", "clinic", "centre", "center",
}


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _urls(value: Any) -> list[str]:
    vals: list[Any] = []
    if isinstance(value, (list, tuple, set)):
        vals.extend(value)
    elif isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    vals.extend(parsed)
                elif isinstance(parsed, str):
                    vals.append(parsed)
                else:
                    vals.append(raw)
            except Exception:
                vals.append(raw)
    elif value:
        vals.append(value)
    out: list[str] = []
    seen: set[str] = set()
    for v in vals:
        u = str(v or "").strip()
        if not u:
            continue
        if "://" not in u:
            u = "https://" + u
        h = safe.base.v2.host(u)
        if not h or h in seen:
            continue
        seen.add(h)
        out.append(u)
    return out


def source_website_candidates(row: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for field in ("overture_websites", "overture_website", "source_website"):
        for url in _urls(row.get(field)):
            candidates.append({"url": url, "provenance": field})
    hist = row.get("historical_reject_evidence") or {}
    for url in _urls(hist.get("owned_website")):
        candidates.append({"url": url, "provenance": "historical_reject_evidence.owned_website"})
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in candidates:
        h = safe.base.v2.host(c["url"])
        if h and h not in seen:
            seen.add(h)
            out.append(c)
    return out


def _identity_compacts(row: dict[str, Any], c: dict[str, Any]) -> list[str]:
    vals = [
        str(c.get("n") or ""),
        str(row.get("hub_name") or ""),
        str(row.get("overture_name") or ""),
    ]
    return [x for x in {_compact(v) for v in vals} if len(x) >= 5]


def source_listing_aliases(row: dict[str, Any], c: dict[str, Any]) -> list[dict[str, str]]:
    """Use third-party URL slugs only as search aliases, never ownership proof."""
    identities = _identity_compacts(row, c)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in source_website_candidates(row):
        url = item["url"]
        if not (safe.own.is_third_party(url) or safe.base.v2.platform(url)):
            continue
        try:
            segs = [unquote(x) for x in urlparse(url).path.split("/") if x]
        except Exception:
            continue
        for seg in reversed(segs[-3:]):
            words = [x.lower() for x in re.findall(r"[A-Za-zÀ-ÿ]+", seg.replace("_", "-").replace("+", " "))]
            words = [x for x in words if len(x) >= 3 and x not in _ALIAS_STOP]
            if len(words) < 2:
                continue
            slug_compact = _compact(" ".join(words))
            # Require a real linkage back to the source identity. This prevents a
            # random directory category slug from becoming a business alias.
            linked = any(
                ident in slug_compact
                or slug_compact in ident
                or sum(1 for w in words if _compact(w) and _compact(w) in ident) >= 2
                for ident in identities
            )
            if not linked:
                continue
            variants = [words]
            nongeneric = [w for w in words if w not in _GENERIC_ALIAS]
            if len(nongeneric) >= 2:
                variants.append(nongeneric)
            for ws in variants:
                alias = " ".join(ws[:5]).strip()
                key = _compact(alias)
                if len(key) < 5 or key in seen:
                    continue
                seen.add(key)
                found.append({"alias": alias, "source_url": url, "provenance": item["provenance"]})
                if len(found) >= 4:
                    return found
    return found


def _augmented_challenge_queries(row: dict[str, Any], c: dict[str, Any]) -> tuple[list[str], str]:
    base_queries, phone_source = _ORIGINAL_CHALLENGE_QUERIES(row, c)
    address = " ".join(str(c.get("a") or row.get("hub_address") or "").split()[:10]).strip()
    extra: list[str] = []
    for item in source_listing_aliases(row, c):
        alias = item["alias"]
        if address:
            extra.append(f'"{alias}" "{address}" website')
        extra.append(f'"{alias}" website')
    merged: list[str] = []
    for q in extra + base_queries:
        if q and q not in merged:
            merged.append(q)
    return merged[:5], phone_source


# The underlying final challenge resolves this module global at runtime. Patch it
# once for the source-safe process; no per-candidate monkey patching/race.
safe._challenge_queries = _augmented_challenge_queries


def source_website_precheck(row: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for candidate in source_website_candidates(row):
        url = candidate["url"]
        provenance = candidate["provenance"]
        if safe.own.is_third_party(url) or safe.base.v2.platform(url):
            events.append({"url": url, "provenance": provenance, "status": "THIRD_PARTY_OR_PLATFORM"})
            continue
        try:
            ev = safe.base.home.probe_host(c, url)
        except Exception as exc:
            assessment = safe.own.assess(row, {"owned": url, "owned_identity": {}, "owned_via": "source_website_exception"})
            item = {"url": url, "provenance": provenance, "status": "PROBE_EXCEPTION", "error": str(exc)[:240], "assessment": assessment}
            events.append(item)
            if assessment.get("branded_host") and not assessment.get("third_party"):
                blockers.append(item)
            continue

        final = str(ev.get("final") or url)
        p = {"owned": final, "owned_identity": ev.get("identity") or {}, "owned_via": "source_website_precheck"}
        assessment = safe.own.assess(row, p)
        item = {
            "url": url,
            "final": final,
            "provenance": provenance,
            "ok": bool(ev.get("ok")),
            "matched": bool(ev.get("matched")),
            "dns_negative": bool(ev.get("dns_negative")),
            "status_code": int(ev.get("status") or 0),
            "error": str(ev.get("error") or "")[:160],
            "identity": ev.get("identity") or {},
            "assessment": assessment,
        }
        if ev.get("matched") and assessment.get("confident"):
            item["status"] = "OWNED_CONFIRMED"
            events.append(item)
            return {"status": "OWNED_CONFIRMED", "owned": final, "assessment": assessment, "events": events, "blockers": blockers}

        if ev.get("dns_negative"):
            item["status"] = "DNS_NEGATIVE_FALLTHROUGH"
        elif ev.get("ok"):
            item["status"] = "LIVE_BRANDED_UNRESOLVED" if assessment.get("branded_host") else "LIVE_UNBRANDED_FALLTHROUGH"
            if assessment.get("branded_host") and not assessment.get("third_party"):
                blockers.append(item)
        elif int(ev.get("status") or 0) in {404, 410} and not ev.get("error"):
            item["status"] = "NOT_FOUND_FALLTHROUGH"
        else:
            item["status"] = "TRANSIENT_OR_BLOCKED"
            if assessment.get("branded_host") and not assessment.get("third_party"):
                blockers.append(item)
        events.append(item)

    if blockers:
        retryable = any(x.get("status") in {"PROBE_EXCEPTION", "TRANSIENT_OR_BLOCKED"} for x in blockers)
        return {"status": "BLOCKING_RETRYABLE" if retryable else "BLOCKING_AMBIGUOUS", "owned": "", "events": events, "blockers": blockers}
    return {"status": "CLEAR", "owned": "", "events": events, "blockers": []}


def _domain_guess_inputs(row: dict[str, Any], c: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    base_names = [str(c.get("n") or ""), str(row.get("hub_name") or ""), str(row.get("overture_name") or "")]
    for name in base_names:
        key = _compact(name)
        if len(key) >= 5 and key not in seen:
            seen.add(key)
            items.append({"alias": name, "origin": "business_name"})
    for item in source_listing_aliases(row, c):
        key = _compact(item["alias"])
        if key not in seen:
            seen.add(key)
            items.append({"alias": item["alias"], "origin": "third_party_slug_alias"})
    return items[:6]


def direct_domain_challenge(row: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    guesses: list[dict[str, str]] = []
    seen_hosts: set[str] = set()
    for item in _domain_guess_inputs(row, c):
        words = [x.lower() for x in re.findall(r"[A-Za-zÀ-ÿ0-9]+", item["alias"]) if len(x) >= 2]
        meaningful = [w for w in words if w not in _ALIAS_STOP and w not in _GENERIC_ALIAS and not w.isdigit()]
        stems: list[str] = []
        if len(meaningful) >= 2:
            stems.extend(["".join(meaningful[:2]), "".join(meaningful)])
        elif meaningful:
            stems.append("".join(meaningful))
        raw = _compact(item["alias"])
        if len(raw) >= 5:
            stems.append(raw)
        for stem in stems:
            if len(stem) < 5 or len(stem) > 40:
                continue
            for tld in ("be", "com"):
                host = f"{stem}.{tld}"
                if host in seen_hosts:
                    continue
                seen_hosts.add(host)
                guesses.append({"url": "https://" + host, "alias": item["alias"], "origin": item["origin"]})
                if len(guesses) >= 10:
                    break
            if len(guesses) >= 10:
                break
        if len(guesses) >= 10:
            break

    events: list[dict[str, Any]] = []
    live_ambiguous: list[dict[str, Any]] = []
    for guess in guesses:
        try:
            ev = safe.base.home.probe_host(c, guess["url"])
        except Exception as exc:
            events.append({**guess, "status": "PROBE_EXCEPTION_IGNORED_GUESS", "error": str(exc)[:160]})
            continue
        final = str(ev.get("final") or guess["url"])
        alias_row = dict(row)
        alias_row["hub_name"] = guess["alias"]
        p = {"owned": final, "owned_identity": ev.get("identity") or {}, "owned_via": "direct_domain_high_challenge"}
        assessment = safe.own.assess(alias_row, p)
        item = {
            **guess,
            "final": final,
            "ok": bool(ev.get("ok")),
            "matched": bool(ev.get("matched")),
            "dns_negative": bool(ev.get("dns_negative")),
            "status_code": int(ev.get("status") or 0),
            "assessment": assessment,
        }
        if ev.get("matched") and assessment.get("confident"):
            item["status"] = "OWNED_CONFIRMED"
            events.append(item)
            return {"status": "OWNED_CONFIRMED", "owned": final, "assessment": assessment, "events": events}
        if ev.get("ok") and assessment.get("branded_host") and not assessment.get("third_party"):
            item["status"] = "LIVE_BRANDED_UNRESOLVED"
            live_ambiguous.append(item)
        elif ev.get("dns_negative"):
            item["status"] = "DNS_NEGATIVE"
        else:
            item["status"] = "NO_BLOCK"
        events.append(item)
    if live_ambiguous:
        return {"status": "AMBIGUOUS", "owned": "", "events": events, "ambiguous": live_ambiguous}
    return {"status": "CLEAR", "owned": "", "events": events}


def classify_strict_source_safe(row: dict[str, Any], c: dict[str, Any], pe: dict[str, Any], fabric, max_queries: int) -> dict[str, Any]:
    source_check = source_website_precheck(row, c)
    if source_check.get("status") == "OWNED_CONFIRMED":
        out = deepcopy(row)
        out.update({
            "outcome": "REJECT",
            "reason": "OWNED_SITE_FIRST_PARTY_CONFIRMED_SOURCE_WEBSITE",
            "needs_gpt_review": False,
            "verification_status": "REJECT",
            "verification_provider": "openserp_ci_ownership_safe",
            "owned_website": str(source_check.get("owned") or ""),
            "source_website_precheck": source_check,
            "web_pass1": {"skipped": True, "reason": "SOURCE_WEBSITE_OWNERSHIP_CONFIRMED"},
            "web_pass2": {"skipped": True, "reason": "SOURCE_WEBSITE_OWNERSHIP_CONFIRMED"},
            "certificate": {"verified": False, "reason": "OWNED_SITE_FIRST_PARTY_SOURCE_WEBSITE", "source_website_precheck": source_check},
            "certificate_digest": "",
        })
        return out

    out = _ORIGINAL_CLASSIFY(row, c, pe, fabric, max_queries)
    out["source_website_precheck"] = source_check
    cert = dict(out.get("certificate") or {})
    cert["source_website_precheck"] = source_check

    if str(out.get("verification_status") or "").upper() == "HIGH" and source_check.get("status") in {"BLOCKING_RETRYABLE", "BLOCKING_AMBIGUOUS"}:
        retryable = source_check.get("status") == "BLOCKING_RETRYABLE"
        out.update({
            "outcome": "REVIEW" if retryable else "UNCERTAIN",
            "reason": "SOURCE_WEBSITE_PROBE_INCOMPLETE" if retryable else "SOURCE_WEBSITE_LIVE_BRANDED_UNRESOLVED",
            "verification_status": "ERROR_RETRYABLE" if retryable else "UNCERTAIN",
            "needs_gpt_review": True,
            "owned_website": "",
        })
        cert["verified"] = False
        cert["source_website_blocks_high"] = True
        cert.pop("high_challenge_clear", None)

    if str(out.get("verification_status") or "").upper() == "HIGH":
        domain_check = direct_domain_challenge(row, c)
        out["direct_domain_high_challenge"] = domain_check
        cert["direct_domain_high_challenge"] = domain_check
        if domain_check.get("status") == "OWNED_CONFIRMED":
            out.update({
                "outcome": "REJECT",
                "reason": "OWNED_SITE_FOUND_BY_DIRECT_DOMAIN_HIGH_CHALLENGE",
                "verification_status": "REJECT",
                "needs_gpt_review": False,
                "owned_website": str(domain_check.get("owned") or ""),
            })
            cert["verified"] = False
            cert["direct_domain_disproved_no_website"] = True
            cert.pop("high_challenge_clear", None)
        elif domain_check.get("status") == "AMBIGUOUS":
            out.update({
                "outcome": "UNCERTAIN",
                "reason": "DIRECT_DOMAIN_HIGH_CHALLENGE_AMBIGUOUS",
                "verification_status": "UNCERTAIN",
                "needs_gpt_review": True,
                "owned_website": "",
            })
            cert["verified"] = False
            cert["direct_domain_blocks_high"] = True
            cert.pop("high_challenge_clear", None)

    out["certificate"] = cert
    out["certificate_digest"] = str(cert.get("evidence_digest") or out.get("certificate_digest") or "")
    return out


def main() -> int:
    safe.classify_strict_safe = classify_strict_source_safe
    return safe.main()


if __name__ == "__main__":
    raise SystemExit(main())
