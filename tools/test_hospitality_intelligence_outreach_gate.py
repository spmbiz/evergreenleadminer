#!/usr/bin/env python3
from __future__ import annotations

import unittest

from hospitality_intelligence_outreach_gate import evaluate


CFG = {
    'min_confidence': 0.70,
    'premium_signal_score': 24,
    'strong_premium_score': 42,
    'portfolio_signal_count': 3,
    'strong_portfolio_count': 10,
    'good_b_contactability_min': 48,
    'premium_property_score_min': 60,
    'premium_property_commercial_min': 58,
}


def base_v2(**overrides):
    row = {
        'account_id': 'acct:example.com',
        'domain': 'example.com',
        'name': 'Example Luxury Villas',
        'fit_decision': 'STRONG_FIT',
        'entity_match': 'MATCH',
        'business_type': 'SHORT_STAY_OPERATOR',
        'confidence': 0.95,
        'commercial_score': 88,
        'contactability_score': 88,
        'portfolio_leverage_score': 85,
        'property_count_known': 18,
        'sample_property_urls': ['https://example.com/luxury-villas/oceanfront-estate'],
        'matching_evidence': ['verified villa portfolio'],
        'classification_reason': 'verified short stay operator',
        'public_email': 'sales@example.com',
        'public_phone': '',
        'instagram': 'https://instagram.com/example',
        'facebook': '',
        'whatsapp': '',
        'contact_page': 'https://example.com/contact',
        'classifier_error': '',
    }
    row.update(overrides)
    return row


def base_v1(**overrides):
    row = {
        'account_id': 'acct:example.com',
        'name': 'Example Luxury Villas',
        'live_status': 'HIGH',
        'fit_tier': 'A',
        'operator_score': 88,
        'premium_score': 48,
        'raw': {'brand': 'Example Luxury Villas', 'category': 'vacation rental'},
    }
    row.update(overrides)
    return row


class OutreachGateTests(unittest.TestCase):
    def test_strong_premium_portfolio_is_ready(self):
        r = evaluate(base_v2(), base_v1(), CFG)
        self.assertEqual(r['commercial_tier'], 'S')
        self.assertTrue(r['outreach_ready'])
        self.assertTrue(r['premium_signal'])
        self.assertTrue(r['v1_permissive_pass'])

    def test_good_b_requires_real_premium_and_contactability(self):
        v2 = base_v2(commercial_score=66, portfolio_leverage_score=58, property_count_known=5, contactability_score=58)
        v1 = base_v1(operator_score=62, premium_score=24)
        r = evaluate(v2, v1, CFG)
        self.assertEqual(r['commercial_tier'], 'B')
        self.assertTrue(r['outreach_ready'])

    def test_premium_standalone_hotel_does_not_need_portfolio_count(self):
        v2 = base_v2(
            name='Alpine Design Hotel', business_type='BOUTIQUE_HOTEL', commercial_score=61,
            portfolio_leverage_score=25, property_count_known=0, sample_property_urls=[],
            matching_evidence=['verified boutique hotel'], classification_reason='premium boutique hotel',
        )
        v1 = base_v1(
            name='Alpine Design Hotel', operator_score=45, premium_score=75,
            raw={'brand':'Alpine Design Hotel', 'category':'boutique hotel'},
        )
        r = evaluate(v2, v1, CFG)
        self.assertEqual(r['commercial_tier'], 'A')
        self.assertTrue(r['premium_property_path'])
        self.assertTrue(r['outreach_ready'])

    def test_non_premium_operator_stays_out_of_outreach(self):
        v2 = base_v2(name='Generic Property Management', commercial_score=82, property_count_known=30, sample_property_urls=[], matching_evidence=[], classification_reason='short stay operator')
        v1 = base_v1(name='Generic Property Management', premium_score=0, raw={'brand':'','category':'property management'})
        r = evaluate(v2, v1, CFG)
        self.assertEqual(r['commercial_tier'], 'C')
        self.assertFalse(r['outreach_ready'])

    def test_clear_semantic_reject_never_outreach(self):
        r = evaluate(base_v2(fit_decision='REJECT_OBVIOUS'), base_v1(), CFG)
        self.assertEqual(r['commercial_tier'], 'REJECT')
        self.assertFalse(r['outreach_ready'])

    def test_qualified_without_contact_is_not_ready(self):
        v2 = base_v2(public_email='', instagram='', contact_page='', public_phone='', facebook='', whatsapp='')
        r = evaluate(v2, base_v1(), CFG)
        self.assertIn(r['commercial_tier'], {'S','A'})
        self.assertFalse(r['outreach_ready'])
        self.assertIn('qualified_but_no_public_contact_route', r['outreach_reasons'])


if __name__ == '__main__':
    unittest.main()
