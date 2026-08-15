#!/usr/bin/env python3
"""Reserve account-wide capacity for the one-time GWS legacy 5,047 challenge.

The challenge reuses the global capacity broker, but it must not be starved by
idle *elastic* reservations belonging to sibling workloads. During this bounded
legacy benchmark sibling minimum guarantees are preserved while their elastic
weights are treated as borrowable. Existing live jobs are never preempted.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import global_capacity_broker as broker

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = 5047


def legacy_remaining() -> int:
    health_path = ROOT / "metrics/gws_legacy_deep_health.json"
    latest_path = ROOT / "metrics/gws_legacy_deep_latest.json"
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
    except Exception:
        health = {}
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        latest = {}
    if health.get("state") == "SUCCESS" and int(latest.get("attempted_unique") or 0) == EXPECTED:
        return 0
    attempted = int(latest.get("attempted_unique") or 0)
    return max(1, EXPECTED - min(EXPECTED, attempted))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requested", type=int, default=10)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--owner", default="walidgdg1-ai")
    ap.add_argument("--repo", default="walidgdg1-ai/evergreenleadminer")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    normal_local_demand = broker.local_demand
    normal_load_json = broker.fr.load_json
    remaining = legacy_remaining()

    def challenge_demand():
        d = normal_local_demand()
        # This workflow represents only the legacy challenge. Once it reaches an
        # exact durable 5,047 SUCCESS, it must not steal slots merely because the
        # normal GWS discovery planner has unrelated backlog.
        d["gws"] = remaining if remaining > 0 else 0
        return d

    def challenge_load_json(path, default=None):
        cfg = normal_load_json(path, default)
        try:
            p = Path(path)
        except Exception:
            return cfg
        if p.name != "global_fleet.json" or not isinstance(cfg, dict):
            return cfg
        cfg = copy.deepcopy(cfg)
        workloads = cfg.get("workloads") or {}
        # Preserve every sibling minimum guarantee, but do not let an idle
        # sibling's weighted elastic target reserve the pool ahead of this
        # bounded GWS backlog. Live jobs still count as occupied in the broker.
        for name, scfg in workloads.items():
            if name != "gws" and isinstance(scfg, dict):
                scfg["weight"] = 0.0
        return cfg

    broker.local_demand = challenge_demand
    broker.fr.load_json = challenge_load_json
    args = SimpleNamespace(
        workload="gws",
        requested=a.requested,
        run_id=a.run_id,
        owner=a.owner,
        repo=a.repo,
        out=a.out,
        dry_run=a.dry_run,
    )
    broker.reserve(args)

    payload = json.loads(Path(a.out).read_text(encoding="utf-8"))
    payload["legacy_expected"] = EXPECTED
    payload["legacy_remaining"] = remaining
    payload["legacy_capacity_policy"] = "preserve_sibling_floors_borrow_idle_elastic"
    Path(a.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
