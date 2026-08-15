#!/usr/bin/env python3
"""Yield-aware multi-lane planner for the autonomous hospitality world fleet.

Coverage remains independent per lane, but phases are no longer a hard global
barrier. High-priority recovery work can exploit known commercial markets while
a small exploration budget keeps cheap first-pass geographic coverage moving.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import fleet_runtime as fr
import hospitality_grid_plan as gp

ROOT = Path(__file__).resolve().parents[1]
LANES_PATH = ROOT / "config/hospitality_source_lanes.json"
ATLAS_PATH = ROOT / "config/hospitality_world_atlas.json"
# A phase still carries a cost premium, but it must not force us to exhaust every
# low-priority remote Pass-A cell before touching P0/P1 recovery inventory.
PHASE_PENALTY = 20_000_000.0
EXPLORE_SHARE = 0.15


def lane_catalog():
    cfg = fr.load_json(LANES_PATH, {})
    lanes = [x for x in (cfg.get("lanes") or []) if x.get("enabled") and x.get("planner_mode") == "geographic"]
    base = gp.expanded_catalog()
    out = []
    for lane in lanes:
        lane_id = str(lane.get("id") or "")
        mode = str(lane.get("worker_mode") or "fast_email")
        phase = int(lane.get("phase") or 0)
        max_rows = int(lane.get("max_rows_per_cell") or 125000)
        revisit_mult = float(lane.get("revisit_multiplier") or 1.0)
        suffix = str(lane.get("coverage_suffix") or "").strip()
        for source_cell in base:
            c = dict(source_cell)
            c["lane_id"] = lane_id
            c["lane"] = mode
            c["lane_phase"] = phase
            c["source_family"] = str(lane.get("source") or "")
            c["max_rows"] = min(int(c.get("max_rows") or max_rows), max_rows)
            c["revisit_hours"] = max(1.0, float(c.get("revisit_hours") or 168) * revisit_mult)
            if mode == "site_recovery":
                crawl = lane.get("contact_crawl") or {}
                c["contact_workers"] = int(crawl.get("workers") or 48)
                c["contact_timeout"] = float(crawl.get("timeout_seconds") or 7)
                c["contact_max_pages"] = int(crawl.get("max_pages_per_domain") or 3)
                c["contact_max_bytes"] = int(crawl.get("max_bytes_per_page") or 900000)
            else:
                c["contact_workers"] = 0
                c["contact_timeout"] = 7
                c["contact_max_pages"] = 3
                c["contact_max_bytes"] = 900000

            # Preserve deployed Pass-A keys. Recovery gets its own deterministic
            # coverage key, allowing both lanes to progress independently.
            if suffix:
                c["name"] = f"{c['name']}--{suffix}"
                c["region"] = f"{c['region']}::lane={suffix}"
                c["key"] = fr.shard_key(c)
            out.append(c)
    return out


def choose_exploit_explore(ranked, capacity: int, force_lane: str):
    """Allocate ~85% to highest expected yield and ~15% to cheap exploration.

    Exploration uses unseen/retryable fast-email cells. This preserves discovery
    breadth without letting low-value P3 geography monopolize all runners while
    high-priority site-recovery inventory exists.
    """
    capacity = max(0, int(capacity))
    if capacity == 0:
        return []
    if force_lane:
        return [dict(x[2]) for x in ranked[:capacity]]

    explore_n = 0 if capacity < 4 else max(1, min(capacity - 1, round(capacity * EXPLORE_SHARE)))
    exploit_n = capacity - explore_n
    chosen = []
    chosen_keys = set()

    for score, key, cell in ranked:
        if len(chosen) >= exploit_n:
            break
        chosen.append(dict(cell, planner_bucket="exploit", planner_score=round(float(score), 3)))
        chosen_keys.add(key)

    if explore_n:
        for score, key, cell in ranked:
            if len(chosen) >= capacity:
                break
            if key in chosen_keys or cell.get("lane") != "fast_email":
                continue
            chosen.append(dict(cell, planner_bucket="explore", planner_score=round(float(score), 3)))
            chosen_keys.add(key)

    # If the exploration pool is exhausted, never leave usable capacity idle.
    if len(chosen) < capacity:
        for score, key, cell in ranked:
            if len(chosen) >= capacity:
                break
            if key in chosen_keys:
                continue
            chosen.append(dict(cell, planner_bucket="exploit-fill", planner_score=round(float(score), 3)))
            chosen_keys.add(key)
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), default="github")
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ignore-coverage", action="store_true")
    ap.add_argument("--force-lane", default="")
    a = ap.parse_args()

    fr.init_state()
    desired = fr.load_json(ROOT / "control/desired_state.json", {})
    providers = (fr.load_json(ROOT / "config/providers.json", {}).get("providers") or {})
    pcfg = providers.get(a.provider) or {}
    coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
    source_doc = fr.load_json(ROOT / "state/source_state.json", {})
    source = source_doc.get("overture_hospitality_v6") or {}
    lane_state = source_doc.get("hospitality_lanes") or {}
    atlas_cfg = fr.load_json(ATLAS_PATH, {})
    local_workers = int(source.get("recommended_local_http_workers") or 64)
    enabled = bool(desired.get("enabled")) and bool(desired.get("continuous", True)) and bool(pcfg.get("enabled"))
    now = dt.datetime.now(dt.timezone.utc)
    cycle = now.strftime("%Y%m%dT%H%M%SZ") + "-" + a.provider + "-multilane"

    catalog = lane_catalog()
    if a.force_lane:
        catalog = [s for s in catalog if s.get("lane") == a.force_lane or s.get("lane_id") == a.force_lane]

    ranked = []
    tier_backlog = {}
    layer_backlog = {}
    lane_backlog = {}
    for s in catalog:
        c = coverage.get(s["key"]) or {}
        base_score = gp.rank_cell(s, c, now, atlas_cfg, a.ignore_coverage)
        if base_score is None:
            continue
        phase = int(s.get("lane_phase") or 0)
        score = base_score - phase * PHASE_PENALTY
        ranked.append((score, s["key"], s))
        tier = str(s.get("tier") or "unknown")
        layer = str(s.get("catalog_layer") or "unknown")
        lane = str(s.get("lane") or "unknown")
        tier_backlog[tier] = tier_backlog.get(tier, 0) + 1
        layer_backlog[layer] = layer_backlog.get(layer, 0) + 1
        lane_backlog[lane] = lane_backlog.get(lane, 0) + 1

    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = choose_exploit_explore(ranked, int(a.capacity), a.force_lane) if enabled else []
    for i, s in enumerate(selected):
        s["slot"] = i
        s["local_workers"] = local_workers
        if s.get("lane") == "site_recovery":
            state = lane_state.get("site_recovery") or {}
            s["contact_workers"] = int(state.get("recommended_contact_workers") or s.get("contact_workers") or 48)

    payload = {
        "enabled": enabled,
        "cycle_id": cycle,
        "provider": a.provider,
        "capacity": int(a.capacity),
        "local_workers": local_workers,
        "catalog_size": len(catalog),
        "useful_backlog": len(ranked),
        "tier_backlog": tier_backlog,
        "layer_backlog": layer_backlog,
        "lane_backlog": lane_backlog,
        "planner_policy": {"exploit_share": 1.0 - EXPLORE_SHARE, "explore_share": EXPLORE_SHARE, "phase_penalty": PHASE_PENALTY},
        "selected_lane_counts": {},
        "selected_bucket_counts": {},
        "include": selected,
    }
    for s in selected:
        lane = str(s.get("lane") or "unknown")
        bucket = str(s.get("planner_bucket") or "unknown")
        payload["selected_lane_counts"][lane] = payload["selected_lane_counts"].get(lane, 0) + 1
        payload["selected_bucket_counts"][bucket] = payload["selected_bucket_counts"].get(bucket, 0) + 1

    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in payload.items() if k != "include"}, indent=2))


if __name__ == "__main__":
    main()
