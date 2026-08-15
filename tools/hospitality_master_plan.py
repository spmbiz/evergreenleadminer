#!/usr/bin/env python3
"""Unified planner: geographic Overture backlog + bounded external-source exploration.

Overture remains the coverage backbone. Approved external source tasks may reserve
only a tiny configured number of slots when their source release changed or their
revisit is due. Source failures never block geographic planning.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
ATP_CFG = ROOT / "config/atp_hospitality_spiders.json"


def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def atp_current_run(spider: str) -> tuple[str, str]:
    url = f"https://data.alltheplaces.xyz/runs/latest/output/{spider}.geojson"
    headers = {"User-Agent":"AIProdLeadHarvester/1.0 (+public-business-research)"}
    last_error = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                final = response.geturl()
                match = re.search(r"/runs/([^/]+)/output/", final)
                if match:
                    return match.group(1), final
                last_error = RuntimeError(f"could not parse ATP run from {final}")
        except Exception as e:
            last_error = e
    raise RuntimeError(f"ATP release resolve failed for {spider}: {type(last_error).__name__}: {last_error}")


def source_tasks(now, coverage):
    cfg = fr.load_json(ATP_CFG, {})
    if not (cfg.get("policy") or {}).get("production_enabled"):
        return [], []
    tasks, errors = [], []
    for item in cfg.get("spiders") or []:
        if not item.get("production_enabled"):
            continue
        spider = str(item.get("spider") or "")
        if not spider:
            continue
        try:
            release, final_url = atp_current_run(spider)
        except Exception as e:
            errors.append({"source":"alltheplaces","spider":spider,"error":f"{type(e).__name__}: {e}"})
            continue
        shard = {"country":"GLOBAL","region":f"ATP::{spider}","bbox":f"atp:{spider}"}
        key = fr.shard_key(shard)
        prior = coverage.get(key) or {}
        last = parse_ts(prior.get("last_success"))
        revisit = float(item.get("revisit_hours") or 168)
        age = 1e9 if not last else max(0.0, (now - last).total_seconds() / 3600.0)
        changed = str(prior.get("release") or "") != release
        retryable = prior.get("status") in ("partial", "failed_retryable")
        if not (changed or not last or age >= revisit or retryable):
            continue
        tasks.append({
            "task_type":"atp_spider",
            "name":f"ATP::{spider}",
            "country":"GLOBAL",
            "region":f"ATP::{spider}",
            "bbox":f"atp:{spider}",
            "release":release,
            "source_final_url":final_url,
            "spider":spider,
            "lane":"atp_directory_contact" if item.get("mode") == "trusted_directory_contact" else "atp_first_party",
            "lane_id":f"atp::{spider}",
            "lane_phase":2,
            "source_family":"alltheplaces",
            "catalog_layer":"external-source",
            "tier":"SOURCE",
            "priority":int(item.get("priority") or 0),
            "key":key,
            "max_rows":0,
            "local_workers":32,
            "contact_workers":0,
            "contact_timeout":8,
            "contact_max_pages":0,
            "contact_max_bytes":0,
            "revisit_hours":revisit,
            "source_age_hours":age,
            "source_changed":changed,
        })
    tasks.sort(key=lambda x:(bool(x.get("source_changed")), int(x.get("priority") or 0), float(x.get("source_age_hours") or 0), x["spider"]), reverse=True)
    return tasks, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=("github", "circleci"), default="github")
    ap.add_argument("--capacity", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ignore-coverage", action="store_true")
    ap.add_argument("--force-lane", default="")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    coverage = (fr.load_json(ROOT / "state/coverage.json", {}).get("shards") or {})
    atp_cfg = fr.load_json(ATP_CFG, {})
    source_cap = int((atp_cfg.get("policy") or {}).get("source_slot_cap_per_cycle") or 0)
    source_due, source_errors = source_tasks(now, coverage)
    source_n = min(max(0, int(a.capacity)), max(0, source_cap), len(source_due))
    selected_sources = source_due[:source_n]
    geo_capacity = max(0, int(a.capacity) - source_n)

    tmp = Path(a.out).with_suffix(".geo.json")
    cmd = [sys.executable, "tools/hospitality_multilane_plan.py", "--provider", a.provider, "--capacity", str(geo_capacity), "--out", str(tmp)]
    if a.ignore_coverage:
        cmd.append("--ignore-coverage")
    if a.force_lane and a.force_lane not in ("atp", "alltheplaces", "atp_directory_contact"):
        cmd += ["--force-lane", a.force_lane]
    if a.force_lane in ("atp", "alltheplaces", "atp_directory_contact"):
        selected_sources = source_due[: min(int(a.capacity), len(source_due))]
        geo_capacity = 0
        cmd = [sys.executable, "tools/hospitality_multilane_plan.py", "--provider", a.provider, "--capacity", "0", "--out", str(tmp)]
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    geo = fr.load_json(tmp, {})
    try:
        tmp.unlink()
    except Exception:
        pass
    geos = geo.get("include") or []
    for item in geos:
        item["task_type"] = "geo"
        item["spider"] = ""

    selected = selected_sources + geos
    for i, item in enumerate(selected):
        item["slot"] = i
    lane_backlog = dict(geo.get("lane_backlog") or {})
    lane_backlog["atp_directory_contact"] = len(source_due)
    selected_lane_counts = {}
    for item in selected:
        lane = str(item.get("lane") or "unknown")
        selected_lane_counts[lane] = selected_lane_counts.get(lane, 0) + 1

    payload = dict(geo)
    payload.update({
        "capacity":int(a.capacity),
        "geo_capacity":geo_capacity,
        "source_slot_cap":source_cap,
        "external_source_backlog":len(source_due),
        "source_errors":source_errors,
        "useful_backlog":int(geo.get("useful_backlog") or 0) + len(source_due),
        "lane_backlog":lane_backlog,
        "selected_lane_counts":selected_lane_counts,
        "include":selected,
    })
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in payload.items() if k != "include"}, indent=2))


if __name__ == "__main__":
    main()
