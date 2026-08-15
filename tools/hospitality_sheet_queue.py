#!/usr/bin/env python3
"""Materialize durable, text-based Google Sheet sync queues from canonical hospitality SQLite.

The private MASTER Sheet is not directly writable from GitHub Actions without Google
credentials. This bridge turns canonical state into small repo queue chunks. A connected
Google Sheets agent can consume chunks idempotently, MASTER-dedupe, append+verify, then
delete processed chunks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

SIGNATURE_VERSION = 2
MULTICHANNEL_FIELDS = ("facebook", "contact_page", "whatsapp", "portfolio_url")


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


def enqueue_partition(partition: Path | None, queue_dir: Path, cycle_id: str) -> int:
    if not partition or not partition.exists():
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
    write_chunk(queue_dir / f"cycle-{safe_name(cycle_id)}.jsonl", records)
    return len(records)


def canonical_records(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM leads ORDER BY first_seen, domain").fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except Exception:
            raw = {}
        r = dict(raw) if isinstance(raw, dict) else {}
        for k in (
            "domain", "name", "country", "region", "city", "state", "street",
            "website", "public_email", "public_phone", "instagram", "facebook",
            "contact_page", "whatsapp", "portfolio_url", "live_status",
            "fit_tier", "operator_score", "premium_score", "source_url", "overture_id",
            "source_release", "first_seen", "last_seen",
        ):
            if not r.get(k) and d.get(k) not in (None, ""):
                r[k] = d.get(k)
        out.append(r)
    con.close()
    return out


def hash_fields(r: dict, keys: tuple[str, ...]) -> str:
    payload = "\x1f".join(str(r.get(k) or "").strip() for k in keys)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def signature_v1(r: dict) -> str:
    """Legacy signature used before multichannel fields became first-class."""
    return hash_fields(r, (
        "domain", "name", "country", "region", "city", "state", "street",
        "website", "public_email", "public_phone", "instagram", "live_status",
        "fit_tier", "operator_score", "premium_score", "source_url", "overture_id",
    ))


def signature(r: dict) -> str:
    # Any outreach-relevant enrichment change must create a Sheet delta.
    return hash_fields(r, (
        "domain", "name", "country", "region", "city", "state", "street",
        "website", "public_email", "public_phone", "instagram", "facebook",
        "contact_page", "whatsapp", "portfolio_url", "live_status",
        "fit_tier", "operator_score", "premium_score", "source_url", "overture_id",
    ))


def chunk_records(records: list[dict], queue_dir: Path, prefix: str, kind: str, chunk_size: int) -> int:
    chunks = 0
    for i in range(0, len(records), chunk_size):
        batch = [normalize_record(r, prefix, kind) for r in records[i:i + chunk_size]]
        chunks += 1
        write_chunk(queue_dir / f"{safe_name(prefix)}-{chunks:03d}.jsonl", batch)
    return chunks


def snapshot_diff(db_path: Path, queue_dir: Path, state_path: Path, cycle_id: str, chunk_size: int) -> tuple[int, int, bool]:
    records = canonical_records(db_path)
    current = {str(r.get("domain") or "").strip().lower(): signature(r) for r in records if r.get("domain")}
    state = load_json(state_path, {"schema_version": 1, "initialized": False, "signatures": {}})
    previous = state.get("signatures") or {}
    prior_sig_version = int(state.get("signature_version") or 1)
    first = not bool(state.get("initialized"))

    if first:
        changed = records
        kind = "canonical_bootstrap"
        prefix = f"bootstrap-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    else:
        changed = []
        for r in records:
            domain = str(r.get("domain") or "").strip().lower()
            if not domain:
                continue
            prev = previous.get(domain)
            cur = current.get(domain)
            if prior_sig_version >= SIGNATURE_VERSION:
                if cur != prev:
                    changed.append(r)
                continue

            # One-time v1 -> v2 migration. If the legacy signature still matches,
            # do NOT replay the entire canonical just because the hash schema grew.
            # Queue it only when a newly tracked multichannel value actually exists.
            if prev == signature_v1(r):
                if any(str(r.get(k) or "").strip() for k in MULTICHANNEL_FIELDS):
                    changed.append(r)
            else:
                # A real old-field change/new record happened since prior snapshot.
                changed.append(r)
        kind = "canonical_delta"
        prefix = f"delta-{safe_name(cycle_id)}"

    chunks = chunk_records(changed, queue_dir, prefix, kind, chunk_size) if changed else 0
    write_json(state_path, {
        "schema_version": 1,
        "signature_version": SIGNATURE_VERSION,
        "initialized": True,
        "updated_at": now_z(),
        "cycle_id": cycle_id,
        "canonical_records": len(records),
        "queued_records_this_pass": len(changed),
        "chunks_created_this_pass": chunks,
        "signatures": current,
        "note": "Queue only. Google Sheet consumer must MASTER-dedupe and verify before deleting queue chunks. Multichannel fields are signature-tracked; v1 migration avoids a full canonical replay."
    })
    return len(changed), chunks, first


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--partition")
    ap.add_argument("--queue-dir", default="gpt/sheet_sync_queue")
    ap.add_argument("--snapshot-state", default="state/sheet_sync_snapshot.json")
    ap.add_argument("--chunk-size", type=int, default=400)
    a = ap.parse_args()

    db = Path(a.canonical_db)
    queue = Path(a.queue_dir)
    queue.mkdir(parents=True, exist_ok=True)
    chunk_size = max(50, int(a.chunk_size))

    queued, chunks, first = snapshot_diff(db, queue, Path(a.snapshot_state), a.cycle_id, chunk_size)
    partition_records = enqueue_partition(Path(a.partition) if a.partition else None, queue, a.cycle_id)
    print(json.dumps({
        "snapshot_bootstrap": first,
        "snapshot_records_queued": queued,
        "snapshot_chunks_created": chunks,
        "partition_records_queued": partition_records,
        "queue_dir": str(queue),
        "cycle_id": a.cycle_id,
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
