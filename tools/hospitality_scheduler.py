#!/usr/bin/env python3
"""Tiny durable cloud-worker autoscaler for the 24/7 hospitality fleet.

It intentionally starts below the GitHub ceiling and promotes one canary step only
when the previous complete cycle is healthy. Source/local HTTP concurrency remains
managed separately by fleet_runtime.py.
"""
from __future__ import annotations
import argparse, datetime as dt, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state/hospitality_scheduler.json"
DEFAULT = {
    "schema_version": 1,
    "steps": [4, 8, 12, 16, 20],
    "recommended_cloud_workers": 4,
    "last_cycle": None,
    "last_decision": "initial_canary",
    "updated_at": None,
}

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)

def save(obj):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    obj["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    STATE.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")

def capacity(available: int, force: int | None):
    st = load_json(STATE, DEFAULT)
    steps = sorted({int(x) for x in st.get("steps", DEFAULT["steps"]) if int(x) > 0})
    recommended = int(st.get("recommended_cloud_workers") or steps[0])
    if force is not None and force > 0:
        chosen = min(int(available), int(force), max(steps))
        reason = "forced_test_canary"
    else:
        chosen = min(int(available), recommended, max(steps))
        reason = "durable_recommendation"
    print(json.dumps({"available": int(available), "recommended": recommended, "chosen": max(0, chosen), "reason": reason}, separators=(",", ":")))

def after(metrics_path: str):
    st = load_json(STATE, DEFAULT)
    m = load_json(Path(metrics_path), {})
    steps = sorted({int(x) for x in st.get("steps", DEFAULT["steps"]) if int(x) > 0})
    cur = int(st.get("recommended_cloud_workers") or steps[0])
    if cur not in steps:
        cur = min(steps, key=lambda x: abs(x-cur))
    idx = steps.index(cur)
    health = m.get("health") or {}
    errors = int(m.get("errors") or 0)
    completed = int(m.get("workers_completed") or 0)
    r429 = float(health.get("429_rate") or 0)
    tout = float(health.get("timeout_rate") or 0)
    err_rate = float(health.get("error_rate") or 0)
    useful = int(m.get("live_ready_before_canonical_dedupe") or 0)

    unhealthy = errors > 0 or r429 > 0.02 or tout > 0.15 or err_rate > 0.20
    healthy = errors == 0 and r429 < 0.005 and tout < 0.05 and err_rate < 0.08 and completed > 0

    if unhealthy and idx > 0:
        nxt = steps[idx-1]
        decision = "demote_unhealthy"
    elif healthy and useful > 0 and idx < len(steps)-1:
        nxt = steps[idx+1]
        decision = "promote_healthy_canary"
    else:
        nxt = cur
        decision = "hold"

    st.update({
        "steps": steps,
        "recommended_cloud_workers": nxt,
        "last_cycle": m.get("cycle_id"),
        "last_decision": decision,
        "last_health": {"429_rate": r429, "timeout_rate": tout, "error_rate": err_rate, "errors": errors},
        "last_workers_completed": completed,
        "last_live_ready": useful,
    })
    save(st)
    print(json.dumps(st, separators=(",", ":")))

def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("capacity")
    p.add_argument("--available", type=int, required=True)
    p.add_argument("--force", type=int)
    p = sp.add_parser("after")
    p.add_argument("--metrics", required=True)
    a = ap.parse_args()
    if a.cmd == "capacity":
        capacity(a.available, a.force)
    else:
        after(a.metrics)

if __name__ == "__main__":
    main()
