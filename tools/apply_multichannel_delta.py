#!/usr/bin/env python3
"""Apply asynchronous multichannel enrichment deltas to the latest canonical DB.

Safety rules:
- domain is the join key;
- enrichment fields are fill-only (never overwrite a non-empty canonical value);
- raw_json enrichment is monotonic;
- attempt/success timestamps only move forward;
- missing domains are ignored and counted rather than recreated;
- the caller must hold the repository-wide hospitality-canonical-writer lock.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path

TARGET_FIELDS = ("instagram", "facebook", "contact_page", "whatsapp", "portfolio_url")


def norm(v) -> str:
    return " ".join(str(v or "").split()).strip()


def ensure_schema(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    wanted = {
        "facebook": "TEXT",
        "contact_page": "TEXT",
        "whatsapp": "TEXT",
        "portfolio_url": "TEXT",
        "multichannel_last_attempt": "TEXT",
        "multichannel_last_success": "TEXT",
        "multichannel_status": "TEXT",
    }
    for name, typ in wanted.items():
        if name not in cols:
            con.execute(f"ALTER TABLE leads ADD COLUMN {name} {typ}")
    con.execute("""CREATE TABLE IF NOT EXISTS multichannel_runs(
        cycle_id TEXT PRIMARY KEY,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        batch_size INTEGER,
        workers INTEGER,
        attempted INTEGER,
        domains_enriched INTEGER,
        field_values_added INTEGER,
        instagram_added INTEGER,
        facebook_added INTEGER,
        contact_page_added INTEGER,
        whatsapp_added INTEGER,
        portfolio_url_added INTEGER,
        pages_fetched INTEGER,
        failed INTEGER,
        failure_rate REAL,
        elapsed_seconds REAL,
        useful_per_productive_minute REAL,
        incomplete_domains_remaining INTEGER,
        raw_json TEXT
    )""")
    con.commit()


def later(a: str, b: str) -> str:
    a = norm(a)
    b = norm(b)
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--delta", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--out-summary", required=True)
    a = ap.parse_args()

    con = sqlite3.connect(a.canonical_db)
    ensure_schema(con)
    con.row_factory = sqlite3.Row

    records = []
    with gzip.open(a.delta, "rt", encoding="utf-8") as z:
        for line in z:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass

    rows_seen = 0
    rows_changed = 0
    domains_missing = 0
    actual_field_adds = {k: 0 for k in TARGET_FIELDS}

    for rec in records:
        domain = norm(rec.get("domain")).lower()
        if not domain:
            continue
        row = con.execute(
            "SELECT instagram,facebook,contact_page,whatsapp,portfolio_url,raw_json,"
            "multichannel_last_attempt,multichannel_last_success,multichannel_status "
            "FROM leads WHERE domain=?",
            (domain,),
        ).fetchone()
        if not row:
            domains_missing += 1
            continue
        rows_seen += 1

        current = {k: norm(row[k]) for k in TARGET_FIELDS}
        final = dict(current)
        dirty = False
        gained_now = []
        for key in TARGET_FIELDS:
            candidate = norm(rec.get(key))
            if not final[key] and candidate:
                final[key] = candidate
                gained_now.append(key)
                actual_field_adds[key] += 1
                dirty = True

        try:
            raw = json.loads(row["raw_json"] or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for key in TARGET_FIELDS:
            if final[key] and not norm(raw.get(key)):
                raw[key] = final[key]
                dirty = True

        attempted_at = norm(rec.get("attempted_at"))
        next_attempt = later(row["multichannel_last_attempt"], attempted_at)
        if next_attempt != norm(row["multichannel_last_attempt"]):
            dirty = True

        found_any = bool(list(rec.get("gained") or []))
        next_success = later(row["multichannel_last_success"], attempted_at if found_any else "")
        if next_success != norm(row["multichannel_last_success"]):
            dirty = True

        status = norm(rec.get("status")) or norm(row["multichannel_status"])
        if status != norm(row["multichannel_status"]):
            dirty = True

        raw["multichannel_enrichment_status"] = status
        raw["multichannel_enrichment_reason"] = norm(rec.get("reason"))
        if attempted_at:
            raw["multichannel_enrichment_last_attempt"] = attempted_at

        con.execute(
            "UPDATE leads SET instagram=?,facebook=?,contact_page=?,whatsapp=?,portfolio_url=?,"
            "multichannel_last_attempt=?,multichannel_last_success=?,multichannel_status=?,raw_json=? "
            "WHERE domain=?",
            (
                final["instagram"], final["facebook"], final["contact_page"],
                final["whatsapp"], final["portfolio_url"], next_attempt, next_success,
                status, json.dumps(raw, ensure_ascii=False), domain,
            ),
        )
        if dirty:
            rows_changed += 1

    con.commit()

    source_summary = load_json(a.summary) if a.summary else {}
    remaining = con.execute(
        "SELECT COUNT(*) FROM leads WHERE website IS NOT NULL AND website<>'' AND "
        "(COALESCE(instagram,'')='' OR COALESCE(facebook,'')='' OR COALESCE(contact_page,'')='' "
        "OR COALESCE(whatsapp,'')='' OR COALESCE(portfolio_url,'')='')"
    ).fetchone()[0]

    attempted = int(source_summary.get("attempted") or rows_seen)
    failed = int(source_summary.get("failed") or 0)
    elapsed = float(source_summary.get("elapsed_seconds") or 0)
    enriched = int(source_summary.get("domains_enriched") or 0)
    field_total = sum(actual_field_adds.values())
    failure_rate = round(failed / attempted, 5) if attempted else 0.0
    useful_per_minute = round(enriched / max(elapsed / 60.0, 1e-9), 3) if elapsed else 0.0

    con.execute(
        """INSERT OR REPLACE INTO multichannel_runs(
            cycle_id,batch_size,workers,attempted,domains_enriched,field_values_added,
            instagram_added,facebook_added,contact_page_added,whatsapp_added,portfolio_url_added,
            pages_fetched,failed,failure_rate,elapsed_seconds,useful_per_productive_minute,
            incomplete_domains_remaining,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            a.cycle_id,
            int(source_summary.get("batch_size") or 0),
            int(source_summary.get("workers") or 0),
            attempted,
            enriched,
            field_total,
            actual_field_adds["instagram"],
            actual_field_adds["facebook"],
            actual_field_adds["contact_page"],
            actual_field_adds["whatsapp"],
            actual_field_adds["portfolio_url"],
            int(source_summary.get("pages_fetched") or 0),
            failed,
            failure_rate,
            elapsed,
            useful_per_minute,
            int(remaining),
            json.dumps(source_summary, ensure_ascii=False),
        ),
    )
    con.commit()
    con.close()

    out = {
        "cycle_id": a.cycle_id,
        "delta_records": len(records),
        "domains_present": rows_seen,
        "domains_missing": domains_missing,
        "rows_changed": rows_changed,
        "actual_field_values_added": field_total,
        "field_adds": actual_field_adds,
        "incomplete_domains_remaining": int(remaining),
    }
    Path(a.out_summary).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_summary).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
