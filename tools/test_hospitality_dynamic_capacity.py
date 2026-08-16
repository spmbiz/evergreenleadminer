#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import global_capacity_broker as broker
from hospitality_intelligence_worker_fast import deterministic_classification


class HospitalityDynamicCapacityTests(unittest.TestCase):
    def test_all_workload_caps_are_twenty(self):
        fleet = json.loads((ROOT / 'config/global_fleet.json').read_text(encoding='utf-8'))
        for workload in ('hospitality', 'tenders', 'gws'):
            self.assertEqual(int(fleet['workloads'][workload]['max_slots']), 20)
        intel = json.loads((ROOT / 'config/hospitality_intelligence_v2.json').read_text(encoding='utf-8'))
        self.assertEqual(int(intel['max_workers']), 20)
        self.assertEqual(int(intel['max_accounts_per_pass']), 400)

    def test_broker_demand_override_raises_only_current_workload_demand(self):
        original_demand = broker.v3.local_demand
        original_reserve = broker.v3.reserve
        seen = {}
        try:
            broker.v3.local_demand = lambda: {'hospitality': 0, 'tenders': 7, 'gws': 4}

            def fake_reserve(args):
                seen.update(broker.v3.local_demand())

            broker.v3.reserve = fake_reserve
            args = SimpleNamespace(workload='hospitality', demand_override=20)
            broker._reserve_with_optional_demand_override(args)
        finally:
            broker.v3.local_demand = original_demand
            broker.v3.reserve = original_reserve
        self.assertEqual(seen['hospitality'], 20)
        self.assertEqual(seen['tenders'], 7)
        self.assertEqual(seen['gws'], 4)

    def test_positive_fast_path_never_needs_a_negative_decision(self):
        cfg = {
            'enabled': True,
            'positive_only': True,
            'require_live_homepage': True,
            'min_operator_score': 80,
            'min_premium_score': 60,
            'min_property_count': 2,
            'strong_property_count': 5,
            'allow_pms_as_portfolio_evidence': True,
        }
        bundle = {
            'account': {
                'account_id': 'acct:example.com',
                'domain': 'example.com',
                'website': 'https://example.com',
                'operator_score': 91,
                'premium_score': 82,
                'fit_tier': 'A',
            },
            'portfolio': {
                'homepage_status': 200,
                'property_count_known': 8,
                'pms_fingerprints': ['guesty'],
            },
        }
        result = deterministic_classification(bundle, cfg)
        self.assertIsNotNone(result)
        self.assertEqual(result['fit_decision'], 'STRONG_FIT')
        self.assertEqual(result['entity_match'], 'MATCH')
        self.assertFalse(result.get('_classifier_error'))

        weak = {
            'account': {
                'account_id': 'acct:weak.example',
                'domain': 'weak.example',
                'website': 'https://weak.example',
                'operator_score': 40,
                'premium_score': 20,
                'fit_tier': '',
            },
            'portfolio': {'homepage_status': 200, 'property_count_known': 0, 'pms_fingerprints': []},
        }
        self.assertIsNone(deterministic_classification(weak, cfg))

    def test_workflow_is_brokered_twenty_slot_and_fail_safe(self):
        wf = (ROOT / '.github/workflows/hospitality-intelligence-v2.yml').read_text(encoding='utf-8')
        self.assertIn('max-parallel: 20', wf)
        self.assertIn('--demand-override "$REQUESTED"', wf)
        self.assertIn('group: ai-prod-global-capacity-broker', wf)
        self.assertIn('release_capacity_lease:', wf)
        self.assertIn('hospitality_intelligence_worker_fast.py', wf)
        self.assertIn("needs.intelligence.result == 'success'", wf)
        self.assertIn('cancel-in-progress: false', wf)
        self.assertIn("cron: '11,41 * * * *'", wf)


if __name__ == '__main__':
    unittest.main()
