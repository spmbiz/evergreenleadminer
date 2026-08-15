#!/usr/bin/env python3
"""Hospitality aggregate wrapper.

Keeps fleet_runtime as the single canonical implementation, fixes registrable-
domain handling for common multi-label public suffixes, then persists lane-
specific health so expensive contact-recovery traffic can autoscale independently
from the normal live verifier.
"""
from __future__ import annotations

import argparse
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
    fr.aggregate(a)
    persist_lane_health(a.results_root)


if __name__ == "__main__":
    main()
