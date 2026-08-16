#!/usr/bin/env python3
"""Bounded DATAtourisme planner overlay.

This module is intentionally inert until imported by the main planner wrapper AND
config production_enabled=true. It can inject at most one official DATAtourisme
France task and may replace only legacy geo/site_recovery work. It never grows
broker capacity and never displaces FreshSearch, Wikidata, ATP, or OSM.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/hospitality_datatourisme_sources.json"


def _parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def due_tasks(now: dt.datetime, coverage: dict, excluded: set[str]) -> list[dict]:
    cfg = fr.load_json(CFG, {})
    policy = cfg.get("policy") or {}
    if not cfg.get("enabled") or not cfg.get("production_enabled"):
        return []
    revisit = float(policy.get("revisit_hours") or 24)
    shard = {
        "country": "France",
        "region": "DATATOURISME::FR",
        "bbox": "datatourisme:fr",
    }
    key = fr.shard_key(shard)
    if key in excluded:
        return []
    prior = coverage.get(key) or {}
    last = _parse_ts(prior.get("last_success"))
    age = 1e9 if not last else max(0.0, (now - last).total_seconds() / 3600.0)
    retryable = prior.get("status") in ("partial", "failed_retryable")
    if last and age < revisit and not retryable:
        return []
    return [{
        "task_type": "datatourisme_hospitality",
        "name": "DATATOURISME::FR",
        "country": "France",
        "region": "DATATOURISME::FR",
        "bbox": "datatourisme:fr",
        "release": now.strftime("%Y-%m-%d"),
        "source_final_url": "https://www.data.gouv.fr/",
        "spider": "",
        "extract_id": "",
        "search_cursor": 0,
        "search_queries": 0,
        "lane": "datatourisme_hospitality",
        "lane_id": "datatourisme::fr",
        "lane_phase": 1,
        "source_family": "datatourisme_official_hospitality",
        "catalog_layer": "external-source",
        "tier": "SOURCE",
        "priority": 1190,
        "key": key,
        "max_rows": int(policy.get("max_candidates") or 1500),
        "local_workers": int(policy.get("local_workers") or 32),
        "contact_workers": int(policy.get("contact_workers") or 24),
        "contact_timeout": 8,
        "contact_max_pages": 3,
        "contact_max_bytes": 700000,
        "revisit_hours": revisit,
        "source_age_hours": age,
        "source_changed": not last,
    }]


def inject(plan: dict, excluded: set[str]) -> dict:
    cfg = fr.load_json(CFG, {})
    policy = cfg.get("policy") or {}
    cap = max(0, int(policy.get("source_slot_cap_per_cycle") or 0))
    capacity = max(0, int(plan.get("capacity") or 0))
    if cap <= 0 or capacity <= 0 or not cfg.get("production_enabled"):
        return plan
    coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
    due = due_tasks(dt.datetime.now(dt.timezone.utc), coverage, excluded)
    plan.setdefault("lane_backlog", {})["datatourisme_hospitality"] = len(due)
    if not due:
        return plan

    selected = list(plan.get("include") or [])[:capacity]
    if any(str(x.get("task_type") or "") == "datatourisme_hospitality" for x in selected):
        return plan

    # Safety invariant: DATAtourisme may replace ONLY low-yield legacy work.
    replace_idx = next(
        (i for i in range(len(selected) - 1, -1, -1)
         if str(selected[i].get("task_type") or "") in {"geo"}
         or str(selected[i].get("lane") or "") == "site_recovery"),
        None,
    )
    if replace_idx is None:
        plan["datatourisme_inserted"] = 0
        return plan

    selected[replace_idx] = due[0]
    for i, item in enumerate(selected):
        item["slot"] = i
    counts = {}
    for item in selected:
        lane = str(item.get("lane") or "unknown")
        counts[lane] = counts.get(lane, 0) + 1
    plan["include"] = selected
    plan["selected_lane_counts"] = counts
    plan["datatourisme_backlog"] = len(due)
    plan["datatourisme_inserted"] = 1
    return plan
