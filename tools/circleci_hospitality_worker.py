#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan-json', required=True)
    ap.add_argument('--task-index', type=int, required=True)
    ap.add_argument('--outdir', required=True)
    a = ap.parse_args()
    plan = json.loads(Path(a.plan_json).read_text(encoding='utf-8'))
    arr = plan.get('include') or []
    if a.task_index < 0 or a.task_index >= len(arr):
        print(json.dumps({'status': 'noop', 'task_index': a.task_index, 'selected_count': len(arr)}))
        return 0
    s = arr[a.task_index]
    cmd = [
        sys.executable, 'tools/hospitality_worker.py',
        '--provider', 'circleci',
        '--cycle-id', plan['cycle_id'],
        '--name', s['name'],
        '--country', s.get('country', ''),
        '--region', s.get('region', ''),
        f"--bbox={s['bbox']}",
        '--release', s.get('release', '2026-06-17.0'),
        '--max-rows', str(s.get('max_rows', 250000)),
        '--local-workers', str(s.get('local_workers', 64)),
        '--outdir', a.outdir,
    ]
    return subprocess.call(cmd)


if __name__ == '__main__':
    raise SystemExit(main())
