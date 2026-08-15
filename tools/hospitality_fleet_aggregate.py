#!/usr/bin/env python3
"""Hospitality aggregate wrapper.

Keeps fleet_runtime as the single canonical implementation, fixes registrable-
domain handling for common multi-label public suffixes, preserves monotonic
multichannel enrichment, persists lane-specific health, and runs a bounded
second-pass enrichment against the same single-writer canonical SQLite.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
MULTI_SUFFIXES = {
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk",
    "com.au","net.au","org.au","id.au","asn.au",
    "co.nz","net.nz","org.nz",
    "com.br","net.br","org.br","com.mx","com.ar","com.co","com.pe","com.ec","com.uy","com.py","com.ve",
    "co.za","org.za","net.za","com.sg","com.hk","com.tr","com.gr","com.cy","com.mt",
    "co.cr","com.pa","com.do","com.gt","com.hn","com.sv","com.ni",
    "co.il","com.my","co.th","com.ph","com.tw","com.cn","com.jp","co.jp","ne.jp",
}
MULTICHANNEL_BATCH_SIZE = 2800
MULTICHANNEL_WORKERS = 64
MONOTONIC_RAW_FIELDS = (
    "facebook", "facebook_source_url", "contact_page", "whatsapp", "portfolio_url",
    "instagram", "instagram_source_url", "email_source_url",
)


def registrable_domain(host: str) -> str:
    h = (host or "").lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    parts = h.split(".")
    if len(parts) < 2:
        return h
    last2 = ".".join(parts[-2:])
    if last2 in MULTI_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def weighted(summaries, key: str, weight_key: str) -> float:
    denom = sum(max(0, int(s.get(weight_key) or 0)) for s in summaries)
    if not denom:
        return 0.0
    return sum(float(s.get(key) or 0) * max(0, int(s.get(weight_key) or 0)) for s in summaries) / denom


def snapshot_monotonic_fields(canonical_db: str) -> dict[str, dict]:
    db = Path(canonical_db)
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    try:
        out = {}
        for domain, raw_json in con.execute("SELECT domain, raw_json FROM leads"):
            try:
                raw = json.loads(raw_json or "{}")
            except Exception:
                raw = {}
            keep = {k: raw.get(k) for k in MONOTONIC_RAW_FIELDS if str(raw.get(k) or "").strip()}
            if keep:
                out[str(domain)] = keep
        return out
    finally:
        con.close()


def restore_monotonic_fields(canonical_db: str, snapshot: dict[str, dict]) -> int:
    """Never let a poorer later observation erase explicit public enrichment."""
    if not snapshot or not Path(canonical_db).exists():
        return 0
    con = sqlite3.connect(canonical_db)
    changed = 0
    try:
        for domain, prior in snapshot.items():
            row = con.execute("SELECT raw_json FROM leads WHERE domain=?", (domain,)).fetchone()
            if not row:
                continue
            try:
                raw = json.loads(row[0] or "{}")
            except Exception:
                raw = {}
            dirty = False
            for key, value in prior.items():
                if value and not str(raw.get(key) or "").strip():
                    raw[key] = value
                    dirty = True
            if dirty:
                con.execute("UPDATE leads SET raw_json=? WHERE domain=?", (json.dumps(raw, ensure_ascii=False), domain))
                changed += 1
        con.commit()
        return changed
    finally:
        con.close()


def ensure_requests() -> None:
    try:
        import requests  # noqa: F401
        return
    except Exception:
        pass
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "requests"],
        cwd=ROOT,
        check=True,
    )


def persist_multichannel_benchmark(canonical_db: str, outdir: str, cycle_id: str) -> None:
    summary_path = Path(outdir) / "multichannel_enrichment_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return
    elapsed = float(summary.get("elapsed_seconds") or 0)
    enriched = int(summary.get("domains_enriched") or 0)
    attempted = int(summary.get("attempted") or 0)
    failed = int(summary.get("failed") or 0)
    field_adds = sum(int(summary.get(k) or 0) for k in (
        "instagram_added", "facebook_added", "contact_page_added", "whatsapp_added", "portfolio_url_added"
    ))
    useful_per_minute = round(enriched / max(elapsed / 60.0, 1e-9), 3) if elapsed else 0.0
    failure_rate = round(failed / attempted, 5) if attempted else 0.0
    con = sqlite3.connect(canonical_db)
    try:
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
        con.execute(
            """INSERT OR REPLACE INTO multichannel_runs(
                cycle_id,batch_size,workers,attempted,domains_enriched,field_values_added,
                instagram_added,facebook_added,contact_page_added,whatsapp_added,portfolio_url_added,
                pages_fetched,failed,failure_rate,elapsed_seconds,useful_per_productive_minute,
                incomplete_domains_remaining,raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cycle_id, MULTICHANNEL_BATCH_SIZE, MULTICHANNEL_WORKERS, attempted, enriched, field_adds,
                int(summary.get("instagram_added") or 0), int(summary.get("facebook_added") or 0),
                int(summary.get("contact_page_added") or 0), int(summary.get("whatsapp_added") or 0),
                int(summary.get("portfolio_url_added") or 0), int(summary.get("pages_fetched") or 0),
                failed, failure_rate, elapsed, useful_per_minute,
                int(summary.get("incomplete_domains_remaining") or 0), json.dumps(summary, ensure_ascii=False),
            ),
        )
        con.commit()
    finally:
        con.close()


def run_multichannel_enrichment(canonical_db: str, outdir: str, cycle_id: str) -> None:
    try:
        ensure_requests()
        subprocess.run(
            [
                sys.executable,
                "tools/canonical_multichannel_enrich.py",
                "--canonical-db", canonical_db,
                "--outdir", outdir,
                "--batch-size", str(MULTICHANNEL_BATCH_SIZE),
                "--workers", str(MULTICHANNEL_WORKERS),
                "--timeout", "6",
                "--max-pages", "3",
                "--max-bytes", "750000",
                "--retry-hours", "72",
                "--metrics", "metrics/latest.json",
            ],
            cwd=ROOT,
            check=True,
        )
        persist_multichannel_benchmark(canonical_db, outdir, cycle_id)
    except Exception as exc:
        print(f"multichannel enrichment degraded but harvest remains valid: {type(exc).__name__}: {exc}", file=sys.stderr)


def persist_lane_health(results_root: str) -> None:
    root = Path(results_root)
    summaries = [fr.load_json(p, {}) for p in root.rglob("worker_summary.json")]
    if not summaries:
        return
    grouped = {}
    for s in summaries:
        lane = str(s.get("lane") or "fast_email")
        grouped.setdefault(lane, []).append(s)

    source_path = ROOT / "state/source_state.json"
    source_doc = fr.load_json(source_path, {"schema_version": 1})
    lane_doc = source_doc.setdefault("hospitality_lanes", {})
    metrics_path = ROOT / "metrics/latest.json"
    metrics = fr.load_json(metrics_path, {})
    lane_metrics = {}

    for lane, arr in grouped.items():
        live_weight = "fast_ready"
        contact_weight = "recovery_candidates"
        lm = {
            "workers_completed": len(arr),
            "workers_failed": sum(s.get("status") != "success" for s in arr),
            "raw_discovered": sum(int(s.get("raw_site_email_rows") or 0) for s in arr),
            "fast_ready": sum(int(s.get("fast_ready") or 0) for s in arr),
            "live_ready": sum(int(s.get("live_ready") or 0) for s in arr),
            "recovery_candidates": sum(int(s.get("recovery_candidates") or 0) for s in arr),
            "recovered_public_emails": sum(int(s.get("recovered_public_emails") or 0) for s in arr),
            "contact_ready": sum(int(s.get("contact_ready") or 0) for s in arr),
            "social_or_contact_without_email": sum(int(s.get("social_or_contact_without_email") or 0) for s in arr),
            "instagram_found": sum(int(s.get("instagram_found") or 0) for s in arr),
            "facebook_found": sum(int(s.get("facebook_found") or 0) for s in arr),
            "live_health": {
                "429_rate": round(weighted(arr, "http_429_rate", live_weight), 5),
                "timeout_rate": round(weighted(arr, "timeout_rate", live_weight), 5),
                "error_rate": round(weighted(arr, "error_rate", live_weight), 5),
            },
            "contact_health": {
                "429_rate": round(weighted(arr, "contact_429_rate", contact_weight), 5),
                "timeout_rate": round(weighted(arr, "contact_timeout_rate", contact_weight), 5),
                "error_rate": round(weighted(arr, "contact_error_rate", contact_weight), 5),
            },
        }
        lane_metrics[lane] = lm
        state = lane_doc.setdefault(lane, {})
        state.update({
            "last_workers_completed": lm["workers_completed"],
            "last_workers_failed": lm["workers_failed"],
            "last_raw_discovered": lm["raw_discovered"],
            "last_fast_ready": lm["fast_ready"],
            "last_live_ready": lm["live_ready"],
            "last_recovery_candidates": lm["recovery_candidates"],
            "last_recovered_public_emails": lm["recovered_public_emails"],
            "last_contact_ready": lm["contact_ready"],
            "last_social_or_contact_without_email": lm["social_or_contact_without_email"],
            "last_live_health": lm["live_health"],
            "last_contact_health": lm["contact_health"],
        })
        if lane == "site_recovery":
            current = int(state.get("recommended_contact_workers") or 48)
            h = lm["contact_health"]
            if h["429_rate"] > 0.02 or h["timeout_rate"] > 0.15 or h["error_rate"] > 0.20:
                current = max(16, current - 8)
            elif lm["recovery_candidates"] >= 50 and h["429_rate"] < 0.005 and h["timeout_rate"] < 0.05 and h["error_rate"] < 0.08:
                current = min(48, current + 8)
            state["recommended_contact_workers"] = current

    metrics["lane_metrics"] = lane_metrics
    fr.write_json(metrics_path, metrics)
    fr.write_json(source_path, source_doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--provider", default="github")
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()
    fr.root_host = registrable_domain
    prior_enrichment = snapshot_monotonic_fields(a.canonical_db)
    fr.aggregate(a)
    restored = restore_monotonic_fields(a.canonical_db, prior_enrichment)
    print(json.dumps({"monotonic_raw_rows_restored": restored}))
    run_multichannel_enrichment(a.canonical_db, a.outdir, a.cycle_id)
    persist_lane_health(a.results_root)


if __name__ == "__main__":
    main()
