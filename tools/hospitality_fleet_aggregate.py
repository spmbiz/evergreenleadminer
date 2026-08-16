#!/usr/bin/env python3
"""Fast critical-path hospitality canonical aggregator.

The canonical writer must only merge/dedupe/persist harvest results and update
small scheduler/lane state. Slow first-party multichannel crawling is handled by
the asynchronous hospitality-multichannel-postprocess workflow and later merged
back monotonically under the same canonical-writer lock.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SAFETY_FLOOR = int(os.environ.get("HOSPITALITY_CANONICAL_SAFETY_FLOOR", "10000"))
MULTI_SUFFIXES = {
    "co.uk","org.uk","me.uk","ltd.uk","plc.uk","net.uk",
    "com.au","net.au","org.au","id.au","asn.au",
    "co.nz","net.nz","org.nz",
    "com.br","net.br","org.br","com.mx","com.ar","com.co","com.pe","com.ec","com.uy","com.py","com.ve",
    "co.za","org.za","net.za","com.sg","com.hk","com.tr","com.gr","com.cy","com.mt",
    "co.cr","com.pa","com.do","com.gt","com.hn","com.sv","com.ni",
    "co.il","com.my","co.th","com.ph","com.tw","com.cn","com.jp","co.jp","ne.jp",
}
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


def canonical_health(canonical_db: str) -> tuple[int, str]:
    db = Path(canonical_db)
    if not db.exists() or db.stat().st_size == 0:
        return 0, "missing"
    try:
        con = sqlite3.connect(db)
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
        if not exists:
            con.close()
            return 0, "missing_leads_table"
        count = int(con.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        con.close()
        return count, integrity
    except Exception as exc:
        return 0, f"sqlite_error:{type(exc).__name__}"


def canonical_count(canonical_db: str) -> int:
    return canonical_health(canonical_db)[0]


def restore_lkg_if_needed(canonical_db: str) -> tuple[int, str, bool]:
    count, integrity = canonical_health(canonical_db)
    if count >= CANONICAL_SAFETY_FLOOR and integrity == "ok":
        return count, integrity, False

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        return count, integrity, False

    with tempfile.TemporaryDirectory(prefix="hospitality-lkg-") as td:
        cmd = [
            "gh", "release", "download", "harvest-state-backup",
            "--repo", repo,
            "--pattern", "hospitality-canonical-lkg.sqlite",
            "--dir", td,
            "--clobber",
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            print(json.dumps({
                "canonical_self_heal": "lkg_download_failed",
                "primary_rows": count,
                "primary_integrity": integrity,
                "stderr": (proc.stderr or "")[-500:],
            }))
            return count, integrity, False
        lkg = Path(td) / "hospitality-canonical-lkg.sqlite"
        lkg_count, lkg_integrity = canonical_health(str(lkg))
        if lkg_count < CANONICAL_SAFETY_FLOOR or lkg_integrity != "ok":
            print(json.dumps({
                "canonical_self_heal": "lkg_invalid",
                "primary_rows": count,
                "lkg_rows": lkg_count,
                "lkg_integrity": lkg_integrity,
            }))
            return count, integrity, False
        dest = Path(canonical_db)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lkg, dest)
        healed_count, healed_integrity = canonical_health(canonical_db)
        print(json.dumps({
            "canonical_self_heal": "restored_from_lkg",
            "primary_rows_before": count,
            "rows_after": healed_count,
            "integrity_after": healed_integrity,
        }))
        return healed_count, healed_integrity, True


def require_healthy_canonical(canonical_db: str) -> int:
    count, integrity, healed = restore_lkg_if_needed(canonical_db)
    if count < CANONICAL_SAFETY_FLOOR or integrity != "ok":
        raise RuntimeError(
            f"Refusing Hospitality canonical aggregate: rows={count} integrity={integrity} "
            f"below safety requirements floor={CANONICAL_SAFETY_FLOOR}. LKG healed={healed}."
        )
    return count


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
    metrics["multichannel_enrichment_mode"] = "asynchronous_delta_postprocess"
    fr.write_json(metrics_path, metrics)
    fr.write_json(source_path, source_doc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--provider", default="github")
    ap.add_argument("--canonical-db", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    fr.root_host = registrable_domain
    prior_count = require_healthy_canonical(a.canonical_db)
    prior_enrichment = snapshot_monotonic_fields(a.canonical_db)
    fr.aggregate(a)
    after_count = canonical_count(a.canonical_db)
    after_integrity = canonical_health(a.canonical_db)[1]
    if after_count < prior_count or after_count < CANONICAL_SAFETY_FLOOR or after_integrity != "ok":
        raise RuntimeError(
            f"Refusing Hospitality canonical persistence: before={prior_count} after={after_count} "
            f"integrity={after_integrity} floor={CANONICAL_SAFETY_FLOOR}. Canonical aggregation must be monotonic."
        )
    restored = restore_monotonic_fields(a.canonical_db, prior_enrichment)
    final_count, final_integrity = canonical_health(a.canonical_db)
    print(json.dumps({
        "canonical_rows_before": prior_count,
        "canonical_rows_after": final_count,
        "canonical_integrity": final_integrity,
        "monotonic_raw_rows_restored": restored,
        "multichannel_inline": False,
    }))
    persist_lane_health(a.results_root)


if __name__ == "__main__":
    main()
