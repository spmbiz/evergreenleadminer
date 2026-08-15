#!/usr/bin/env python3
"""Semantically merge generated hospitality small-state onto latest main.

Long-running aggregates may finish after other controllers have updated the same
JSON files. Git text rebases are the wrong conflict primitive for structured
state. This tool unions keyed state and uses timestamps/cycle IDs to preserve the
newest observation without losing the just-completed cycle.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

FILES = (
    "state/coverage.json",
    "state/checkpoints.json",
    "state/source_state.json",
    "state/hospitality_provider_capacity.json",
    "state/hospitality_scheduler.json",
    "metrics/latest.json",
    "metrics/history.jsonl",
    "gpt/latest_summary.json",
    "gpt/pending_batches.json",
)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ts(value):
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        x = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=dt.timezone.utc)
        return x
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def newest(a: dict, b: dict, fields=("finished_at", "updated_at", "last_updated", "last_attempt", "created_at")):
    def score(x):
        return max((ts(x.get(k)) for k in fields), default=ts(None))
    return b if score(b) >= score(a) else a


def merge_coverage(base, gen):
    out = dict(base or {"schema_version": 1, "shards": {}})
    shards = dict(out.get("shards") or {})
    for key, value in (gen.get("shards") or {}).items():
        prior = shards.get(key)
        shards[key] = value if not prior else newest(prior, value, ("last_attempt", "last_success"))
    out["shards"] = shards
    out["schema_version"] = max(int(out.get("schema_version") or 1), int(gen.get("schema_version") or 1))
    return out


def merge_checkpoints(base, gen):
    out = dict(base or {"schema_version": 1, "cycles": {}})
    cycles = dict(out.get("cycles") or {})
    cycles.update(gen.get("cycles") or {})
    if len(cycles) > 140:
        ordered = sorted(cycles.items(), key=lambda kv: ts((kv[1] or {}).get("finished_at")))
        cycles = dict(ordered[-140:])
    out["cycles"] = cycles
    if cycles:
        out["last_cycle"] = max(cycles, key=lambda k: ts((cycles[k] or {}).get("finished_at")))
    return out


def merge_source(base, gen):
    out = dict(base or {"schema_version": 1})
    for key, value in gen.items():
        if key == "schema_version":
            continue
        if not isinstance(value, dict):
            out[key] = value
            continue
        prior = out.get(key)
        if not isinstance(prior, dict):
            out[key] = value
            continue
        if key == "hospitality_lanes":
            lanes = dict(prior)
            for lane, lane_value in value.items():
                lp = lanes.get(lane)
                lanes[lane] = lane_value if not isinstance(lp, dict) else newest(lp, lane_value)
            out[key] = lanes
        else:
            out[key] = newest(prior, value)
    return out


def merge_pending(base, gen):
    out = dict(base or {"schema_version": 1, "batches": []})
    items = {}
    for doc in (base, gen):
        for row in (doc or {}).get("batches") or []:
            key = str(row.get("batch") or row.get("cycle_id") or json.dumps(row, sort_keys=True))
            prior = items.get(key)
            items[key] = row if prior is None else newest(prior, row)
    ordered = sorted(items.values(), key=lambda x: ts(x.get("created_at")))
    out["batches"] = ordered[-100:]
    return out


def merge_history(base_path: Path, gen_path: Path):
    rows = {}
    order = []
    for p in (base_path, gen_path):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                key = str(obj.get("cycle_id") or line)
            except Exception:
                key = line
            if key not in rows:
                order.append(key)
            rows[key] = line
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text("".join(rows[k] + "\n" for k in order), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generated", required=True)
    a = ap.parse_args()
    generated = Path(a.generated)

    for rel in FILES:
        dst = Path(rel)
        src = generated / rel
        if not src.exists():
            continue
        if rel == "metrics/history.jsonl":
            merge_history(dst, src)
            continue
        base = load_json(dst, {})
        gen = load_json(src, {})
        if rel == "state/coverage.json":
            merged = merge_coverage(base, gen)
        elif rel == "state/checkpoints.json":
            merged = merge_checkpoints(base, gen)
        elif rel == "state/source_state.json":
            merged = merge_source(base, gen)
        elif rel == "gpt/pending_batches.json":
            merged = merge_pending(base, gen)
        elif rel in ("metrics/latest.json", "gpt/latest_summary.json", "state/hospitality_scheduler.json", "state/hospitality_provider_capacity.json"):
            merged = newest(base, gen)
        else:
            merged = gen
        write_json(dst, merged)


if __name__ == "__main__":
    main()
