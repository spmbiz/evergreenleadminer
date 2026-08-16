#!/usr/bin/env python3
from __future__ import annotations
import argparse,gzip,json,subprocess,sys,time
from pathlib import Path
import fleet_runtime as fr
ROOT=Path(__file__).resolve().parents[1]

def run(cmd):
 p=subprocess.run(cmd,cwd=ROOT,text=True)
 if p.returncode: raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")

def count_snapshot(path):
 p=Path(path); n=0
 if not p.exists(): return 0
 op=gzip.open if p.suffix=='.gz' else open
 with op(p,'rt',encoding='utf-8') as f:
  for line in f:
   if line.strip(): n+=1
 return n

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--provider',default='github'); ap.add_argument('--cycle-id',required=True); ap.add_argument('--canonical-domains',required=True); ap.add_argument('--local-workers',type=int,default=32); ap.add_argument('--contact-workers',type=int,default=24); ap.add_argument('--outdir',required=True); a=ap.parse_args()
 root=Path(a.outdir); source=root/'source'; recovery=root/'recovery'; source.mkdir(parents=True,exist_ok=True); recovery.mkdir(parents=True,exist_ok=True); t0=time.time(); status='success'; error=''; snap=count_snapshot(a.canonical_domains)
 try:
  if snap<10000: raise RuntimeError(f'canonical snapshot too small: {snap}')
  run([sys.executable,'tools/hospitality_datatourisme_source.py','--canonical-domains',a.canonical_domains,'--outdir',str(source)])
  import shutil; shutil.copy2(source/'v6_recovery_candidates.csv',recovery/'v6_recovery_candidates.csv')
  run([sys.executable,'tools/v6_public_contact_enrich.py','--input',str(recovery/'v6_recovery_candidates.csv'),'--outdir',str(recovery),'--workers',str(a.contact_workers),'--timeout','8','--max-pages','3','--max-bytes','700000'])
  run([sys.executable,'tools/promote_contact_ready.py','--input',str(recovery/'v6_recovery_enriched.csv'),'--output',str(recovery/'v6_fast_ready.csv'),'--summary',str(recovery/'v6_contact_ready_summary.json')])
  run([sys.executable,'tools/v6_live_verify.py','--input',str(recovery/'v6_fast_ready.csv'),'--outdir',str(recovery),'--workers',str(a.local_workers),'--timeout','8'])
 except Exception as exc: status='failed_retryable'; error=f'{type(exc).__name__}: {exc}'
 src=fr.load_json(source/'datatourisme_source_summary.json',{}); rec=fr.load_json(recovery/'v6_contact_recovery_summary.json',{}); ready=fr.load_json(recovery/'v6_contact_ready_summary.json',{}); live=fr.load_json(recovery/'v6_live_summary.json',{})
 summary={'provider':a.provider,'cycle_id':a.cycle_id,'lane':'datatourisme_hospitality','task_type':'datatourisme_hospitality','shard':{'name':'DATATOURISME::FR','country':'France','region':'DATATOURISME::FR','bbox':'datatourisme:fr','release':time.strftime('%Y-%m-%d',time.gmtime()),'lane':'datatourisme_hospitality'},'status':status,'error':error,'canonical_snapshot_domains':snap,'elapsed_seconds':round(time.time()-t0,2),'raw_site_email_rows':int(src.get('raw_rows_scanned') or 0),'canonical_prefilter_rejected':int(src.get('canonical_known_rejected_early') or 0),'fresh_candidate_domains':int(src.get('canonical_unseen_candidate_domains') or 0),'recovery_candidates':int(src.get('canonical_unseen_candidate_domains') or 0),'recovered_public_emails':int(rec.get('recovered_public_emails') or 0),'contact_ready':int(ready.get('contact_ready') or 0),'live_ready':int(live.get('live_ready') or 0),'instagram_found':int(rec.get('instagram_found') or live.get('instagram_found') or 0),'facebook_found':int(rec.get('facebook_found') or 0),'worker_errors':0 if status=='success' else 1}
 fr.write_json(root/'worker_summary.json',summary); print(json.dumps(summary,indent=2))
 if status!='success': raise SystemExit(2)
if __name__=='__main__': main()
