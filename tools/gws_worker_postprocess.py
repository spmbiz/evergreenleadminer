#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

OUTCOME_RANK = {
    "REVIEW": 60,
    "HIGH": 60,
    "MEDIUM": 50,
    "REJECT": 40,
    "UNCERTAIN": 20,
    "ERROR_RETRYABLE": 10,
    "ERROR_HARD": 5,
}


def canonical_key(row: dict) -> str:
    overture_id = str(row.get("overture_id") or "").strip()
    if overture_id:
        return f"overture:{overture_id}"
    oid = str(row.get("hub_objectid") or "").strip()
    if oid.startswith("osm:") or oid.startswith("overture:"):
        return oid
    return f"hub:{oid}" if oid else str(row.get("record_key") or "")


def quality(row: dict) -> tuple:
    sim = row.get("name_similarity")
    dist = row.get("distance_m")
    try:
        simf = float(sim)
    except Exception:
        simf = -1.0
    try:
        distf = float(dist)
    except Exception:
        distf = 1e9
    return (OUTCOME_RANK.get(str(row.get("outcome") or ""), 0), simf, -distf)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", required=True)
    args = ap.parse_args()
    shard = Path(args.shard_dir)
    path = shard / "records.jsonl"
    if not path.exists():
        print(json.dumps({"status": "noop", "reason": "no_records"}))
        return 0

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            row["record_key"] = canonical_key(row)
            rows.append(row)

    best: dict[str, dict] = {}
    for row in rows:
        key = row.get("record_key") or ""
        prior = best.get(key)
        if prior is None or quality(row) > quality(prior):
            best[key] = row

    kept = list(best.values())
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in kept), encoding="utf-8")
    csv_path = shard / "records.csv"
    # The JSONL is canonical transport; remove stale CSV rather than allowing mismatched duplicate counts.
    if csv_path.exists() and len(kept) != len(rows):
        csv_path.unlink()
    print(json.dumps({"status": "ok", "input_records": len(rows), "canonical_records": len(kept), "duplicates_collapsed": len(rows) - len(kept)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
