#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def _compound_cfg(config: dict[str, Any]) -> dict[str, Any]:
    raw = dict(config.get('compound_sidecar') or {})
    return {
        'enabled': bool(raw.get('enabled', True)),
        'records_per_harvest_worker': max(1, int(raw.get('records_per_harvest_worker') or 8)),
        'max_accounts_per_cycle': max(1, int(raw.get('max_accounts_per_cycle') or 160)),
        'standalone_overflow_max_workers': max(1, int(raw.get('standalone_overflow_max_workers') or 4)),
        'standalone_overflow_max_accounts': max(1, int(raw.get('standalone_overflow_max_accounts') or 80)),
        'fail_open': bool(raw.get('fail_open', True)),
    }


def _annotate_zero(plan: dict[str, Any]) -> None:
    for task in plan.get('include') or []:
        task['compound_intel_records'] = 0
        task['compound_intel_path'] = ''
        task['compound_intel_shard'] = -1
        task['compound_qwen_revision'] = ''
        task['compound_qwen_file'] = ''
        task['compound_qwen_image'] = ''


def plan_sidecars(args: argparse.Namespace) -> int:
    harvest_path = Path(args.harvest_plan)
    config_path = Path(args.config)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    harvest = _load_json(harvest_path, {}) or {}
    config = _load_json(config_path, {}) or {}
    sidecfg = _compound_cfg(config)
    tasks = list(harvest.get('include') or [])
    _annotate_zero(harvest)

    base_meta = {
        'enabled': False,
        'harvest_workers': len(tasks),
        'accounts_planned': 0,
        'worker_count': 0,
        'records_per_harvest_worker': sidecfg['records_per_harvest_worker'],
        'max_accounts_per_cycle': sidecfg['max_accounts_per_cycle'],
        'reason': '',
    }

    if not sidecfg['enabled']:
        base_meta['reason'] = 'compound_sidecar_disabled'
        harvest['compound_intelligence'] = base_meta
        _write_json(harvest_path, harvest)
        _write_json(outdir / 'plan.json', base_meta | {'include': []})
        return 0
    if not tasks:
        base_meta['reason'] = 'no_harvest_workers'
        harvest['compound_intelligence'] = base_meta
        _write_json(harvest_path, harvest)
        _write_json(outdir / 'plan.json', base_meta | {'include': []})
        return 0
    canonical = Path(args.canonical_db)
    if not canonical.is_file():
        base_meta['reason'] = 'canonical_missing_fail_open'
        harvest['compound_intelligence'] = base_meta
        _write_json(harvest_path, harvest)
        _write_json(outdir / 'plan.json', base_meta | {'include': []})
        return 0

    records_per_worker = sidecfg['records_per_harvest_worker']
    max_accounts = min(sidecfg['max_accounts_per_cycle'], len(tasks) * records_per_worker)
    effective = dict(config)
    effective['max_workers'] = len(tasks)
    effective['shard_size'] = records_per_worker
    effective['max_accounts_per_pass'] = max_accounts
    effective_path = outdir / 'effective_config.json'
    _write_json(effective_path, effective)

    intelligence_db = Path(args.intelligence_db)
    intelligence_db.parent.mkdir(parents=True, exist_ok=True)
    planner = Path(__file__).with_name('hospitality_intelligence_plan.py')
    cmd = [
        sys.executable, str(planner),
        '--canonical-db', str(canonical),
        '--intelligence-db', str(intelligence_db),
        '--config', str(effective_path),
        '--outdir', str(outdir),
        '--max-accounts', str(max_accounts),
        '--max-workers', str(len(tasks)),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        base_meta['reason'] = 'intelligence_planner_failed_fail_open'
        base_meta['planner_error'] = (proc.stderr or proc.stdout or '')[-1200:]
        harvest['compound_intelligence'] = base_meta
        _write_json(harvest_path, harvest)
        _write_json(outdir / 'plan.json', base_meta | {'include': []})
        if not sidecfg['fail_open']:
            raise SystemExit(proc.returncode)
        return 0

    sideplan = _load_json(outdir / 'plan.json', {}) or {}
    side_shards = list(sideplan.get('include') or [])
    qwen = dict(config.get('qwen') or {})
    for idx, shard in enumerate(side_shards):
        if idx >= len(tasks):
            break
        recs = int(shard.get('records') or 0)
        if recs <= 0:
            continue
        task = tasks[idx]
        task['compound_intel_records'] = recs
        task['compound_intel_path'] = f"intelligence_sidecar/{shard.get('path') or ''}"
        task['compound_intel_shard'] = int(shard.get('shard') or idx)
        task['compound_qwen_revision'] = str(qwen.get('model_revision') or '')
        task['compound_qwen_file'] = str(qwen.get('model_file') or '')
        task['compound_qwen_image'] = str(qwen.get('llama_cpp_image') or 'ghcr.io/ggml-org/llama.cpp:server')

    harvest['include'] = tasks
    meta = {
        **base_meta,
        'enabled': bool(side_shards),
        'accounts_planned': int(sideplan.get('accounts_planned') or 0),
        'worker_count': int(sideplan.get('worker_count') or 0),
        'reason': 'compound_sidecars_planned' if side_shards else str(sideplan.get('reason') or 'no_intelligence_backlog'),
    }
    harvest['compound_intelligence'] = meta
    _write_json(harvest_path, harvest)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


def _sidecar_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for summary in root.rglob('compound_intelligence/summary.json'):
        d = summary.parent.resolve()
        key = str(d)
        if key not in seen:
            seen.add(key)
            found.append(d)
    return sorted(found, key=lambda p: str(p))


def _run_embedded_aggregate(results_root: Path, intelligence_db: Path, run_id: str, outdir: Path) -> None:
    import hospitality_intelligence_aggregate as aggregate_module

    aggregate_module.require_complete_github_matrix = lambda: {
        'checked': False,
        'reason': 'compound_same_harvest_matrix_prevalidated',
    }
    old_argv = sys.argv[:]
    try:
        sys.argv = [
            'hospitality_intelligence_aggregate.py',
            '--results-root', str(results_root),
            '--intelligence-db', str(intelligence_db),
            '--run-id', str(run_id),
            '--outdir', str(outdir),
        ]
        aggregate_module.main()
    finally:
        sys.argv = old_argv


def aggregate_sidecars(args: argparse.Namespace) -> int:
    incoming = Path(args.incoming)
    harvest_plan = Path(args.harvest_plan)
    canonical = Path(args.canonical_db)
    intelligence = Path(args.intelligence_db)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    status_path = outdir / 'compound-status.json'

    plan = _load_json(harvest_plan, {}) or {}
    expected_tasks = [t for t in (plan.get('include') or []) if int(t.get('compound_intel_records') or 0) > 0]
    expected = len(expected_tasks)
    dirs = _sidecar_dirs(incoming)
    status: dict[str, Any] = {
        'status': 'SKIPPED',
        'expected_workers': expected,
        'completed_sidecars': len(dirs),
        'run_id': args.run_id,
    }
    if expected == 0:
        status['reason'] = 'no_compound_intelligence_planned'
        _write_json(status_path, status)
        return 0
    if len(dirs) != expected:
        status['reason'] = 'incomplete_compound_sidecars_fail_open'
        _write_json(status_path, status)
        print(json.dumps(status))
        if args.strict:
            raise SystemExit(4)
        return 0
    if not canonical.is_file():
        status['reason'] = 'canonical_missing_fail_open'
        _write_json(status_path, status)
        if args.strict:
            raise SystemExit(5)
        return 0

    staging = outdir / 'staging'
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    for idx, source in enumerate(dirs):
        shutil.copytree(source, staging / f'worker-{idx:02d}')

    working_intel = outdir / 'hospitality-intelligence.working.sqlite'
    working_canonical = outdir / 'hospitality-canonical.working.sqlite'
    if working_intel.exists():
        working_intel.unlink()
    if intelligence.is_file():
        shutil.copy2(intelligence, working_intel)
    shutil.copy2(canonical, working_canonical)

    aggregate_out = outdir / 'aggregate'
    merge_out = outdir / 'merge-summary.json'
    if aggregate_out.exists():
        shutil.rmtree(aggregate_out)
    aggregate_out.mkdir(parents=True, exist_ok=True)

    try:
        _run_embedded_aggregate(staging, working_intel, args.run_id, aggregate_out)
        delta = aggregate_out / 'canonical-intelligence-delta.jsonl.gz'
        if not delta.is_file():
            raise RuntimeError('compound aggregate did not produce canonical-intelligence-delta.jsonl.gz')
        apply_script = Path(__file__).with_name('apply_hospitality_intelligence_delta.py')
        proc = subprocess.run([
            sys.executable, str(apply_script),
            '--canonical-db', str(working_canonical),
            '--delta', str(delta),
            '--out-summary', str(merge_out),
        ], text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or 'compound canonical fill-only merge failed')[-1600:])

        intelligence.parent.mkdir(parents=True, exist_ok=True)
        working_intel.replace(intelligence)
        working_canonical.replace(canonical)
        aggregate_summary = _load_json(aggregate_out / 'summary.json', {}) or {}
        status.update({
            'status': 'MERGED',
            'reason': 'compound_intelligence_merged_under_existing_canonical_writer',
            'accounts_processed': int(aggregate_summary.get('accounts_processed') or 0),
            'qwen_classified': int(aggregate_summary.get('qwen_classified') or 0),
            'deterministic_classified': int(aggregate_summary.get('deterministic_classified') or 0),
            'canonical_merge': _load_json(merge_out, {}) or {},
        })
        (outdir / 'merged.ok').write_text('ok\n', encoding='utf-8')
        _write_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False))
        return 0
    except (Exception, SystemExit) as exc:
        status['status'] = 'FAILED_OPEN'
        status['reason'] = 'compound_intelligence_merge_failed_without_touching_durable_inputs'
        status['error'] = str(exc)[-1600:]
        _write_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False))
        if args.strict:
            raise
        return 0
    finally:
        for p in (working_intel, working_canonical):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Run Hospitality Intelligence as a sidecar inside already-allocated harvest workers.')
    sub = ap.add_subparsers(dest='command', required=True)

    p = sub.add_parser('plan')
    p.add_argument('--harvest-plan', required=True)
    p.add_argument('--canonical-db', required=True)
    p.add_argument('--intelligence-db', required=True)
    p.add_argument('--config', required=True)
    p.add_argument('--outdir', required=True)
    p.set_defaults(func=plan_sidecars)

    a = sub.add_parser('aggregate')
    a.add_argument('--incoming', required=True)
    a.add_argument('--harvest-plan', required=True)
    a.add_argument('--canonical-db', required=True)
    a.add_argument('--intelligence-db', required=True)
    a.add_argument('--outdir', required=True)
    a.add_argument('--run-id', required=True)
    a.add_argument('--strict', action='store_true')
    a.set_defaults(func=aggregate_sidecars)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(args.func(args) or 0))


if __name__ == '__main__':
    main()
