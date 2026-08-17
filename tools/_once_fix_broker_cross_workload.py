#!/usr/bin/env python3
from pathlib import Path

PLAN_GROUPS = {
    '.github/workflows/gws-pending-search-verify.yml': 'ai-prod-global-capacity-broker-gws',
    '.github/workflows/gws-autonomous-fleet.yml': 'ai-prod-global-capacity-broker-gws',
    '.github/workflows/gws-semantic-fleet.yml': 'ai-prod-global-capacity-broker-gws',
    '.github/workflows/hospitality-autonomous-fleet.yml': 'ai-prod-global-capacity-broker-hospitality',
    '.github/workflows/hospitality-intelligence-v2.yml': 'ai-prod-global-capacity-broker-hospitality',
}

for rel, group in PLAN_GROUPS.items():
    p = Path(rel)
    s = p.read_text(encoding='utf-8')
    marker = '  plan:\n    concurrency:\n      group: ai-prod-global-capacity-broker\n      cancel-in-progress: false\n'
    replacement = f'  plan:\n    concurrency:\n      group: {group}\n      cancel-in-progress: false\n'
    if marker in s:
        s = s.replace(marker, replacement, 1)
        p.write_text(s, encoding='utf-8')
        print(f'patched planner lock: {rel} -> {group}')
    elif replacement in s:
        print(f'already patched planner lock: {rel}')
    else:
        raise SystemExit(f'planner broker marker missing: {rel}')

p = Path('tools/global_capacity_broker_v3.py')
s = p.read_text(encoding='utf-8')
if 'import time\n' not in s:
    s = s.replace('import tempfile\n', 'import tempfile\nimport time\n', 1)

anchor = '''def save_remote_state(repo: str, state: dict):
    state["updated_at"] = iso(now_utc())
    with tempfile.TemporaryDirectory(prefix="global-fleet-broker-") as td:
        p = Path(td) / STATE_ASSET
        p.write_text(json.dumps(state, indent=2) + "\\n", encoding="utf-8")
        fr.release_upload(repo, STATE_TAG, str(p))
'''
helper = '''

def save_reservation_reconciled(repo: str, intended_state: dict, lease: dict | None, attempts: int = 5):
    """Persist admission without losing a sibling workload's concurrent lease."""
    if lease is None:
        save_remote_state(repo, intended_state)
        return {"confirmed": True, "attempts": 1, "mode": "zero_slot"}

    run_id = str(lease.get("run_id") or "")
    if not run_id:
        raise RuntimeError("refusing to persist broker lease without run_id")

    candidate = dict(intended_state)
    for attempt in range(1, max(1, attempts) + 1):
        fresh_before = load_remote_state(repo)
        fresh_leases = prune_leases(fresh_before)
        merged = [x for x in fresh_leases if str(x.get("run_id") or "") != run_id]
        merged.append(lease)
        candidate["schema_version"] = 3
        candidate["leases"] = merged
        save_remote_state(repo, candidate)

        fresh_after = load_remote_state(repo)
        confirmed = any(
            str(x.get("run_id") or "") == run_id
            and int(x.get("slots") or 0) == int(lease.get("slots") or 0)
            for x in (fresh_after.get("leases") or [])
        )
        if confirmed:
            return {"confirmed": True, "attempts": attempt, "mode": "optimistic_reconcile"}
        time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))

    raise RuntimeError(
        f"broker reservation persistence race unresolved after {attempts} attempts run_id={run_id}"
    )
'''
if 'def save_reservation_reconciled(' not in s:
    if anchor not in s:
        raise SystemExit('save_remote_state anchor missing in broker v3')
    s = s.replace(anchor, anchor + helper, 1)

old = '''    if not args.dry_run:
        save_remote_state(args.repo, state)
    payload = dict(decision)
    payload.update({"lease": lease, "dry_run": bool(args.dry_run)})
'''
new = '''    persistence = {"confirmed": True, "attempts": 0, "mode": "dry_run"}
    if not args.dry_run:
        persistence = save_reservation_reconciled(args.repo, state, lease)
    payload = dict(decision)
    payload.update({"lease": lease, "dry_run": bool(args.dry_run), "reservation_persistence": persistence})
'''
if old in s:
    s = s.replace(old, new, 1)
elif 'reservation_persistence' not in s:
    raise SystemExit('reserve persistence anchor missing in broker v3')

p.write_text(s, encoding='utf-8')

for f, group in PLAN_GROUPS.items():
    text = Path(f).read_text(encoding='utf-8')
    assert f'group: {group}' in text, f
    assert text.count('group: ai-prod-global-capacity-broker') >= 1, f
broker = Path('tools/global_capacity_broker_v3.py').read_text(encoding='utf-8')
assert 'def save_reservation_reconciled' in broker
assert 'reservation_persistence' in broker
print('PATCH_INVARIANTS_OK')
