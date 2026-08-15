#!/usr/bin/env python3
"""High-concurrency hospitality planner built on the existing durable fleet state.

Large source bboxes are deterministically subdivided into smaller cells. This
increases independent useful work, reduces per-run spatial scan size, and lets
GitHub/CircleCI consume dozens of workers without inventing new source data.
Coverage is tracked per cell because each cell receives its own region+bbox key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]


def split_axis(lo: float, hi: float, n: int):
    step = (hi - lo) / n
    return [(lo + i * step, hi if i == n - 1 else lo + (i + 1) * step) for i in range(n)]


def split_shape(bbox: str):
    x1, y1, x2, y2 = [float(x) for x in bbox.split(',')]
    width = abs(x2 - x1)
    height = abs(y2 - y1)
    nx = 3 if width >= 12 else 2 if width >= 4 else 1
    ny = 3 if height >= 8 else 2 if height >= 3 else 1
    return x1, y1, x2, y2, nx, ny


def expanded_catalog():
    cells = []
    for parent in fr.catalog():
        try:
            x1, y1, x2, y2, nx, ny = split_shape(parent['bbox'])
        except Exception:
            continue
        xs = split_axis(x1, x2, nx)
        ys = split_axis(y1, y2, ny)
        for iy, (sy1, sy2) in enumerate(ys):
            for ix, (sx1, sx2) in enumerate(xs):
                c = dict(parent)
                suffix = f"g{iy+1}{ix+1}of{ny}x{nx}"
                c['name'] = f"{parent['name']}-{suffix}"
                c['region'] = f"{parent['region']}::{suffix}"
                c['bbox'] = f"{sx1:.6f},{sy1:.6f},{sx2:.6f},{sy2:.6f}"
                c['parent_name'] = parent['name']
                c['grid_nx'] = nx
                c['grid_ny'] = ny
                c['max_rows'] = min(int(parent.get('max_rows') or 250000), 125000)
                c['key'] = fr.shard_key(c)
                cells.append(c)
    # De-dupe exact country/region/bbox identities deterministically.
    unique = {c['key']: c for c in cells}
    return list(unique.values())


def parse_ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--provider', choices=('github', 'circleci'), default='github')
    ap.add_argument('--capacity', type=int, required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--ignore-coverage', action='store_true')
    a = ap.parse_args()

    fr.init_state()
    desired = fr.load_json(ROOT / 'control/desired_state.json', {})
    providers = (fr.load_json(ROOT / 'config/providers.json', {}).get('providers') or {})
    pcfg = providers.get(a.provider) or {}
    coverage = (fr.load_json(ROOT / 'state/coverage.json', {}).get('shards') or {})
    source = (fr.load_json(ROOT / 'state/source_state.json', {}).get('overture_hospitality_v6') or {})
    local_workers = int(source.get('recommended_local_http_workers') or 64)
    enabled = bool(desired.get('enabled')) and bool(desired.get('continuous', True)) and bool(pcfg.get('enabled'))
    now = dt.datetime.now(dt.timezone.utc)
    cycle = now.strftime('%Y%m%dT%H%M%SZ') + '-' + a.provider + '-grid'

    ranked = []
    for s in expanded_catalog():
        c = coverage.get(s['key']) or {}
        last = parse_ts(c.get('last_success'))
        changed = c.get('release') != s.get('release')
        age = 1e9 if changed or not last else max(0.0, (now - last).total_seconds() / 3600)
        useful = a.ignore_coverage or changed or not last or age >= 168 or c.get('status') in ('partial', 'failed_retryable')
        if useful:
            # Penalize repeated failures but keep them retryable.
            score = age - int(c.get('consecutive_failures') or 0) * 72
            ranked.append((score, s['key'], s))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [dict(x[2]) for x in ranked[:max(0, int(a.capacity))]] if enabled else []
    for i, s in enumerate(selected):
        s['slot'] = i
        s['local_workers'] = local_workers

    payload = {
        'enabled': enabled,
        'cycle_id': cycle,
        'provider': a.provider,
        'capacity': int(a.capacity),
        'local_workers': local_workers,
        'parent_catalog_size': len(fr.catalog()),
        'catalog_size': len(expanded_catalog()),
        'useful_backlog': len(ranked),
        'include': selected,
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in payload.items() if k != 'include'}, indent=2))


if __name__ == '__main__':
    main()
