#!/usr/bin/env python3
"""Read-only commercial-fit audit for the durable Hospitality canonical SQLite."""
from __future__ import annotations
import argparse,csv,json,sqlite3
from collections import Counter,defaultdict
from pathlib import Path
from hospitality_quality_v2 import QUALITY_VERSION,assess_record

def safe_json(raw):
    try:return json.loads(raw or '{}')
    except Exception:return {}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',required=True);ap.add_argument('--outdir',required=True);a=ap.parse_args()
    out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(a.db);con.row_factory=sqlite3.Row
    rows=con.execute('SELECT * FROM leads ORDER BY domain').fetchall()
    tier=Counter();decision=Counter();reasons=Counter();source=defaultdict(Counter);country=defaultdict(Counter)
    old_fit=Counter();old_live=Counter();score_pairs=Counter();suspects=[];overlay=[]
    for rr in rows:
        base=dict(rr);raw=safe_json(base.pop('raw_json',None));merged=dict(raw);merged.update({k:v for k,v in base.items() if v not in (None,'')})
        q=assess_record(merged);d=str(base.get('domain') or merged.get('domain') or '')
        t=q['commercial_fit_tier'];tier[t]+=1;decision[q['quality_decision']]+=1
        old_fit[str(base.get('fit_tier') or '')]+=1;old_live[str(base.get('live_status') or '')]+=1
        score_pairs[(int(base.get('operator_score') or 0),int(base.get('premium_score') or 0))]+=1
        sf=str(merged.get('source_family') or merged.get('source') or 'UNKNOWN')[:100];source[sf][t]+=1
        cc=str(base.get('country') or merged.get('country') or 'UNKNOWN');country[cc][t]+=1
        for reason in q['quality_reason'].split(';'):reasons[reason]+=1
        rec={'domain':d,'name':base.get('name') or merged.get('name') or '','country':cc,'source':sf,'old_fit_tier':base.get('fit_tier') or '','old_live_status':base.get('live_status') or '',**q}
        overlay.append(rec)
        if t in {'C','X'}:suspects.append(rec)
    con.close()
    def nested(dd):return {k:dict(v) for k,v in sorted(dd.items(),key=lambda x:(-sum(x[1].values()),x[0]))}
    top_pairs=[{'operator_score':k[0],'premium_score':k[1],'rows':v} for k,v in score_pairs.most_common(20)]
    summary={'quality_version':QUALITY_VERSION,'canonical_rows':len(rows),'proposed_tiers':dict(tier),'decisions':dict(decision),'sales_ready_rows':tier.get('A',0)+tier.get('B',0),'held_or_rejected_rows':tier.get('C',0)+tier.get('X',0),'hard_reject_rows':tier.get('X',0),'review_rows':tier.get('C',0),'existing_fit_tiers':dict(old_fit),'existing_live_statuses':dict(old_live),'top_quality_reasons':dict(reasons.most_common(40)),'top_legacy_score_pairs':top_pairs,'by_source':nested(source),'by_country':nested(country),'canonical_mutation':False}
    (out/'quality-audit-summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    fields=['domain','name','country','source','old_fit_tier','old_live_status','commercial_fit_tier','commercial_score','entity_validity_score','premium_score_v2','operator_score_v2','sales_ready','quality_decision','quality_reason','premium_signals','destination_signal_count','short_stay_signal_count','self_lodging_signal_count','sanitized_instagram','sanitized_facebook','invalid_social_count','quality_version']
    with (out/'quality-audit-suspects.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(suspects)
    with (out/'quality-overlay.jsonl').open('w',encoding='utf-8') as f:
        for r in overlay:f.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
    print(json.dumps(summary,indent=2,ensure_ascii=False))
if __name__=='__main__':main()
