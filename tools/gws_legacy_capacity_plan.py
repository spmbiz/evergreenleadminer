#!/usr/bin/env python3
"""Reserve account-wide capacity for the one-time GWS legacy 5,047 challenge.

This deliberately reuses global_capacity_broker instead of creating a second
capacity model. The normal GWS planner may have zero fresh discovery backlog
while the legacy challenge is still incomplete, so this wrapper injects the
legacy verification backlog into the broker's demand snapshot for this run.
"""
from __future__ import annotations

import argparse
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

    def challenge_demand():
        d = normal_local_demand()
        d["gws"] = max(int(d.get("gws") or 0), legacy_remaining())
        return d

    broker.local_demand = challenge_demand
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
    payload["legacy_remaining"] = legacy_remaining()
    Path(a.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
