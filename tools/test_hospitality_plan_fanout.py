#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class HospitalityPlanFanoutTests(unittest.TestCase):
    def test_explicit_20x4_produces_four_five_row_shards(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            canonical = td / 'canonical.sqlite'
            intel = td / 'intel.sqlite'
            out = td / 'plan'
            cfg = td / 'cfg.json'

            con = sqlite3.connect(canonical)
            con.execute('''CREATE TABLE leads(domain TEXT PRIMARY KEY,name TEXT,country TEXT,region TEXT,city TEXT,state TEXT,street TEXT,website TEXT,public_email TEXT,public_phone TEXT,instagram TEXT,live_status TEXT,fit_tier TEXT,operator_score INTEGER,premium_score INTEGER,source_url TEXT,overture_id TEXT,first_seen TEXT,last_seen TEXT,source_release TEXT,raw_json TEXT)''')
            for i in range(20):
                domain=f'fanout-{i:02d}.example'
                con.execute('INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                    domain,f'Fanout Villas {i}','US','Florida','Miami','FL','',f'https://{domain}','','','',
                    'LIVE','A',90,80,'src',f'ov{i}','2026-08-16T00:00:00Z','2026-08-16T00:00:00Z','r',json.dumps({})
                ))
            con.commit(); con.close()
            cfg.write_text(json.dumps({
                'enabled': True,
                'canonical_safety_floor': 0,
                'max_workers': 10,
                'shard_size': 20,
                'max_accounts_per_pass': 200,
                'retry_hours': 24,
                'refresh_days': 30,
            }))

            subprocess.check_call([
                sys.executable, str(HERE/'hospitality_intelligence_plan.py'),
                '--canonical-db', str(canonical),
                '--intelligence-db', str(intel),
                '--config', str(cfg),
                '--outdir', str(out),
                '--force',
                '--max-accounts', '20',
                '--max-workers', '4',
            ], env={**os.environ, 'PYTHONPATH': str(HERE)})

            plan=json.loads((out/'plan.json').read_text())
            self.assertEqual(plan['accounts_planned'],20)
            self.assertEqual(plan['worker_count'],4)
            self.assertEqual([x['records'] for x in plan['include']],[5,5,5,5])
            self.assertEqual(plan['config']['explicit_workers'],4)


if __name__ == '__main__':
    unittest.main()
