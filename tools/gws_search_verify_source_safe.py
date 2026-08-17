#!/usr/bin/env python3
"""Source-website-safe wrapper for the ownership-safe GWS verifier.

P0 invariant: a source-provided website can never be silently ignored on the
path to VERIFIED_NO_WEBSITE. Current first-party ownership may REJECT. A live
or transient branded source website that is not strong enough to REJECT blocks
HIGH and is kept UNCERTAIN/ERROR_RETRYABLE. Dead/NXDOMAIN evidence may fall
through to the existing two-pass + final-challenge verifier.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import gws_search_verify_ownership_safe as safe

_ORIGINAL_CLASSIFY = safe.classify_strict_safe


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
    # Keep one candidate per host; freshest Overture fields win over historical evidence.
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in candidates:
        h = safe.base.v2.host(c["url"])
        if h and h not in seen:
            seen.add(h)
            out.append(c)
    return out


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
            # A branded source URL that cannot be probed is not proof of absence.
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
            # Live + branded is enough to block a no-site certificate, but not
            # enough to terminally REJECT unless the ownership gate is confident.
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
    out["certificate"] = cert
    out["certificate_digest"] = str(cert.get("evidence_digest") or out.get("certificate_digest") or "")
    return out


def main() -> int:
    safe.classify_strict_safe = classify_strict_source_safe
    return safe.main()


if __name__ == "__main__":
    raise SystemExit(main())
