#!/usr/bin/env python3
"""Export a compact, auditable strict-HIGH handoff for canonical persistence.

This does not mutate canonical state. It only materializes records that the
strict verifier already classified HIGH/VERIFIED_NO_WEBSITE with a verified
certificate and clear final challenge.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    src = Path(args.input)
    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if str(r.get("verification_status") or "").upper() != "HIGH":
            continue
        if str(r.get("reason") or "").upper() != "VERIFIED_NO_WEBSITE":
            continue
        cert = r.get("certificate") or {}
        if not cert.get("verified"):
            continue
        challenge = r.get("final_high_challenge") or {}
        # Old certified rows may predate this field; current ownership-safe rows
        # must explicitly prove the challenge CLEAR before canonical handoff.
        if str(r.get("verification_provider") or "").endswith("ownership_safe") and challenge.get("status") != "CLEAR":
            continue
        rows.append({
            "record_key": r.get("record_key") or r.get("hub_objectid") or "",
            "fingerprint": r.get("fingerprint") or "",
            "certificate_digest": r.get("certificate_digest") or cert.get("evidence_digest") or "",
            "verification_status": "HIGH",
            "reason": "VERIFIED_NO_WEBSITE",
            "verification_provider": r.get("verification_provider") or "",
            "observed_at": r.get("observed_at") or "",
            "fleet_run_id": r.get("fleet_run_id") or "",
            "task_id": r.get("task_id") or "",
            "business_name": r.get("hub_name") or r.get("overture_name") or "",
            "category": r.get("hub_type") or r.get("overture_category") or r.get("hub_category") or "",
            "family": r.get("family") or "",
            "territory": r.get("territory") or "",
            "address": r.get("hub_address") or r.get("overture_address") or "",
            "postalcode": r.get("hub_postalcode") or "",
            "phone": r.get("hub_phone") or r.get("overture_phone") or "",
            "email": r.get("hub_email") or r.get("overture_email") or "",
            "facebook": next((x for x in json.loads(r.get("overture_socials") or "[]") if "facebook.com" in str(x)), "") if isinstance(r.get("overture_socials"), str) else "",
            "instagram": next((x for x in json.loads(r.get("overture_socials") or "[]") if "instagram.com" in str(x)), "") if isinstance(r.get("overture_socials"), str) else "",
            "maps": r.get("hub_google_maps") or "",
            "owned_website": r.get("owned_website") or "",
            "evidence_digest": cert.get("evidence_digest") or "",
            "search_pass1": cert.get("pass1") or {},
            "search_pass2": cert.get("pass2") or {},
            "high_challenge": challenge,
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    print("GWS_CANONICAL_READY_EXPORT=" + json.dumps({"input": str(src), "strict_high": len(rows), "out": str(out)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
