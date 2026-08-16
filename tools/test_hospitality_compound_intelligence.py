from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

import hospitality_compound_intelligence as compound


class CompoundIntelligenceTests(unittest.TestCase):
    def test_plan_no_harvest_workers_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harvest = root / 'plan.json'
            config = root / 'config.json'
            harvest.write_text(json.dumps({'enabled': True, 'include': []}), encoding='utf-8')
            config.write_text(json.dumps({'compound_sidecar': {'enabled': True}}), encoding='utf-8')
            args = argparse.Namespace(
                harvest_plan=str(harvest),
                canonical_db=str(root / 'missing.sqlite'),
                intelligence_db=str(root / 'intel.sqlite'),
                config=str(config),
                outdir=str(root / 'sidecars'),
            )
            self.assertEqual(compound.plan_sidecars(args), 0)
            updated = json.loads(harvest.read_text(encoding='utf-8'))
            self.assertEqual(updated['compound_intelligence']['reason'], 'no_harvest_workers')
            sideplan = json.loads((root / 'sidecars' / 'plan.json').read_text(encoding='utf-8'))
            self.assertEqual(sideplan['include'], [])

    def test_incomplete_sidecars_never_touch_durable_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            incoming = root / 'incoming'
            incoming.mkdir()
            harvest = root / 'plan.json'
            harvest.write_text(json.dumps({
                'include': [{'key': 'x', 'compound_intel_records': 8}]
            }), encoding='utf-8')
            canonical = root / 'hospitality-canonical.sqlite'
            intelligence = root / 'hospitality-intelligence.sqlite'
            canonical.write_bytes(b'canonical-sentinel')
            intelligence.write_bytes(b'intelligence-sentinel')
            before_canonical = canonical.read_bytes()
            before_intelligence = intelligence.read_bytes()
            args = argparse.Namespace(
                incoming=str(incoming),
                harvest_plan=str(harvest),
                canonical_db=str(canonical),
                intelligence_db=str(intelligence),
                outdir=str(root / 'out'),
                run_id='test',
                strict=False,
            )
            self.assertEqual(compound.aggregate_sidecars(args), 0)
            self.assertEqual(canonical.read_bytes(), before_canonical)
            self.assertEqual(intelligence.read_bytes(), before_intelligence)
            status = json.loads((root / 'out' / 'compound-status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['reason'], 'incomplete_compound_sidecars_fail_open')

    def test_only_compound_intelligence_summaries_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('a', 'b'):
                d = root / name / 'compound_intelligence'
                d.mkdir(parents=True)
                (d / 'summary.json').write_text('{}', encoding='utf-8')
            other = root / 'unrelated'
            other.mkdir()
            (other / 'summary.json').write_text('{}', encoding='utf-8')
            dirs = compound._sidecar_dirs(root)
            self.assertEqual(len(dirs), 2)
            self.assertTrue(all(d.name == 'compound_intelligence' for d in dirs))

    def test_config_bounds_are_positive(self):
        cfg = compound._compound_cfg({'compound_sidecar': {
            'records_per_harvest_worker': 0,
            'max_accounts_per_cycle': 0,
            'standalone_overflow_max_workers': 0,
            'standalone_overflow_max_accounts': 0,
        }})
        self.assertEqual(cfg['records_per_harvest_worker'], 8)
        self.assertEqual(cfg['max_accounts_per_cycle'], 160)
        self.assertEqual(cfg['standalone_overflow_max_workers'], 4)
        self.assertEqual(cfg['standalone_overflow_max_accounts'], 80)


if __name__ == '__main__':
    unittest.main()
