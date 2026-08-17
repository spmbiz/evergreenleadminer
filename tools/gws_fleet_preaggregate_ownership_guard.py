#!/usr/bin/env python3
"""Fail-safe guard immediately before the autonomous GWS single writer.

Two independent protections live here:
1. an explicitly cancelled run is stopped before any source-state or canonical
   aggregation write, even when GitHub schedules the aggregate through `if: always()`;
2. an unproven owned-site REJECT is quarantined to UNCERTAIN + review.

This module never creates HIGH.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import gws_ownership_gate as ownership


def _explicit_cancel_target() -> str:
    try:
        data = json.loads(Path("control/gws_cancel_run.json").read_text(encoding="utf-8"))
        return str(data.get("run_id") or "").strip()
    except Exception:
        return ""


def assert_not_explicitly_cancelled() -> None:
    current = str(os.getenv("GITHUB_RUN_ID") or "").strip()
    target = _explicit_cancel_target()
    if current and target and current == target:
        reason = ""
        try:
            reason = str(json.loads(Path("control/gws_cancel_run.json").read_text(encoding="utf-8")).get("reason") or "")
        except Exception:
            pass
        print("GWS_PREAGGREGATE_CANCEL_GUARD=" + json.dumps({"run_id": current, "cancel_target": target, "blocked": True, "reason": reason}, separators=(",", ":")))
        raise SystemExit(f"Explicitly cancelled GWS run {current}; refusing pre-aggregate persistence")


def ownership_pass_for(row: dict) -> dict:
    reason = str(row.get("reason") or "").upper()
    ordered = [row.get("web_pass2") or {}, row.get("web_pass1") or {}] if "PASS2" in reason else [row.get("web_pass1") or {}, row.get("web_pass2") or {}]
    for p in ordered:
        if p.get("owned"):
            return p
    return {
        "owned": str(row.get("owned_website") or ""),
        "owned_identity": row.get("owned_identity") or {},
        "owned_via": "preaggregate_fallback",
    }


def guard(row: dict) -> tuple[dict, bool]:
    r = dict(row)
    reason = str(r.get("reason") or "").upper()
    if str(r.get("outcome") or "").upper() != "REJECT" or ("OWNED_SITE" not in reason and "OWNED WEBSITE" not in reason):
        return r, False

    assessment = ownership.assess(r, ownership_pass_for(r))
    r["preaggregate_ownership_assessment"] = assessment
    if assessment.get("confident"):
        r["preaggregate_ownership_guard_passed"] = True
        return r, False

    r["preaggregate_ownership_guard_passed"] = False
    r["ownership_quarantine_previous"] = {
        "outcome": r.get("outcome"),
        "reason": r.get("reason"),
        "verification_status": r.get("verification_status"),
        "owned_website": r.get("owned_website"),
    }
    r["outcome"] = "UNCERTAIN"
    r["reason"] = "OWNERSHIP_REJECT_QUARANTINED_PREAGGREGATE"
    r["verification_status"] = "UNCERTAIN"
    r["owned_website"] = ""
    r["needs_gpt_review"] = True
    r["ownership_ambiguous_candidates"] = [assessment]
    cert = dict(r.get("certificate") or {})
    cert["verified"] = False
    cert["ownership_quarantined"] = True
    r["certificate"] = cert
    r["certificate_digest"] = ""
    return r, True


def main() -> int:
    assert_not_explicitly_cancelled()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results/shards")
    args = ap.parse_args()
    files = sorted(Path(args.root).rglob("records.jsonl"))
    scanned = quarantined = 0
    for path in files:
        rows = []
        changed = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            scanned += 1
            row, q = guard(json.loads(line))
            quarantined += int(q)
            changed = changed or q
            rows.append(row)
        if changed:
            path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    print("GWS_PREAGGREGATE_OWNERSHIP_GUARD=" + json.dumps({"files": len(files), "rows": scanned, "quarantined": quarantined}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
