#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path


def config(workload: str, selected: int) -> str:
    if selected <= 0:
        return '''version: 2.1\njobs:\n  no_work:\n    docker:\n      - image: cimg/base:stable\n    resource_class: small\n    steps:\n      - run: echo "Fleet disabled or no useful CircleCI backlog."\nworkflows:\n  fleet_no_work:\n    jobs:\n      - no_work\n'''
    if workload == 'gws':
        worker = '''python -c 'import os; print("circleci_cpu_count=", os.cpu_count())'\n            python tools/gws_fleet_worker.py \\
              --plan-json results/fleet_plan/plan.json \\
              --task-index "$CIRCLE_NODE_INDEX" \\
              --hub-snapshot results/fleet_plan/hub_brussels_current.jsonl \\
              --outdir "results/shards/gws-${CIRCLE_WORKFLOW_ID}-${CIRCLE_NODE_INDEX}" \\
              --threads 4\n            python tools/gws_worker_postprocess.py \\
              --shard-dir "results/shards/gws-${CIRCLE_WORKFLOW_ID}-${CIRCLE_NODE_INDEX}"\n            python tools/gws_async_probe.py \\
              --shard-dir "results/shards/gws-${CIRCLE_WORKFLOW_ID}-${CIRCLE_NODE_INDEX}" \\
              --concurrency 64 --per-host 2 --max-domains 6 --timeout 4.5'''
        deps = 'duckdb aiohttp'
    else:
        worker = '''python tools/circleci_hospitality_worker.py \\
              --plan-json results/fleet_plan/plan.json \\
              --task-index "$CIRCLE_NODE_INDEX" \\
              --outdir "results/shards/hospitality-${CIRCLE_WORKFLOW_ID}-${CIRCLE_NODE_INDEX}"'''
        deps = 'duckdb requests'
    return f'''version: 2.1\njobs:\n  harvest:\n    parallelism: {selected}\n    docker:\n      - image: cimg/python:3.11\n    resource_class: medium\n    steps:\n      - checkout\n      - restore_cache:\n          keys:\n            - fleet-plan-v2-<< pipeline.id >>\n      - run:\n          name: Install worker dependencies only\n          command: python -m pip install --disable-pip-version-check {deps}\n      - run:\n          name: Run provider-neutral {workload} shard\n          command: |\n            set -euo pipefail\n            test -f results/fleet_plan/plan.json || {{ echo "Fleet plan cache missing" >&2; exit 41; }}\n            mkdir -p results/shards\n            {worker}\n      - persist_to_workspace:\n          root: results\n          paths:\n            - shards\n\n  inbox:\n    docker:\n      - image: cimg/python:3.11\n    resource_class: small\n    steps:\n      - checkout\n      - restore_cache:\n          keys:\n            - fleet-plan-v2-<< pipeline.id >>\n      - attach_workspace:\n          at: /tmp/fleet-workspace\n      - run:\n          name: Build immutable provider inbox bundle\n          command: |\n            set -euo pipefail\n            test -f results/fleet_plan/plan.json || {{ echo "Fleet plan cache missing" >&2; exit 41; }}\n            mkdir -p results/shards\n            if [ -d /tmp/fleet-workspace/shards ]; then cp -R /tmp/fleet-workspace/shards/. results/shards/; fi\n            tar -czf "circleci-{workload}-inbox-${{CIRCLE_WORKFLOW_ID}}.tar.gz" results\n      - store_artifacts:\n          path: circleci-{workload}-inbox-${{CIRCLE_WORKFLOW_ID}}.tar.gz\n          destination: fleet-inbox\n      - run:\n          name: Persist inbox before ephemeral workers disappear\n          command: |\n            test -n "${{FLEET_GH_TOKEN:-}}" || {{ echo "FLEET_GH_TOKEN missing; durable upload unavailable." >&2; exit 42; }}\n            FLEET_GH_TOKEN="$FLEET_GH_TOKEN" python tools/fleet_runtime.py upload \\
              --repo walidgdg1-ai/evergreenleadminer \\
              --tag harvest-inbox \\
              --file "circleci-{workload}-inbox-${{CIRCLE_WORKFLOW_ID}}.tar.gz"\n\nworkflows:\n  harvest_fleet:\n    jobs:\n      - harvest\n      - inbox:\n          requires:\n            - harvest\n'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workload', choices=('hospitality', 'gws'), required=True)
    ap.add_argument('--selected-count', type=int, required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    Path(a.out).write_text(config(a.workload, a.selected_count), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
