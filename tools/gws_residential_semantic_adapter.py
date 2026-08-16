#!/usr/bin/env python3
from __future__ import annotations

import argparse, json
from pathlib import Path
from urllib.parse import urlparse

ADAPTER_VERSION='gws-residential-semantic-v2'


def host(u):
    try:return (urlparse(str(u or '')).hostname or '').lower().removeprefix('www.')
    except Exception:return ''
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',default='data/gws/residential_ingress');ap.add_argument('--out',required=True);a=ap.parse_args()
    latest={}
    for p in sorted(Path(a.root).glob('*.jsonl')) if Path(a.root).exists() else []:
        for line in p.read_text(encoding='utf-8').splitlines():
            if not line.strip():continue
            ev=json.loads(line); key=str(ev.get('record_key') or '')
            if key:latest[key]=ev
    rows=[]
    for key,ev in latest.items():
        status=str(ev.get('status') or ''); url=str(ev.get('owned_site') or '')
        direct=[]
        for d in ev.get('direct_evidence') or []:
            direct.append({'seed':d.get('url'),'final':d.get('url'),'status':200 if d.get('ok') else 0,'ok':bool(d.get('ok')),'identity':{'matched':bool(d.get('matched')),'match_mode':'residential_page_identity_presence_not_ownership','page_name_overlap':d.get('name_overlap'),'address_overlap':d.get('address_overlap'),'postcode_match':d.get('postcode_hit'),'phone_exact':d.get('phone_hit')}})
        if status=='OWNED_SITE_CONFIRMED' and url:
            outcome='MEDIUM'; verification_status='MEDIUM'; reason='RESIDENTIAL_SITE_CANDIDATE_REQUIRES_OWNERSHIP_RESOLUTION'
            candidates=[url]
        elif status=='SEARCH_INCOMPLETE':
            outcome='UNCERTAIN'; verification_status='ERROR_RETRYABLE'; reason='RESIDENTIAL_SEARCH_INCOMPLETE'; candidates=[]
        else:
            outcome='UNCERTAIN'; verification_status='UNCERTAIN'; reason='RESIDENTIAL_NO_OWNED_SITE_OBSERVED_NOT_PROOF'; candidates=[]
        rows.append({
            'record_key':key,'hub_name':ev.get('hub_name') or '','hub_address':ev.get('hub_address') or '',
            'hub_postalcode':ev.get('hub_postalcode') or '','fingerprint':ev.get('source_fingerprint') or '',
            'outcome':outcome,'verification_status':verification_status,'reason':reason,'owned_website':'',
            'verification_provider':'residential_openserp_shadow','residential_status':status,
            'residential_engines':ev.get('engines_responded') or [],
            'web_pass1':{'owned':'','search_candidates':candidates,'direct_health':direct,'healthy_providers':ev.get('engines_responded') or []},
            'web_pass2':{},'certificate':{'unresolved_plausible_domains':[{'host':host(url),'source':'residential_candidate'}] if url else []},
            'observed_at':ev.get('observed_at'),'residential_candidate_url':url,'residential_adapter_version':ADAPTER_VERSION,
        })
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)+'\n' for r in rows),encoding='utf-8')
    print('GWS_RESIDENTIAL_SEMANTIC_ADAPTER='+json.dumps({'version':ADAPTER_VERSION,'input_unique':len(latest),'output':len(rows),'owned_candidates':sum(bool(r.get('residential_candidate_url')) for r in rows)},separators=(',',':')))
if __name__=='__main__':main()
