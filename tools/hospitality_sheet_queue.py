#!/usr/bin/env python3
"""Materialize durable, text-based Sheet-sync queues from the hospitality canonical DB.

GitHub Actions cannot write the private Google Sheet directly without Google credentials.
This bridge persists queue chunks in the repo so a connected Google Sheets agent can
consume them idempotently, dedupe against MASTER, append, verify, then delete chunks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sqlite3
from pathlib import Path


def now_z() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in value)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_record(r: dict, cycle_id: str, kind: str) -> dict:
    out = dict(r)
    out["_sheet_sync_cycle_id"] = cycle_id
    out["_sheet_sync_kind"] = kind
    out["_sheet_sync_queued_at"] = now_z()
    return out


def write_chunk(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def enqueue_partition(partition: Path, queue_dir: Path, cycle_id: str) -> int:
    if not partition.exists():
        return 0
    records: list[dict] = []
    with gzip.open(partition, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            records.append(normalize_record(r, cycle_id, "cycle_delta"))
    if not records:
        return 0
    out = queue_dir / f"cycle-{safe_name(cycle_id)}.jsonl"
    write_chunk(out, records)
    return len(records)


def canonical_records(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM leads ORDER BY first_seen, domain").fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        raw = {}
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except Exception:
            raw = {}
        # Raw worker row is preferred because it carries scoring/contact context.
        r = dict(raw) if isinstance(raw, dict) else {}
        for k in (
            "domain", "name", "country", "region", "city", "state", "street",
            "website", "public_email", "public_phone", "instagram", "live_status",
            "fit_tier", "operator_score", "premium_score", "source_url", "overture_id",
            "source_release", "first_seen", "last_seen",
        ):
            if not r.get(k) and d.get(k) not in (None, ""):
                r[k] = d.get(k)
        out.append(r)
    con.close()
    return out


def bootstrap_once(db_path: Path, queue_dir: Path, state_path: Path, chunk_size: int) -> tuple[int, int]:
    state = load_json(state_path, {"schema_version": 1, "done": False})
    if state.get("done"):
        return 0, 0
    records = canonical_records(db_path)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chunks = 0
    for i in range(0, len(records), chunk_size):
        batch = [normalize_record(r, f"bootstrap-{stamp}", "canonical_bootstrap") for r in records[i:i + chunk_size]]
        chunks += 1
        write_chunk(queue_dir / f"bootstrap-{stamp}-{chunks:03d}.jsonl", batch)
    write_json(state_path, {
        "schema_version": 1,
        "done": True,
        "completed_at": now_z(),
        "records_exported": len(records),
        "chunks_created": chunks,
        "chunk_size": chunk_size,
        "note": "One-time full canonical export for MASTER reconciliation. Consumer must MASTER-dedupe before append."
    })
    return len(records), chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--partition", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--queue-dir", default="gpt/sheet_sync_queue")
    ap.add_argument("--bootstrap-state", default="state/sheet_sync_bootstrap.json")
    ap.add_argument("--bootstrap-chunk-size", type=int, default=400)
    a = ap.parse_args()

    db = Path(a.canonical_db)
    partition = Path(a.partition)
    queue = Path(a.queue_dir)
    state = Path(a.bootstrap_state)
    queue.mkdir(parents=True, exist_ok=True)

    boot_records, boot_chunks = bootstrap_once(db, queue, state, max(50, a.bootstrap_chunk_size))
    delta_records = enqueue_partition(partition, queue, a.cycle_id)
    print(json.dumps({
        "bootstrap_records": boot_records,
        "bootstrap_chunks": boot_chunks,
        "delta_records": delta_records,
        "queue_dir": str(queue),
        "cycle_id": a.cycle_id,
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
