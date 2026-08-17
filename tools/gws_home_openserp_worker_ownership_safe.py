#!/usr/bin/env python3
"""Ownership-safe wrapper for the residential GWS OpenSERP evidence worker.

The underlying worker remains evidence-only and never declares canonical HIGH.
This wrapper changes one thing only: an identity match may become terminal
REJECT evidence only when the shared ownership gate confirms first-party
ownership. Directory, booking, social, marketplace, editorial, and unbranded
identity pages stay as evidence and the worker continues searching.
"""
from __future__ import annotations

from typing import Any

import gws_home_openserp_worker_v55 as base
import gws_ownership_gate as own

_ORIGINAL_PROBE_HOST = base.probe_host


def _ownership_row(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "hub_name": str(candidate.get("n") or candidate.get("alias") or ""),
        "candidate": candidate,
    }


def _safe_probe_host(candidate: dict[str, Any], seed: str) -> dict[str, Any]:
    """Withhold identity matches that do not prove first-party ownership.

    This runs *inside* the base worker's candidate loop. That is important:
    simply clearing ``owned`` after a pass would be too late because the base
    worker stops probing after its first identity match. Withholding here lets
    it continue to later SERP candidates and still find a real owned site.
    """
    ev = dict(_ORIGINAL_PROBE_HOST(candidate, seed) or {})
    if not ev.get("matched"):
        return ev

    url = str(ev.get("final") or seed or "")
    assessment = own.assess(
        _ownership_row(candidate),
        {
            "owned": url,
            "owned_identity": ev.get("identity") or {},
            "owned_via": "residential_probe",
        },
    )
    ev["ownership_assessment"] = assessment
    if assessment.get("confident"):
        return ev

    ev["identity_match_withheld"] = {
        "url": url,
        "identity": ev.get("identity") or {},
        "assessment": assessment,
        "reason": "IDENTITY_MATCH_IS_NOT_FIRST_PARTY_OWNERSHIP",
    }
    ev["matched"] = False
    return ev


def install() -> None:
    base.probe_host = _safe_probe_host


def main() -> None:
    install()
    base.main()


if __name__ == "__main__":
    main()
