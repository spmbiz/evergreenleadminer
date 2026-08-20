from __future__ import annotations

import json
import urllib.request
from pathlib import Path

CONFIG = Path('config/global_fleet.json')
SUMMARY_URL = 'https://raw.githubusercontent.com/walidgdg1-ai/tender-engine/main/control/qwen_live/classification_summary.json'
BROKER = Path('tools/global_capacity_broker_v3.py')
OLD_REPO = 'TENDER_REPO = "spmbiz/tender-engine"'
NEW_REPO = 'TENDER_REPO = "walidgdg1-ai/tender-engine"'


def fetch_remaining() -> int:
    req = urllib.request.Request(SUMMARY_URL, headers={'User-Agent': 'global-fleet-tender-backfill-policy/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode('utf-8'))
    return max(0, int(payload.get('remaining_classification_queue') or 0))


def split_for(remaining: int) -> tuple[int, int]:
    if remaining >= 50_000:
        return 16, 4
    if remaining >= 20_000:
        return 14, 6
    if remaining >= 5_000:
        return 12, 8
    return 10, 10


def main() -> None:
    remaining = fetch_remaining()
    tender_slots, gws_slots = split_for(remaining)
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    total = int((cfg.get('github') or {}).get('capacity') or 20)
    if total != 20:
        raise SystemExit(f'Unexpected GitHub capacity {total}; refusing automatic split rewrite')

    workloads = cfg['workloads']
    tenders = workloads['tenders']
    gws = workloads['gws']
    tenders['weight'] = tender_slots / total
    tenders['min_slots_when_demanding'] = tender_slots
    tenders['max_slots'] = 20
    tenders['repo'] = 'walidgdg1-ai/tender-engine'
    gws['weight'] = gws_slots / total
    gws['min_slots_when_demanding'] = gws_slots
    gws['max_slots'] = 10
    CONFIG.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')

    # Repair the stale repository identity while this policy is in control.
    broker = BROKER.read_text(encoding='utf-8')
    if NEW_REPO not in broker:
        if OLD_REPO not in broker:
            raise SystemExit('Tender broker repository marker missing')
        broker = broker.replace(OLD_REPO, NEW_REPO, 1)
        BROKER.write_text(broker, encoding='utf-8')

    print(json.dumps({
        'remaining_classification_queue': remaining,
        'tender_min_slots': tender_slots,
        'gws_min_slots': gws_slots,
        'policy': '16/4 >=50k; 14/6 >=20k; 12/8 >=5k; otherwise 10/10',
        'preemption': 'none; natural completion only',
    }, indent=2))


if __name__ == '__main__':
    main()
