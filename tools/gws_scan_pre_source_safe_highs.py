#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import gws_ownership_gate as ownership


def urls(value: Any) -> list[str]:
    if isinstance(value, list): vals=value
    elif isinstance(value, str):
        s=value.strip()
        if not s: return []
        try:
            x=json.loads(s); vals=x if isinstance(x,list) else [x]
        except Exception: vals=[s]
    elif value: vals=[value]
    else: return []
    out=[]
    for v in vals:
        u=str(v or '').strip()
        if u and u not in out: out.append(u)
    return out


def receipt_pairs() -> set[tuple[str,str]]:
    out=set()
    for p in Path('state/gws_canonical_sheet_receipts').glob('*.json'):
        try: d=json.loads(p.read_text(encoding='utf-8'))
        except Exception: continue
        for r in d.get('canonicalized') or []:
            out.add((str(r.get('record_key') or ''),str(r.get('certificate_digest') or '')))
        for r in d.get('withheld_from_canonical') or []:
            out.add((str(r.get('record_key') or ''),str(r.get('certificate_digest') or '')))
    return out


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--outdir',default='results/gws_pre_source_safe_scan'); a=ap.parse_args()
    accounted=receipt_pairs(); latest={}; high_rows=0
    for p in sorted(Path('gpt/gws_verified_review').glob('*.jsonl')):
        try: lines=p.read_text(encoding='utf-8').splitlines()
        except Exception: continue
        for line in lines:
            if not line.strip(): continue
            try: r=json.loads(line)
            except Exception: continue
            if str(r.get('verification_status') or '').upper()!='HIGH' or str(r.get('reason') or '').upper()!='VERIFIED_NO_WEBSITE': continue
            high_rows+=1
            key=str(r.get('record_key') or r.get('hub_objectid') or '')
            dig=str(r.get('certificate_digest') or (r.get('certificate') or {}).get('evidence_digest') or '')
            ident=(key,dig)
            latest[ident]=(str(p),r)
    suspects=[]
    for (key,dig),(batch,r) in latest.items():
        candidate=[]
        for field in ('overture_websites','overture_website','source_website'):
            for u in urls(r.get(field)):
                candidate.append({'url':u,'field':field,'third_party':ownership.is_third_party(u)})
        hist=r.get('historical_reject_evidence') or {}
        for u in urls(hist.get('owned_website')):
            candidate.append({'url':u,'field':'historical_reject_evidence.owned_website','third_party':ownership.is_third_party(u)})
        first_party_candidates=[x for x in candidate if not x['third_party']]
        if not first_party_candidates: continue
        suspects.append({
            'record_key':key,'certificate_digest':dig,'batch':batch,
            'business_name':r.get('hub_name') or r.get('overture_name') or '',
            'address':r.get('hub_address') or r.get('overture_address') or '',
            'territory':r.get('territory') or '',
            'verification_provider':r.get('verification_provider') or '',
            'source_fingerprint':r.get('fingerprint') or r.get('source_fingerprint') or '',
            'overture_id':r.get('overture_id') or '',
            'source_website_candidates':first_party_candidates,
            'canonical_receipt_accounted':(key,dig) in accounted,
        })
    suspects.sort(key=lambda x:(x['canonical_receipt_accounted'],x['batch'],x['business_name']))
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    (out/'suspects.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False,sort_keys=True)+'\n' for x in suspects),encoding='utf-8')
    report={
        'verified_review_files':len(list(Path('gpt/gws_verified_review').glob('*.jsonl'))),
        'high_rows_seen_including_repeated_snapshots':high_rows,
        'unique_high_certificates':len(latest),
        'highs_with_non_third_party_source_website_evidence':len(suspects),
        'already_accounted_by_canonical_receipt':sum(1 for x in suspects if x['canonical_receipt_accounted']),
        'unaccounted_source_website_highs':sum(1 for x in suspects if not x['canonical_receipt_accounted']),
    }
    (out/'report.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print('GWS_PRE_SOURCE_SAFE_HIGH_SCAN='+json.dumps(report,separators=(',',':')))
    return 0

if __name__=='__main__': raise SystemExit(main())
