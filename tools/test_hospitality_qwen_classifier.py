#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from hospitality_qwen_classifier import classify_batch, compact_record

HERE = Path(__file__).resolve().parent


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ''):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def record(i: int) -> dict:
    aid = f'acct:test-{i}.example'
    return {
        'account': {
            'account_id': aid,
            'name': f'Test Villas {i}',
            'country': 'US',
            'region': 'Florida',
            'city': 'Miami',
            'website': f'https://test-{i}.example',
            'operator_score': 90,
            'premium_score': 80,
            'fit_tier': 'A',
            'raw': {'categories': 'vacation rental'},
        },
        'portfolio': {
            'homepage_excerpt': 'villa ' * 1000,
            'property_count_known': 12,
            'first_party': {
                'contact_page': f'https://test-{i}.example/contact',
                'portfolio_url': f'https://test-{i}.example/villas',
            },
            'pms_fingerprints': ['guesty'],
        },
        'search_results': [
            {'provider': 'openserp', 'query_family': 'identity', 'title': 'T' * 500, 'url': 'https://example.com/' + 'u' * 500, 'snippet': 'S' * 1200}
            for _ in range(8)
        ],
    }


class QwenClassifierTests(unittest.TestCase):
    def test_compact_record_has_hard_evidence_bounds(self):
        c = compact_record(record(0))
        self.assertLessEqual(len(c['homepage_excerpt']), 650)
        self.assertLessEqual(len(c['search']), 3)
        self.assertTrue(all(len(x['s']) <= 180 for x in c['search']))
        self.assertTrue(all(len(x['u']) <= 220 for x in c['search']))
        self.assertTrue(all(len(v) <= 180 for v in c['first_party'].values()))

    def test_llama_server_uses_one_full_context_slot(self):
        script = (HERE / 'start_qwen4b_ci.sh').read_text(encoding='utf-8')
        self.assertIn('--ctx-size 8192 --parallel 1', script)
        self.assertNotIn('--parallel 2', script)

    def test_http_400_batch_recursively_splits_instead_of_losing_accounts(self):
        calls = []

        def fake_get(url, timeout=3):
            return FakeResponse(200, {'status': 'ok'})

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            user = json['messages'][1]['content']
            items = __import__('json').loads(user.split('INPUT=', 1)[1])
            if len(items) > 1:
                return FakeResponse(400, {'error': {'message': 'context too large'}}, 'context too large')
            aid = items[0]['account_id']
            content = __import__('json').dumps({'items': [{
                'account_id': aid,
                'entity_match': 'MATCH',
                'business_type': 'SHORT_STAY_OPERATOR',
                'fit_decision': 'FIT',
                'confidence': 0.9,
                'unusual_or_novel': False,
                'matching_evidence': ['public evidence'],
                'contradictions': [],
                'reason': 'valid test classification',
            }]})
            return FakeResponse(200, {'choices': [{'message': {'content': content}}]})

        with patch('hospitality_qwen_classifier.requests.get', side_effect=fake_get), patch('hospitality_qwen_classifier.requests.post', side_effect=fake_post):
            out = classify_batch([record(i) for i in range(4)], 'http://127.0.0.1:8080', 'qwen-test', timeout=1)

        self.assertEqual(len(out), 4)
        self.assertTrue(all(x['fit_decision'] == 'FIT' for x in out))
        self.assertTrue(all(not x.get('_classifier_error') for x in out))
        self.assertGreaterEqual(len(calls), 7)

    def test_invalid_free_json_retries_with_json_constraint(self):
        post_count = 0

        def fake_get(url, timeout=3):
            return FakeResponse(200, {'status': 'ok'})

        def fake_post(url, json=None, timeout=None):
            nonlocal post_count
            post_count += 1
            user = json['messages'][1]['content']
            items = __import__('json').loads(user.split('INPUT=', 1)[1])
            aid = items[0]['account_id']
            if 'response_format' not in json:
                return FakeResponse(200, {'choices': [{'message': {'content': 'not-json'}}]})
            content = __import__('json').dumps({'items': [{
                'account_id': aid,
                'entity_match': 'PROBABLE',
                'business_type': 'BOUTIQUE_HOTEL',
                'fit_decision': 'FIT',
                'confidence': 0.8,
                'unusual_or_novel': False,
                'matching_evidence': [],
                'contradictions': [],
                'reason': 'constrained retry worked',
            }]})
            return FakeResponse(200, {'choices': [{'message': {'content': content}}]})

        with patch('hospitality_qwen_classifier.requests.get', side_effect=fake_get), patch('hospitality_qwen_classifier.requests.post', side_effect=fake_post):
            out = classify_batch([record(1)], 'http://127.0.0.1:8080', 'qwen-test', timeout=1)

        self.assertEqual(out[0]['business_type'], 'BOUTIQUE_HOTEL')
        self.assertFalse(out[0].get('_classifier_error'))
        self.assertEqual(post_count, 2)


if __name__ == '__main__':
    unittest.main()
