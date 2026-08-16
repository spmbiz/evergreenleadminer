#!/usr/bin/env python3
from __future__ import annotations

import unittest

import gws_qwen_semantic as q
import gws_semantic_plan as plan
import gws_semantic_worker as worker


class GwsSemanticTests(unittest.TestCase):
    def test_model_cannot_invent_candidate_url(self):
        expected={'biz:1':'https://example.com/'}
        item={'business_id':'biz:1','candidate_url':'https://evil.example/','decision':'MATCH','confidence':.9,'website_state':'GOOD_SITE','matching_evidence':['x'],'contradictions':[],'needs_gpt_review':False,'reason':'x'}
        out=q.validate_item(item,expected)
        self.assertEqual(out['candidate_url'],'https://example.com/')

    def test_strict_high_benchmark_has_no_candidate_identity(self):
        row={'record_key':'hub:1','hub_name':'Example','hub_address':'Rue 1','hub_postalcode':'1050','outcome':'HIGH','reason':'VERIFIED_NO_WEBSITE','verification_status':'HIGH','web_pass1':{'search_candidates':['https://possible.example/']},'certificate':{}}
        rec=plan.compact(row,'qwen','p1')
        self.assertEqual(rec['candidate_url'],'')
        self.assertEqual(rec['benchmark_kind'],'STRICT_NO_SITE')

    def test_owned_site_benchmark_keeps_evidence_url(self):
        row={'record_key':'hub:2','hub_name':'Example','hub_address':'Rue 2','hub_postalcode':'1050','outcome':'REJECT','reason':'OWNED_SITE_SEARCH_CONFIRMED','verification_status':'REJECT','owned_website':'https://example.com/','certificate':{}}
        rec=plan.compact(row,'qwen','p1')
        self.assertEqual(rec['candidate_url'],'https://example.com/')
        self.assertEqual(rec['benchmark_kind'],'OWNED_SITE_POSITIVE')

    def test_benchmark_positive_requires_match(self):
        rec={'benchmark_kind':'OWNED_SITE_POSITIVE','candidate_url':'https://example.com/'}
        self.assertTrue(worker.benchmark_pass(rec,{'decision':'PROBABLE','website_state':'GOOD_SITE'}))
        self.assertFalse(worker.benchmark_pass(rec,{'decision':'WRONG','website_state':'GOOD_SITE'}))

    def test_fallback_is_uncertain_and_review(self):
        out=q.fallback([{'business_id':'b','candidate_url':''}],'OFFLINE')[0]
        self.assertEqual(out['decision'],'UNCERTAIN')
        self.assertEqual(out['website_state'],'UNCERTAIN')
        self.assertTrue(out['needs_gpt_review'])


if __name__=='__main__': unittest.main()
