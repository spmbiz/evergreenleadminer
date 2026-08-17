#!/usr/bin/env python3
"""One-shot durable repair for four proven non-business technical HIGHs.

The stale pre-token-safe Overture mapper classified public_* categories as Pub
because of substring matching. This script never promotes anything; it converts
only the four audited record keys below to a terminal scope reject inside the
review batch, preserves their former strict result as audit evidence, and
reconciles pending metadata from the actual batch contents.
"""
from __future__ import annotations

import json
from pathlib import Path

BATCH = Path("gpt/gws_review/2026-08-17T023942Z_31986876407-1.jsonl")
PENDING = Path("gpt/gws_pending_batches.json")
TARGETS = {
    "overture:92af277e-ec0c-4291-a26c-a2b0c3bca6fc": "public_utility_company",
    "overture:0b2ac67d-7d0c-4eab-b1db-20a523233073": "public_plaza",
    "overture:c684eba7-b4dc-4a54-aa9d-1928d501a721": "public_plaza",
    "overture:a2b25c6b-4fc9-4668-9143-e1083fdbe668": "public_plaza",
}

TERMINAL = {"HIGH", "REJECT", "DUPLICATE", "ERROR_HARD"}


def load_rows(path: Path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def main() -> int:
    if not BATCH.exists():
        raise SystemExit(f"missing batch {BATCH}")
    rows = load_rows(BATCH)
    found = set()
    changed = 0
    for row in rows:
        key = str(row.get("record_key") or row.get("hub_objectid") or "")
        if key not in TARGETS:
            continue
        found.add(key)
        category = str(row.get("overture_category") or "")
        if category != TARGETS[key]:
            raise SystemExit(f"category mismatch for {key}: {category!r} != {TARGETS[key]!r}")
        if str(row.get("verification_status") or "").upper() != "HIGH":
            raise SystemExit(f"expected stale HIGH for {key}, got {row.get('verification_status')!r}")
        row["scope_quarantine_previous"] = {
            "outcome": row.get("outcome"),
            "verification_status": row.get("verification_status"),
            "reason": row.get("reason"),
            "certificate_digest": row.get("certificate_digest"),
            "hub_type": row.get("hub_type"),
            "overture_category": category,
        }
        row["outcome"] = "REJECT"
        row["verification_status"] = "REJECT"
        row["reason"] = "REJECT_SCOPE_NON_BUSINESS_SOURCE_MAPPING_BUG"
        row["needs_gpt_review"] = False
        row["owned_website"] = ""
        cert = dict(row.get("certificate") or {})
        cert["verified"] = False
        cert["scope_invalid"] = True
        cert["scope_invalid_reason"] = "OVERTURE_PUBLIC_CATEGORY_MISROUTED_BY_PRE_TOKEN_SAFE_MAPPER"
        row["certificate"] = cert
        row["certificate_digest"] = ""
        row["scope_quarantine_audit"] = {
            "source_run_id": "31986876407",
            "mapping_bug": "substring pub matched public_*",
            "corrected_mapper_commit": "e6c2f4f20b1b04429770fa18d0d4c0de00ac8dd0",
            "category": category,
        }
        changed += 1

    missing = sorted(set(TARGETS) - found)
    if missing:
        raise SystemExit("missing audited keys from review batch: " + ",".join(missing))
    if changed != 4:
        raise SystemExit(f"expected 4 corrections, got {changed}")

    BATCH.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")

    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    target_batch = None
    total_pending = 0
    for b in pending.get("batches") or []:
        if str(b.get("batch") or "") == str(BATCH):
            target_batch = b
            b["verification_total"] = len(rows)
            b["verification_remaining"] = sum(
                1 for r in rows
                if str(r.get("verification_status") or "").upper() not in TERMINAL
                and str(r.get("outcome") or "").upper() != "REJECT"
            )
        if b.get("status") != "pending":
            continue
        remaining = b.get("verification_remaining")
        total_pending += int(b.get("records") or 0) if remaining is None else int(remaining or 0)
    if target_batch is None:
        raise SystemExit("target batch metadata missing")
    pending["pending_records"] = total_pending
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    receipt = {
        "schema": "gws-scope-quarantine-v1",
        "source_run_id": "31986876407",
        "batch": str(BATCH),
        "corrected": changed,
        "record_keys": sorted(TARGETS),
        "pending_records_after": total_pending,
        "canonical_sheet_mutation": False,
        "reason": "pre-token-safe Overture substring mapper routed public categories into bars_nightlife",
    }
    out = Path("state/gws_scope_quarantine_31986876407.json")
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("GWS_SCOPE_QUARANTINE=" + json.dumps(receipt, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
