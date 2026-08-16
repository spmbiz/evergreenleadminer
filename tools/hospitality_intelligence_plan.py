#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

from hospitality_intelligence_db import account_id_for_domain, connect, material_hash, retry_due, stale_due

RETRYABLE_STATUSES = {'UNRESOLVED', 'QWEN_UNAVAILABLE', 'SEARCH_FAILED', 'ERROR_RETRYABLE', 'PARTIAL'}


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--canonical-db', required=True)
    ap.add_argument('--intelligence-db', required=True)
    ap.add_argument('--config', required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--max-accounts', type=int, default=0, help='Optional one-run cap; 0 keeps config value')
    ap.add_argument('--max-workers', type=int, default=0, help='Optional one-run worker cap; 0 keeps config value')
    a = ap.parse_args()

    cfg = load_config(a.config)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    if not cfg.get('enabled', True):
        (out / 'plan.json').write_text(json.dumps({'enabled': False, 'include': [], 'reason': 'disabled'}, indent=2) + '\n')
        return

    icon = connect(a.intelligence_db)
    prior = {r['domain']: dict(r) for r in icon.execute('SELECT * FROM accounts')}

    ccon = sqlite3.connect(a.canonical_db)
    ccon.row_factory = sqlite3.Row
    rows = list(ccon.execute('SELECT * FROM leads ORDER BY last_seen DESC'))
    ccon.close()

    retry_hours = int(cfg.get('retry_hours') or 48)
    refresh_days = int(cfg.get('refresh_days') or 30)
    configured_max_accounts = int(cfg.get('max_accounts_per_pass') or 10000)
    max_accounts = int(a.max_accounts) if int(a.max_accounts or 0) > 0 else configured_max_accounts
    candidates = []
    counts = {'canonical_total': len(rows), 'new': 0, 'changed': 0, 'retry': 0, 'stale': 0, 'unchanged_skipped': 0}

    for row in rows:
        d = (row['domain'] or '').strip().lower()
        if not d:
            continue
        aid = account_id_for_domain(d)
        mh = material_hash(row)
        old = prior.get(d)
        reason = None
        if a.force:
            reason = 'FORCE'
        elif not old:
            reason = 'NEW'
            counts['new'] += 1
        elif (old.get('material_hash') or '') != mh:
            reason = 'CHANGED'
            counts['changed'] += 1
        elif (old.get('status') or '') in RETRYABLE_STATUSES and retry_due(old.get('last_attempt_at'), retry_hours):
            reason = 'RETRY'
            counts['retry'] += 1
        elif stale_due(old.get('last_classified_at'), refresh_days):
            reason = 'STALE_REFRESH'
            counts['stale'] += 1
        else:
            counts['unchanged_skipped'] += 1
            continue

        try:
            raw = json.loads(row['raw_json'] or '{}')
        except Exception:
            raw = {}
        rec = dict(row)
        rec['account_id'] = aid
        rec['material_hash'] = mh
        rec['queue_reason'] = reason
        rec['raw'] = raw
        priority = {'CHANGED': 400, 'NEW': 300, 'RETRY': 200, 'STALE_REFRESH': 100, 'FORCE': 500}.get(reason, 0)
        priority += min(100, int(row['operator_score'] or 0)) + min(100, int(row['premium_score'] or 0))
        rec['_priority'] = priority
        candidates.append(rec)

    candidates.sort(key=lambda r: (-int(r['_priority']), r['domain']))
    if max_accounts > 0:
        candidates = candidates[:max_accounts]

    configured_max_workers = max(1, int(cfg.get('max_workers') or 10))
    max_workers = min(configured_max_workers, max(1, int(a.max_workers))) if int(a.max_workers or 0) > 0 else configured_max_workers
    shard_size = max(1, int(cfg.get('shard_size') or 250))
    worker_count = min(max_workers, max(1, math.ceil(len(candidates) / shard_size))) if candidates else 0
    if candidates and worker_count:
        shards = [[] for _ in range(worker_count)]
        for idx, rec in enumerate(candidates):
            rec.pop('_priority', None)
            shards[idx % worker_count].append(rec)
    else:
        shards = []

    include = []
    for idx, shard in enumerate(shards):
        path = out / f'shard-{idx:02d}.jsonl'
        with path.open('w', encoding='utf-8') as f:
            for rec in shard:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        include.append({'shard': idx, 'path': path.name, 'records': len(shard)})

    plan = {
        'enabled': bool(candidates),
        'accounts_planned': len(candidates),
        'worker_count': worker_count,
        'include': include,
        'counts': counts,
        'config': {
            'max_workers': max_workers,
            'max_accounts': max_accounts,
            'shard_size': shard_size,
            'retry_hours': retry_hours,
            'refresh_days': refresh_days,
            'smoke_override': bool(int(a.max_accounts or 0) > 0 or int(a.max_workers or 0) > 0),
        },
    }
    (out / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(plan, ensure_ascii=False))
    icon.close()


if __name__ == '__main__':
    main()
