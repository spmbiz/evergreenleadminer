#!/usr/bin/env python3
"""Ownership-safe wrapper around the parallel GWS search verifier.

The legacy verifier can prove that a page contains the business identity, but that
is not the same thing as proving that the business owns the host. This wrapper
keeps the fast/checkpointed engine while adding a separate first-party ownership
gate before any owned-site REJECT is allowed.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import gws_ownership_gate as own
import gws_search_verify as base
import gws_search_verify_fast as fast


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
        # Remove the false first-party assertion while preserving all evidence.
        q["owned"] = ""
        q["owned_identity"] = {}
        q["owned_via"] = "ownership_gate_withheld"
    return q, assessment


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

    if p2.get("owned") and a2.get("confident"):
        outcome, reason, verify, review = "REJECT", "OWNED_SITE_FIRST_PARTY_CONFIRMED_PASS2", "REJECT", False
        owned = str(p2.get("owned") or "")
    elif ambiguous:
        # A plausible page/domain exists, but ownership was not proven. It may be a
        # directory, editorial page, marketplace, mall page, or an unrelated homonym.
        # This MUST block both REJECT and HIGH until semantic/manual resolution.
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
        outcome, reason, verify, review, owned = "HIGH", "VERIFIED_NO_WEBSITE", "HIGH", True, ""
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
        "web_pass1": p1,
        "web_pass2": p2,
        "certificate": cert,
        "certificate_digest": str(cert.get("evidence_digest") or ""),
    })
    return row


def main() -> int:
    # fast.classify_one resolves this global from the imported module at runtime.
    fast.classify_strict_fast = classify_strict_safe
    return fast.main()


if __name__ == "__main__":
    raise SystemExit(main())
