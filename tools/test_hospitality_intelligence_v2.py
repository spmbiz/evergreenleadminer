#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from hospitality_intelligence_db import connect, material_hash
from hospitality_search_fabric import query_specs


class HospitalityIntelligenceV2Tests(unittest.TestCase):
    def make_canonical(self, path: Path, email: str = 'old@example.com') -> None:
        con = sqlite3.connect(path)
        con.execute('''CREATE TABLE leads(domain TEXT PRIMARY KEY,name TEXT,country TEXT,region TEXT,city TEXT,state TEXT,street TEXT,website TEXT,public_email TEXT,public_phone TEXT,instagram TEXT,live_status TEXT,fit_tier TEXT,operator_score INTEGER,premium_score INTEGER,source_url TEXT,overture_id TEXT,first_seen TEXT,last_seen TEXT,source_release TEXT,raw_json TEXT)''')
        raw = {'facebook': '', 'contact_page': ''}
        con.execute('INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            'example.com','Example Villas','US','Florida','Miami','FL','1 Ocean Dr','https://example.com',email,'+13055550000','',
            'LIVE','A',90,80,'src','ov1','2026-01-01T00:00:00Z','2026-08-16T00:00:00Z','r1',json.dumps(raw)
        ))
        con.commit(); con.close()

    def test_query_specs_are_bounded_and_high_recall(self):
        specs = query_specs({'name':'Example Villas','city':'Miami','public_phone':'+1305','public_email':'','instagram':'','raw':{}}, max_queries=3)
        self.assertEqual(len(specs), 3)
        self.assertEqual(specs[0][0], 'identity')

    def test_plan_new_then_skip_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            canonical, intel, out = td/'canonical.sqlite', td/'intel.sqlite', td/'plan'
            self.make_canonical(canonical)
            cfg = td/'cfg.json'; cfg.write_text(json.dumps({'enabled':True,'max_workers':10,'shard_size':50,'retry_hours':48,'refresh_days':30,'max_accounts_per_pass':1000}))
            subprocess.check_call([sys.executable, str(HERE/'hospitality_intelligence_plan.py'), '--canonical-db',str(canonical),'--intelligence-db',str(intel),'--config',str(cfg),'--outdir',str(out),'--max-accounts','1','--max-workers','1'], env={**os.environ,'PYTHONPATH':str(HERE)})
            plan = json.loads((out/'plan.json').read_text())
            self.assertEqual(plan['accounts_planned'], 1)
            self.assertEqual(plan['worker_count'], 1)
            self.assertTrue(plan['config']['smoke_override'])
            c = connect(intel)
            row = sqlite3.connect(canonical); row.row_factory=sqlite3.Row; lead=row.execute('select * from leads').fetchone(); row.close()
            mh = material_hash(lead)
            c.execute('''INSERT INTO accounts(account_id,domain,name,first_seen,last_seen,material_hash,last_material_change_at,last_attempt_at,last_classified_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      ('acct:example.com','example.com','Example Villas','2026-01-01T00:00:00Z','2026-08-16T00:00:00Z',mh,'2026-08-16T00:00:00Z','2026-08-16T00:00:00Z','2026-08-16T00:00:00Z','READY_SHADOW'))
            c.commit(); c.close()
            out2=td/'plan2'
            subprocess.check_call([sys.executable, str(HERE/'hospitality_intelligence_plan.py'), '--canonical-db',str(canonical),'--intelligence-db',str(intel),'--config',str(cfg),'--outdir',str(out2)], env={**os.environ,'PYTHONPATH':str(HERE)})
            plan2=json.loads((out2/'plan.json').read_text())
            self.assertEqual(plan2['accounts_planned'],0)
            self.assertEqual(plan2['counts']['unchanged_skipped'],1)

    def test_aggregate_and_fill_only_merge(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); canonical=td/'canonical.sqlite'; intel=td/'intel.sqlite'; results=td/'results'/'w0'; results.mkdir(parents=True)
            self.make_canonical(canonical, email='old@example.com')
            account = {
                'account_id':'acct:example.com','domain':'example.com','name':'Example Villas','country':'US','region':'Florida','city':'Miami','website':'https://example.com',
                'first_seen':'2026-01-01T00:00:00Z','last_seen':'2026-08-16T00:00:00Z','material_hash':'abc','queue_reason':'NEW','entity_match':'MATCH',
                'business_type':'SHORT_STAY_OPERATOR','fit_decision':'STRONG_FIT','confidence':0.95,'unusual_or_novel':False,'matching_evidence':['site text'],'contradictions':[],
                'classification_reason':'short stay villas','classifier_error':'','public_email':'new@example.com','public_email_source_url':'https://example.com/contact','public_phone':'+13055550000',
                'instagram':'https://instagram.com/example','instagram_first_party':'https://instagram.com/example','facebook':'https://facebook.com/example','facebook_first_party':'https://facebook.com/example',
                'whatsapp':'','whatsapp_first_party':'','contact_page':'https://example.com/contact','contact_page_first_party':'https://example.com/contact','portfolio_url':'https://example.com/villas','portfolio_url_first_party':'https://example.com/villas',
                'pms_fingerprints':['guesty'],'property_count_known':12,'portfolio_hash':'ph','sample_property_urls':['https://example.com/villas/a'],
                'contactability_score':88,'portfolio_leverage_score':75,'commercial_score':84,'search_deferred':False,'search_results_count':2,'homepage_status':200,'pms_detected':True,
                'status':'READY_SHADOW','fetch_errors':[],'classifier_model':'qwen3-4b-q4_k_m','classifier_prompt_version':'v1','processed_at':'2026-08-16T01:00:00Z'
            }
            with gzip.open(results/'accounts.jsonl.gz','wt') as f:f.write(json.dumps(account)+'\n')
            with gzip.open(results/'assets.jsonl.gz','wt') as f:f.write(json.dumps({'asset_id':'asset:1','account_id':'acct:example.com','property_name':'Villa A','url':'https://example.com/villas/a','source_type':'sitemap','sample_priority':9,'seen_at':'2026-08-16T01:00:00Z'})+'\n')
            with gzip.open(results/'search-results.jsonl.gz','wt') as f:f.write('')
            (results/'search-events.jsonl').write_text('')
            (results/'summary.json').write_text(json.dumps({'qwen_classified':1,'qwen_unavailable':0,'errors':0}))
            out=td/'agg'
            subprocess.check_call([sys.executable,str(HERE/'hospitality_intelligence_aggregate.py'),'--results-root',str(td/'results'),'--intelligence-db',str(intel),'--run-id','r1','--outdir',str(out)], env={**os.environ,'PYTHONPATH':str(HERE)})
            c=connect(intel); r=c.execute('select * from accounts where account_id=?',('acct:example.com',)).fetchone(); self.assertEqual(r['property_count_known'],12); c.close()
            merge=td/'merge.json'
            subprocess.check_call([sys.executable,str(HERE/'apply_hospitality_intelligence_delta.py'),'--canonical-db',str(canonical),'--delta',str(out/'canonical-intelligence-delta.jsonl.gz'),'--out-summary',str(merge)])
            c=sqlite3.connect(canonical); c.row_factory=sqlite3.Row; r=c.execute('select * from leads where domain=?',('example.com',)).fetchone(); raw=json.loads(r['raw_json']); c.close()
            self.assertEqual(r['public_email'],'old@example.com')
            self.assertEqual(r['instagram'],'https://instagram.com/example')
            self.assertEqual(raw['property_count_known'],12)
            self.assertEqual(raw['intelligence_business_type'],'SHORT_STAY_OPERATOR')

    def test_worker_to_aggregate_cli_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            worker_out=td/'worker'; worker_out.mkdir()
            rec={
                'account_id':'acct:example.com','domain':'example.com','name':'Example Villas','country':'US','region':'Florida','city':'Miami',
                'website':'','public_email':'','public_phone':'','instagram':'','operator_score':90,'premium_score':80,'fit_tier':'A',
                'first_seen':'2026-01-01T00:00:00Z','last_seen':'2026-08-16T00:00:00Z','material_hash':'smoke','queue_reason':'NEW','raw':{}
            }
            inp=td/'input.jsonl'; inp.write_text(json.dumps(rec)+'\n')
            cfg=td/'cfg.json'; cfg.write_text(json.dumps({
                'qwen':{'enabled':False,'model_label':'qwen3-4b-q4_k_m','prompt_version':'smoke','batch_size':8},
                'search':{'account_budget_per_worker':0,'max_queries_per_account':0},
                'portfolio':{'workers':1,'timeout_seconds':1,'max_bytes_per_fetch':1000,'max_sitemaps':1,'max_sitemap_urls':1,'max_assets':1,'sample_count':1}
            }))
            subprocess.check_call([sys.executable,str(HERE/'hospitality_intelligence_worker.py'),'--input',str(inp),'--config',str(cfg),'--qwen-url','http://127.0.0.1:65534','--shard','smoke','--outdir',str(worker_out)], env={**os.environ,'PYTHONPATH':str(HERE)})
            self.assertTrue((worker_out/'accounts.jsonl.gz').is_file())
            self.assertTrue((worker_out/'summary.json').is_file())
            intel=td/'intel.sqlite'; agg=td/'agg'
            subprocess.check_call([sys.executable,str(HERE/'hospitality_intelligence_aggregate.py'),'--results-root',str(td),'--intelligence-db',str(intel),'--run-id','smoke','--outdir',str(agg)], env={**os.environ,'PYTHONPATH':str(HERE)})
            self.assertTrue((agg/'summary.json').is_file())
            self.assertTrue((agg/'canonical-intelligence-delta.jsonl.gz').is_file())
            summary=json.loads((worker_out/'summary.json').read_text())
            self.assertEqual(summary['accounts_processed'],1)
            self.assertEqual(summary['qwen_unavailable'],1)

    def test_workflow_contract_matches_python_contracts(self):
        wf=(ROOT/'.github/workflows/hospitality-intelligence-v2.yml').read_text(encoding='utf-8')
        self.assertNotIn('--worker-key', wf)
        self.assertIn('--shard "${{ matrix.shard }}"', wf)
        self.assertIn('--qwen-url "$QWEN_URL"', wf)
        self.assertIn('--run-id "$INTEL_CYCLE_ID"', wf)
        self.assertNotIn('--cycle-id "$INTEL_CYCLE_ID"', wf)
        self.assertIn('canonical-intelligence-delta.jsonl.gz', wf)
        self.assertNotIn('canonical-fill-delta.jsonl.gz', wf)
        self.assertIn('aggregate_out/summary.json', wf)
        self.assertIn('hospitality-intelligence-ledger-writer', wf)
        self.assertIn('hospitality-canonical-writer', wf)
        # Trigger selection must be commit-bound, never filesystem-mtime based.
        self.assertIn('fetch-depth: 2', wf)
        self.assertIn('git diff-tree --no-commit-id --name-only -r "$GITHUB_SHA"', wf)
        self.assertNotIn("find control -maxdepth 1 -type f -name 'hospitality_intelligence_trigger", wf)


if __name__ == '__main__':
    unittest.main()
