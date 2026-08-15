#!/usr/bin/env python3
"""Yield-aware multi-lane planner for the autonomous hospitality world fleet.

Coverage remains independent per lane, but phases are no longer a hard global
barrier. High-priority recovery work can exploit known commercial markets while
a small exploration budget keeps cheap first-pass geographic coverage moving.

One cell per runner is the current production baseline. A multi-cell runner-local
packing canary produced materially worse canonical novelty per runner-minute, so
packing remains disabled until novelty-aware cell metrics justify another canary.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

import fleet_runtime as fr
import hospitality_grid_plan as gp

ROOT = Path(__file__).resolve().parents[1]
LANES_PATH = ROOT / "config/hospitality_source_lanes.json"
ATLAS_PATH = ROOT / "config/hospitality_world_atlas.json"
PHASE_PENALTY = 20_000_000.0
EXPLORE_SHARE = 0.15
EXPLOIT_CELLS_PER_RUNNER = 1
EXPLORE_CELLS_PER_RUNNER = 1


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
            if suffix:
                c["name"] = f"{c['name']}--{suffix}"
                c["region"] = f"{c['region']}::lane={suffix}"
                c["key"] = fr.shard_key(c)
            out.append(c)
    return out


def choose_groups(ranked, capacity: int, force_lane: str):
    """Return runner-local groups of independent geo cells.

    Production currently uses one cell per runner for both exploit and explore.
    The grouping abstraction remains so packing can be canaried again later with
    measured canonical-novelty metrics instead of changing worker contracts.
    """
    capacity = max(0, int(capacity))
    if capacity == 0:
        return []
    if force_lane:
        return [("forced", [dict(x[2], planner_bucket="forced", planner_score=round(float(x[0]), 3))]) for x in ranked[:capacity]]

    explore_slots = 0 if capacity < 4 else max(1, min(capacity - 1, round(capacity * EXPLORE_SHARE)))
    exploit_slots = capacity - explore_slots
    used = set()

    exploit_units = []
    for score, key, cell in ranked:
        if len(exploit_units) >= exploit_slots * EXPLOIT_CELLS_PER_RUNNER:
            break
        exploit_units.append(dict(cell, planner_bucket="exploit", planner_score=round(float(score), 3)))
        used.add(key)

    explore_units = []
    if explore_slots:
        for score, key, cell in ranked:
            if len(explore_units) >= explore_slots * EXPLORE_CELLS_PER_RUNNER:
                break
            if key in used or cell.get("lane") != "fast_email":
                continue
            explore_units.append(dict(cell, planner_bucket="explore", planner_score=round(float(score), 3)))
            used.add(key)

    groups = []
    for i in range(exploit_slots):
        cells = exploit_units[i::exploit_slots]
        if cells:
            groups.append(("exploit", cells))
    for i in range(explore_slots):
        cells = explore_units[i::explore_slots]
        if cells:
            groups.append(("explore", cells))

    if len(groups) < capacity:
        for score, key, cell in ranked:
            if len(groups) >= capacity:
                break
            if key in used:
                continue
            used.add(key)
            groups.append(("exploit-fill", [dict(cell, planner_bucket="exploit-fill", planner_score=round(float(score), 3))]))
    return groups


def encode_batch(cells: list[dict]) -> str:
    raw = json.dumps(cells, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def batch_task(cells: list[dict], bucket: str, index: int) -> dict:
    if len(cells) == 1 and bucket == "forced":
        return dict(cells[0])
    sig = hashlib.sha1("|".join(str(c.get("key") or "") for c in cells).encode()).hexdigest()[:16]
    rep = dict(cells[0])
    lanes = sorted({str(c.get("lane") or "unknown") for c in cells})
    payload = encode_batch(cells)
    # Keep one execution path even for single-cell production groups. The batch
    # wrapper records planner metadata and lets a future packing canary reuse the
    # same worker contract without a workflow fork.
    rep.update({
        "name": f"geo-batch-{bucket}-{index:02d}-{sig}",
        "country": "BATCH",
        "region": f"RunnerQueue::{bucket}::{len(cells)}cells",
        "bbox": f"batch64:{payload}",
        "key": f"batch-{sig}",
        "lane_id": f"runner-queue::{rep.get('lane') or 'geo'}",
        "source_family": "overture-batched",
        "catalog_layer": "runner-queue",
        "tier": "BATCH",
        "planner_bucket": bucket,
        "planner_score": max(float(c.get("planner_score") or 0) for c in cells),
        "batch_size": len(cells),
        "batch_lanes": lanes,
        "batch_cells_b64": payload,
    })
    return rep


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
    groups = choose_groups(ranked, int(a.capacity), a.force_lane) if enabled else []

    selected = []
    selected_work_units = 0
    work_unit_lane_counts = {}
    for i, (bucket, cells) in enumerate(groups):
        configured = []
        for cell in cells:
            c = dict(cell)
            c["local_workers"] = local_workers
            if c.get("lane") == "site_recovery":
                state = lane_state.get("site_recovery") or {}
                c["contact_workers"] = int(state.get("recommended_contact_workers") or c.get("contact_workers") or 48)
            c["verify_engine"] = "thread"
            c["per_host"] = 4
            configured.append(c)
            lane = str(c.get("lane") or "unknown")
            work_unit_lane_counts[lane] = work_unit_lane_counts.get(lane, 0) + 1
        task = batch_task(configured, bucket, i)
        task["slot"] = i
        task["local_workers"] = local_workers
        selected.append(task)
        selected_work_units += len(configured)

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
        "planner_policy": {
            "exploit_share": 1.0 - EXPLORE_SHARE,
            "explore_share": EXPLORE_SHARE,
            "phase_penalty": PHASE_PENALTY,
            "exploit_cells_per_runner": EXPLOIT_CELLS_PER_RUNNER,
            "explore_cells_per_runner": EXPLORE_CELLS_PER_RUNNER,
        },
        "selected_lane_counts": {},
        "selected_bucket_counts": {},
        "selected_work_units": selected_work_units,
        "selected_work_unit_lane_counts": work_unit_lane_counts,
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
