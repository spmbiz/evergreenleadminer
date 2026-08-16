#!/usr/bin/env python3
"""Adapt current flattened GWS pending records to the legacy-compatible home worker input.

This is an evidence-only bridge. It preserves current record identity/fingerprint and
never changes strict verification state or writes canonical stores.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def stable_r(key: str, used: set[int]) -> int:
    base = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16) % 2_000_000_000
    r = max(1, base)
    while r in used:
        r += 1
    used.add(r)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()

    src = Path(a.input)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    out = []
    used: set[int] = set()
    skipped = 0
    for row in rows:
        key = str(row.get("record_key") or row.get("hub_objectid") or "").strip()
        name = str(row.get("hub_name") or row.get("overture_name") or "").strip()
        postcode = str(row.get("hub_postalcode") or "").strip()
        address = str(row.get("hub_address") or "").strip()
        if not key or not name or not postcode:
            skipped += 1
            continue
        oid = str(row.get("overture_id") or "").strip()
        if not oid and key.startswith("overture:"):
            oid = key.split(":", 1)[1]
        r = stable_r(key, used)
        candidate = {
            "r": r,
            "n": name,
            "p": postcode,
            "a": address,
            "ph": str(row.get("overture_phone") or ""),
            "em": str(row.get("overture_email") or ""),
            "alias": str(row.get("overture_name") or name),
            "record_key": key,
            "fingerprint": str(row.get("fingerprint") or ""),
            "territory": str(row.get("territory") or ""),
            "family": str(row.get("family") or ""),
            "observed_at": str(row.get("observed_at") or ""),
        }
        place = {
            "overture_id": oid,
            "overture_name": str(row.get("overture_name") or name),
            "overture_phone": str(row.get("overture_phone") or ""),
            "overture_email": str(row.get("overture_email") or ""),
            "overture_websites": row.get("overture_websites") or "",
            "overture_brand": row.get("overture_brand") or "",
            "overture_category": row.get("overture_category") or "",
            "overture_confidence": row.get("overture_confidence") or "",
        }
        out.append({"r": r, "candidate": candidate, "place": place})

    dest = Path(a.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in out), encoding="utf-8")
    print("GWS_HOME_PENDING_ADAPTER=" + json.dumps({"input": len(rows), "output": len(out), "skipped": skipped}, separators=(",", ":")))


if __name__ == "__main__":
    main()
