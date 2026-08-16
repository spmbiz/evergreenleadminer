#!/usr/bin/env python3
"""Ownership-safe wrapper around the parallel GWS search verifier.

The legacy verifier can prove that a page contains the business identity, but that
is not the same thing as proving that the business owns the host. This wrapper
keeps the fast/checkpointed engine while adding a separate first-party ownership
gate before any owned-site REJECT is allowed.

Before any HIGH is emitted, a cheap final challenge searches exact phone/address
identity and mines domain-looking strings from directory snippets. This catches
owned website outlinks that a search engine exposes only inside a directory result.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

import gws_ownership_gate as own
import gws_search_verify as base
import gws_search_verify_fast as fast

DOMAIN_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)")


def _sanitize_owned(row: dict[str, Any], p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    q = deepcopy(p or {})
    assessment = own.assess(row, q)
    q["ownership_assessment"] = assessment
    if q.get("owned") and not assessment.get("confident"):
        q["untrusted_owned_candidate"] = {
            "url": q.get("owned"),
            "identity": q.get("owned_identity") or {},
            "owned_via": q.get("owned_via"),
            "assessment": assessment,
        }
        q["owned"] = ""
        q["owned_identity"] = {}
        q["owned_via"] = "ownership_gate_withheld"
    return q, assessment


def _challenge_queries(c: dict[str, Any]) -> list[str]:
    name = str(c.get("n") or "").strip()
    phone = str(c.get("ph") or "").strip()
    address = " ".join(str(c.get("a") or "").split()[:10]).strip()
    out: list[str] = []
    if name and phone:
        out.append(f'"{name}" "{phone}" website')
    if name and address:
        out.append(f'"{name}" "{address}" website')
    if phone:
        out.append(f'"{phone}" website')
    return out[:3]


def _candidate_seeds(item) -> list[tuple[str, str]]:
    """Return result URL plus domain-looking strings leaked by snippets/titles."""
    out: list[tuple[str, str]] = []
    direct = str(getattr(item, "url", "") or "").strip()
    if direct:
        out.append((direct, "serp_result"))
    text = " ".join([
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "snippet", "") or ""),
    ])
    for host in DOMAIN_RE.findall(text):
        host = host.strip(". ,;:()[]{}<>'\"").lower()
        if host:
            out.append(("https://" + host, "serp_snippet_domain"))
    return out


def final_high_challenge(row: dict[str, Any], c: dict[str, Any], fabric) -> dict[str, Any]:
    """Search only HIGH survivors for hidden/linked websites.

    Exact-identity directory results frequently contain an outbound website that
    never ranks as its own SERP result. We mine candidate domains from the snippet,
    probe them directly, and run the same ownership gate. Search transport failure
    fails closed: a HIGH becomes retryable rather than being certified blindly.
    """
    events: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    checked: set[str] = set()
    usable = 0

    queries = _challenge_queries(c)
    for query in queries:
        results, event = fabric._openserp("high_exact_identity_challenge", query)
        meta = event.get("meta") or {}
        fams = base.provider_families(meta) if event.get("status") == "OK" else set()
        if len(fams) >= 2:
            usable += 1
        events.append({
            "query": query,
            "status": event.get("status"),
            "http_status": event.get("http_status"),
            "families": sorted(fams),
            "results": len(results),
            "error": event.get("error"),
        })

        for item in results:
            for seed, source in _candidate_seeds(item):
                host = base.v2.host(seed)
                if not host or host in checked:
                    continue
                checked.add(host)

                # Third-party result pages are useful evidence but never ownership.
                # We still mine their snippets above for explicit outbound domains.
                if own.is_third_party(seed) or base.v2.platform(seed):
                    continue

                hint_item = {
                    "url": seed,
                    "host": host,
                    "title": str(getattr(item, "title", "") or ""),
                    "description": str(getattr(item, "snippet", "") or ""),
                }
                plausible, hint = base.home.plausible(c, hint_item)
                if source == "serp_result" and not plausible:
                    continue

                ev = base.home.probe_host(c, seed)
                ev["challenge_source"] = source
                ev["challenge_query"] = query
                ev["serp_hint"] = hint
                probes.append(ev)

                if ev.get("matched"):
                    p = {
                        "owned": str(ev.get("final") or seed),
                        "owned_identity": ev.get("identity") or {},
                        "owned_via": "high_exact_identity_challenge",
                    }
                    assessment = own.assess(row, p)
                    if assessment.get("confident"):
                        return {
                            "status": "OWNED_CONFIRMED",
                            "owned": p["owned"],
                            "assessment": assessment,
                            "queries": events,
                            "probes": probes,
                            "ambiguous": ambiguous,
                            "unresolved": unresolved,
                        }
                    ambiguous.append({"url": p["owned"], "assessment": assessment})
                elif not ev.get("ok") and not ev.get("dns_negative"):
                    # Only block on transient failure when the candidate domain itself
                    # is plausibly related to the business. DNS-negative/dead domains
                    # are historical evidence and do not count as a current website.
                    if plausible:
                        unresolved.append({"url": seed, "source": source, "hint": hint, "error": ev.get("error")})

    if queries and usable < min(2, len(queries)):
        status = "SEARCH_INCOMPLETE"
    elif ambiguous or unresolved:
        status = "AMBIGUOUS"
    else:
        status = "CLEAR"
    return {
        "status": status,
        "owned": "",
        "queries": events,
        "probes": probes,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
        "usable_queries": usable,
    }


def classify_strict_safe(row: dict[str, Any], c: dict[str, Any], pe: dict[str, Any], fabric, max_queries: int) -> dict[str, Any]:
    p1_raw = base.strict_pass(c, fabric, 1, max_queries)
    p1, a1 = _sanitize_owned(row, p1_raw)
    if p1.get("owned") and a1.get("confident"):
        row.update({
            "outcome": "REJECT",
            "reason": "OWNED_SITE_FIRST_PARTY_CONFIRMED_PASS1",
            "needs_gpt_review": False,
            "verification_status": "REJECT",
            "verification_provider": "openserp_ci_ownership_safe",
            "owned_website": str(p1.get("owned") or ""),
            "ownership_assessment": a1,
            "web_pass1": p1,
            "web_pass2": {"skipped": True, "reason": "FIRST_PARTY_OWNERSHIP_CONFIRMED_PASS1"},
            "certificate": {"verified": False, "reason": "OWNED_SITE_FIRST_PARTY_PASS1"},
            "certificate_digest": "",
        })
        return row

    p2_raw = base.strict_pass(c, fabric, 2, max_queries)
    p2, a2 = _sanitize_owned(row, p2_raw)
    cert = base.prod.v5.certificate(c, pe, p1, p2)
    ambiguous = [a for a in (a1, a2) if a.get("url") and not a.get("confident")]
    high_challenge: dict[str, Any] = {}

    if p2.get("owned") and a2.get("confident"):
        outcome, reason, verify, review = "REJECT", "OWNED_SITE_FIRST_PARTY_CONFIRMED_PASS2", "REJECT", False
        owned = str(p2.get("owned") or "")
    elif ambiguous:
        outcome, reason, verify, review = "UNCERTAIN", "OWNERSHIP_CANDIDATE_AMBIGUOUS", "UNCERTAIN", True
        owned = ""
        cert["verified"] = False
        cert["ownership_ambiguity"] = ambiguous
    elif not base.prod.v5.coverage(p1).get("ok"):
        outcome, reason, verify, review, owned = "REVIEW", "SEARCH_COVERAGE_INSUFFICIENT_PASS1", "ERROR_RETRYABLE", True, ""
    elif not base.prod.v5.coverage(p2).get("ok"):
        outcome, reason, verify, review, owned = "REVIEW", "SEARCH_COVERAGE_INSUFFICIENT_PASS2", "ERROR_RETRYABLE", True, ""
    elif cert.get("unresolved_plausible_domains"):
        outcome, reason, verify, review, owned = "UNCERTAIN", "PLAUSIBLE_DOMAIN_UNRESOLVED", "UNCERTAIN", True, ""
    elif not cert.get("gates", {}).get("current_identity_strong"):
        outcome, reason, verify, review, owned = "MEDIUM", "IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH", "MEDIUM", True, ""
    elif cert.get("verified"):
        high_challenge = final_high_challenge(row, c, fabric)
        if high_challenge.get("status") == "OWNED_CONFIRMED":
            outcome, reason, verify, review = "REJECT", "OWNED_SITE_FOUND_BY_FINAL_HIGH_CHALLENGE", "REJECT", False
            owned = str(high_challenge.get("owned") or "")
            cert["verified"] = False
            cert["high_challenge_disproved_no_website"] = True
        elif high_challenge.get("status") == "SEARCH_INCOMPLETE":
            outcome, reason, verify, review, owned = "REVIEW", "FINAL_HIGH_CHALLENGE_SEARCH_INCOMPLETE", "ERROR_RETRYABLE", True, ""
            cert["verified"] = False
        elif high_challenge.get("status") == "AMBIGUOUS":
            outcome, reason, verify, review, owned = "UNCERTAIN", "FINAL_HIGH_CHALLENGE_AMBIGUOUS", "UNCERTAIN", True, ""
            cert["verified"] = False
        else:
            outcome, reason, verify, review, owned = "HIGH", "VERIFIED_NO_WEBSITE", "HIGH", True, ""
            cert["high_challenge_clear"] = True
    else:
        outcome, reason, verify, review, owned = "MEDIUM", "SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE", "MEDIUM", True, ""

    row.update({
        "outcome": outcome,
        "reason": reason,
        "needs_gpt_review": review,
        "verification_status": verify,
        "verification_provider": "openserp_ci_ownership_safe",
        "owned_website": owned,
        "ownership_assessment_pass1": a1,
        "ownership_assessment_pass2": a2,
        "ownership_ambiguous_candidates": ambiguous,
        "final_high_challenge": high_challenge,
        "web_pass1": p1,
        "web_pass2": p2,
        "certificate": cert,
        "certificate_digest": str(cert.get("evidence_digest") or ""),
    })
    return row


def main() -> int:
    fast.classify_strict_fast = classify_strict_safe
    return fast.main()


if __name__ == "__main__":
    raise SystemExit(main())
