#!/usr/bin/env python3
import unittest
from hospitality_intelligence_outreach_gate import evaluate
CFG={'min_confidence':.70,'good_b_contactability_min':48}
def v2(**kw):
 r={'account_id':'acct:x','domain':'x.com','name':'Example Luxury Villas','fit_decision':'STRONG_FIT','entity_match':'MATCH','confidence':.95,'commercial_score':88,'contactability_score':88,'portfolio_leverage_score':85,'property_count_known':18,'matching_evidence':['luxury villa rentals verified'],'classification_reason':'verified short stay operator','public_email':'sales@x.com','instagram':'https://instagram.com/example','contact_page':'https://x.com/contact','classifier_error':''};r.update(kw);return r
def v1(**kw):
 r={'account_id':'acct:x','name':'Example Luxury Villas','live_status':'HIGH','fit_tier':'A','operator_score':88,'premium_score':80,'quality_version':'HOSPITALITY_COMMERCIAL_FIT_V2_1','commercial_fit_tier':'A','premium_score_v2':80,'operator_score_v2':80,'sales_ready':'YES','quality_decision':'ACCEPT','quality_reason':'ok','raw':{'category':'vacation rental'}};r.update(kw);return r
class T(unittest.TestCase):
 def test_strong_ready(self):
  r=evaluate(v2(),v1(),CFG);self.assertEqual(r['commercial_tier'],'S');self.assertTrue(r['outreach_ready']);self.assertFalse(r['v1_permissive_pass'])
 def test_permissive_live_never_ready(self):
  r=evaluate(v2(),v1(live_status='PERMISSIVE'),CFG);self.assertEqual(r['commercial_tier'],'C');self.assertFalse(r['outreach_ready'])
 def test_quality_c_never_ready(self):
  r=evaluate(v2(),v1(commercial_fit_tier='C',sales_ready='NO',quality_decision='REVIEW'),CFG);self.assertEqual(r['commercial_tier'],'C');self.assertFalse(r['outreach_ready'])
 def test_quality_x_reject(self):
  r=evaluate(v2(),v1(commercial_fit_tier='X',sales_ready='NO',quality_decision='REJECT'),CFG);self.assertEqual(r['commercial_tier'],'REJECT')
 def test_semantic_positive_cannot_override_hostel(self):
  legacy=v1(quality_version='',commercial_fit_tier='',sales_ready='',name='a&o Wien Hauptbahnhof',raw={'category':'hostel'})
  r=evaluate(v2(name='a&o Wien Hauptbahnhof',matching_evidence=['hotel rooms']),legacy,CFG);self.assertEqual(r['commercial_tier'],'REJECT')
 def test_bad_social_removed(self):
  r=evaluate(v2(facebook='https://facebook.com/privacy/explanation/'),v1(),CFG);self.assertEqual(r['facebook'],'')
if __name__=='__main__':unittest.main()
