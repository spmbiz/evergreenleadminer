#!/usr/bin/env python3
"""Tiny durable cloud-worker autoscaler for the 24/7 hospitality fleet.

Cloud concurrency measures shard/runner health and source throttling signals. Generic
per-site connection/5xx errors are useful telemetry but must not globally suppress
parallel geographic shards; local HTTP concurrency handles that pressure separately.
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
    failed = int(m.get("workers_failed") or m.get("errors") or 0)
    completed = int(m.get("workers_completed") or 0)
    r429 = float(health.get("429_rate") or 0)
    tout = float(health.get("timeout_rate") or 0)
    site_err = float(health.get("error_rate") or 0)
    useful = int(m.get("live_ready_before_canonical_dedupe") or 0)
    yield_per_worker = useful / completed if completed else 0.0

    # Cloud jobs cover disjoint geography. Demote only when runners actually fail or
    # the source-level throttle signals are materially bad. Per-site DNS/SSL/5xx
    # noise is handled by the local HTTP autoscaler, not by shrinking the cloud fleet.
    unhealthy = failed > 0 or r429 > 0.03 or tout > 0.12
    healthy = failed == 0 and completed > 0 and useful > 0 and r429 < 0.02 and tout < 0.08

    if unhealthy and idx > 0:
        nxt = steps[idx-1]
        decision = "demote_cloud_source_pressure"
    elif healthy and idx < len(steps)-1:
        nxt = steps[idx+1]
        decision = "promote_disjoint_shard_canary"
    else:
        nxt = cur
        decision = "hold"

    st.update({
        "steps": steps,
        "recommended_cloud_workers": nxt,
        "last_cycle": m.get("cycle_id"),
        "last_decision": decision,
        "last_health": {
            "429_rate": r429,
            "timeout_rate": tout,
            "site_error_rate": site_err,
            "workers_failed": failed,
        },
        "last_workers_completed": completed,
        "last_live_ready": useful,
        "last_live_ready_per_worker": round(yield_per_worker, 3),
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
