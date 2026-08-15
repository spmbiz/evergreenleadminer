#!/usr/bin/env python3
"""Run multichannel HTTP enrichment on a canonical snapshot and emit a delta.

This worker is intentionally read-only with respect to the durable canonical DB.
It may add compatibility columns to its local snapshot, but it never publishes
that snapshot. Durable writes happen later through apply_multichannel_delta.py
under the canonical-writer lock.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import canonical_multichannel_enrich as cm


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--delta-out", required=True)
    ap.add_argument("--summary-out", required=True)
    ap.add_argument("--batch-size", type=int, default=2800)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--max-bytes", type=int, default=750000)
    ap.add_argument("--retry-hours", type=int, default=72)
    a = ap.parse_args()

    t0 = time.time()
    db = Path(a.canonical_db)
    delta_out = Path(a.delta_out)
    summary_out = Path(a.summary_out)
    delta_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db)
    cm.ensure_schema(con)
    rows = cm.pick_rows(con, a.batch_size, a.retry_hours)
    incomplete_snapshot = con.execute(
        "SELECT COUNT(*) FROM leads WHERE website IS NOT NULL AND website<>'' AND "
        "(COALESCE(instagram,'')='' OR COALESCE(facebook,'')='' OR COALESCE(contact_page,'')='' "
        "OR COALESCE(whatsapp,'')='' OR COALESCE(portfolio_url,'')='')"
    ).fetchone()[0]
    con.close()

    attempted_at = cm.now_z()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = {
            ex.submit(cm.enrich_one, row, a.timeout, a.max_pages, a.max_bytes): row
            for row in rows
        }
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                res = {
                    "domain": row.get("domain") or "",
                    "status": "FAILED",
                    "reason": type(exc).__name__,
                    "pages_fetched": 0,
                    "gained": [],
                    **{k: cm.norm(row.get(k)) for k in cm.TARGET_FIELDS},
                }
            res["attempted_at"] = attempted_at
            results.append(res)

    results.sort(key=lambda r: str(r.get("domain") or ""))
    with gzip.open(delta_out, "wt", encoding="utf-8") as z:
        for res in results:
            z.write(json.dumps(res, ensure_ascii=False, separators=(",", ":")) + "\n")

    gained_counts = {k: 0 for k in cm.TARGET_FIELDS}
    enriched_domains = 0
    for res in results:
        gained = list(res.get("gained") or [])
        if gained:
            enriched_domains += 1
        for key in gained:
            if key in gained_counts:
                gained_counts[key] += 1

    summary = {
        "attempted": len(rows),
        "delta_records": len(results),
        "domains_enriched": enriched_domains,
        "instagram_added": gained_counts["instagram"],
        "facebook_added": gained_counts["facebook"],
        "contact_page_added": gained_counts["contact_page"],
        "whatsapp_added": gained_counts["whatsapp"],
        "portfolio_url_added": gained_counts["portfolio_url"],
        "pages_fetched": sum(int(r.get("pages_fetched") or 0) for r in results),
        "failed": sum(r.get("status") == "FAILED" for r in results),
        "incomplete_domains_snapshot": int(incomplete_snapshot),
        "retry_hours": a.retry_hours,
        "batch_size": a.batch_size,
        "workers": a.workers,
        "attempted_at": attempted_at,
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    summary_out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
