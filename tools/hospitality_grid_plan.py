#!/usr/bin/env python3
"""High-concurrency hospitality world planner.

Two durable coverage layers are combined:
1. legacy hand-picked premium parent shards, subdivided deterministically;
2. a generated World Atlas grid covering global land-market macro masks.

The atlas exists to keep the autonomous fleet supplied with useful independent
work for hours/days without GPT creating new shards. Coverage is tracked per
cell. Premium legacy work stays highest priority; unseen world cells are then
consumed by geographic priority and eventually revisited on tier-specific
cadences.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
ATLAS_PATH = ROOT / 'config/hospitality_world_atlas.json'


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


def parse_bbox(value: str):
    return tuple(float(x) for x in value.split(','))


def point_in_bbox(x: float, y: float, bbox: str) -> bool:
    x1, y1, x2, y2 = parse_bbox(bbox)
    return x1 <= x <= x2 and y1 <= y <= y2


def legacy_catalog(atlas_cfg: dict):
    cells = []
    rank_cfg = atlas_cfg.get('ranking') or {}
    legacy_priority = int(rank_cfg.get('legacy_premium_priority') or 100)
    legacy_revisit = float(rank_cfg.get('legacy_revisit_hours') or 168)
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
                c['priority'] = int(parent.get('priority') or legacy_priority)
                c['tier'] = str(parent.get('tier') or 'P0')
                c['market_class'] = str(parent.get('market_class') or 'legacy-premium')
                c['revisit_hours'] = float(parent.get('revisit_hours') or legacy_revisit)
                c['catalog_layer'] = 'legacy-premium'
                c['key'] = fr.shard_key(c)
                cells.append(c)
    return cells


def atlas_catalog(atlas_cfg: dict):
    if not atlas_cfg.get('enabled', True):
        return []
    step = float(atlas_cfg.get('cell_degrees') or 5.0)
    min_lat = float(atlas_cfg.get('min_lat') or -60.0)
    max_lat = float(atlas_cfg.get('max_lat') or 75.0)
    max_rows = int(atlas_cfg.get('max_rows_per_cell') or 125000)
    default_priority = int(atlas_cfg.get('default_priority') or 40)
    default_tier = str(atlas_cfg.get('default_tier') or 'P3')
    default_revisit = float(atlas_cfg.get('default_revisit_hours') or 720)
    masks = atlas_cfg.get('macro_masks') or []
    overlays = atlas_cfg.get('priority_overlays') or []
    cells = []

    nlon = int(math.ceil(360.0 / step))
    nlat = int(math.ceil((max_lat - min_lat) / step))
    for iy in range(nlat):
        y1 = min_lat + iy * step
        y2 = min(max_lat, y1 + step)
        cy = (y1 + y2) / 2.0
        for ix in range(nlon):
            x1 = -180.0 + ix * step
            x2 = min(180.0, x1 + step)
            cx = (x1 + x2) / 2.0
            matched_masks = [m for m in masks if point_in_bbox(cx, cy, str(m.get('bbox') or '0,0,0,0'))]
            if not matched_masks:
                continue
            matched_overlays = [o for o in overlays if point_in_bbox(cx, cy, str(o.get('bbox') or '0,0,0,0'))]
            if matched_overlays:
                best = max(matched_overlays, key=lambda o: int(o.get('priority') or default_priority))
                priority = int(best.get('priority') or default_priority)
                tier = str(best.get('tier') or default_tier)
                revisit = float(best.get('revisit_hours') or default_revisit)
                market_class = str(best.get('name') or 'atlas-priority')
            else:
                priority = default_priority
                tier = default_tier
                revisit = default_revisit
                market_class = str(matched_masks[0].get('name') or 'atlas-global')
            bbox = f"{x1:.6f},{y1:.6f},{x2:.6f},{y2:.6f}"
            c = {
                'name': f"atlas-{iy:02d}-{ix:02d}",
                'country': 'AUTO',
                'region': f"World-Atlas::{market_class}::{iy:02d}-{ix:02d}",
                'bbox': bbox,
                'release': '2026-06-17.0',
                'max_rows': max_rows,
                'priority': priority,
                'tier': tier,
                'market_class': market_class,
                'revisit_hours': revisit,
                'catalog_layer': 'world-atlas',
                'grid_nx': 1,
                'grid_ny': 1,
            }
            c['key'] = fr.shard_key(c)
            cells.append(c)
    return cells


def expanded_catalog():
    atlas_cfg = fr.load_json(ATLAS_PATH, {})
    cells = legacy_catalog(atlas_cfg) + atlas_catalog(atlas_cfg)
    # Stable key dedupe. Exact world atlas cells never collide with legacy keys,
    # but this protects future config evolution.
    return list({c['key']: c for c in cells}.values())


def parse_ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except Exception:
        return None


def rank_cell(s: dict, coverage_row: dict, now: dt.datetime, atlas_cfg: dict, ignore_coverage: bool):
    rank_cfg = atlas_cfg.get('ranking') or {}
    unseen_bonus = float(rank_cfg.get('unseen_bonus') or 1_000_000_000)
    priority_mult = float(rank_cfg.get('priority_multiplier') or 1_000_000)
    overdue_mult = float(rank_cfg.get('overdue_multiplier') or 10_000)
    yield_cap = float(rank_cfg.get('yield_bonus_cap') or 50_000)
    failure_penalty = float(rank_cfg.get('failure_penalty') or 100_000)

    last = parse_ts(coverage_row.get('last_success'))
    changed = coverage_row.get('release') != s.get('release')
    age = 1e9 if changed or not last else max(0.0, (now - last).total_seconds() / 3600)
    revisit = max(1.0, float(s.get('revisit_hours') or 168))
    unseen = changed or not last
    retryable = coverage_row.get('status') in ('partial', 'failed_retryable')
    useful = ignore_coverage or unseen or retryable or age >= revisit
    if not useful:
        return None

    priority = int(s.get('priority') or 0)
    overdue_ratio = 1000.0 if unseen else min(1000.0, age / revisit)
    elapsed = float(coverage_row.get('last_elapsed_seconds') or 0)
    ready = float(coverage_row.get('last_live_ready') or 0)
    yield_bonus = min(yield_cap, (ready / max(1.0, elapsed)) * 10_000.0)
    failures = int(coverage_row.get('consecutive_failures') or 0)
    score = (
        (unseen_bonus if unseen else 0.0)
        + priority * priority_mult
        + overdue_ratio * overdue_mult
        + yield_bonus
        - failures * failure_penalty
    )
    return score


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
    atlas_cfg = fr.load_json(ATLAS_PATH, {})
    local_workers = int(source.get('recommended_local_http_workers') or 64)
    enabled = bool(desired.get('enabled')) and bool(desired.get('continuous', True)) and bool(pcfg.get('enabled'))
    now = dt.datetime.now(dt.timezone.utc)
    cycle = now.strftime('%Y%m%dT%H%M%SZ') + '-' + a.provider + '-world'

    catalog = expanded_catalog()
    ranked = []
    tier_backlog = {}
    layer_backlog = {}
    for s in catalog:
        c = coverage.get(s['key']) or {}
        score = rank_cell(s, c, now, atlas_cfg, a.ignore_coverage)
        if score is None:
            continue
        ranked.append((score, s['key'], s))
        tier = str(s.get('tier') or 'unknown')
        layer = str(s.get('catalog_layer') or 'unknown')
        tier_backlog[tier] = tier_backlog.get(tier, 0) + 1
        layer_backlog[layer] = layer_backlog.get(layer, 0) + 1

    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [dict(x[2]) for x in ranked[:max(0, int(a.capacity))]] if enabled else []
    for i, s in enumerate(selected):
        s['slot'] = i
        s['local_workers'] = local_workers

    legacy_count = sum(1 for s in catalog if s.get('catalog_layer') == 'legacy-premium')
    atlas_count = sum(1 for s in catalog if s.get('catalog_layer') == 'world-atlas')
    payload = {
        'enabled': enabled,
        'cycle_id': cycle,
        'provider': a.provider,
        'capacity': int(a.capacity),
        'local_workers': local_workers,
        'parent_catalog_size': len(fr.catalog()),
        'legacy_cell_count': legacy_count,
        'world_atlas_cell_count': atlas_count,
        'catalog_size': len(catalog),
        'useful_backlog': len(ranked),
        'tier_backlog': tier_backlog,
        'layer_backlog': layer_backlog,
        'include': selected,
    }
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in payload.items() if k != 'include'}, indent=2))


if __name__ == '__main__':
    main()
