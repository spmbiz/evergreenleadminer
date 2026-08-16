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


def make_canonical(path: Path, count: int) -> None:
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE leads(domain TEXT PRIMARY KEY,name TEXT,country TEXT,region TEXT,city TEXT,state TEXT,street TEXT,website TEXT,public_email TEXT,public_phone TEXT,instagram TEXT,live_status TEXT,fit_tier TEXT,operator_score INTEGER,premium_score INTEGER,source_url TEXT,overture_id TEXT,first_seen TEXT,last_seen TEXT,source_release TEXT,raw_json TEXT)''')
    for i in range(count):
        domain=f'fanout-{i:03d}.example'
        con.execute('INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
            domain,f'Fanout Villas {i}','US','Florida','Miami','FL','',f'https://{domain}','','','',
            'LIVE','A',90,80,'src',f'ov{i}','2026-08-16T00:00:00Z','2026-08-16T00:00:00Z','r',json.dumps({})
        ))
    con.commit(); con.close()


class HospitalityPlanFanoutTests(unittest.TestCase):
    def test_explicit_20x4_produces_four_five_row_shards(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            canonical = td / 'canonical.sqlite'
            intel = td / 'intel.sqlite'
            out = td / 'plan'
            cfg = td / 'cfg.json'
            make_canonical(canonical, 20)
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

    def test_one_allocated_worker_caps_requested_40_to_20(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            canonical = td / 'canonical.sqlite'
            intel = td / 'intel.sqlite'
            out = td / 'plan'
            cfg = td / 'cfg.json'
            make_canonical(canonical, 40)
            # This mirrors an effective broker config after only one slot was
            # allocated even if the upstream trigger originally requested 12.
            cfg.write_text(json.dumps({
                'enabled': True,
                'canonical_safety_floor': 0,
                'max_workers': 1,
                'shard_size': 20,
                'max_accounts_per_pass': 400,
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
                '--max-accounts', '40',
                '--max-workers', '1',
            ], env={**os.environ, 'PYTHONPATH': str(HERE)})

            plan=json.loads((out/'plan.json').read_text())
            self.assertEqual(plan['accounts_planned'],20)
            self.assertEqual(plan['worker_count'],1)
            self.assertEqual([x['records'] for x in plan['include']],[20])
            self.assertEqual(plan['config']['capacity_account_cap'],20)
            self.assertEqual(plan['config']['effective_max_accounts'],20)
            self.assertEqual(plan['config']['requested_max_accounts'],40)


if __name__ == '__main__':
    unittest.main()
