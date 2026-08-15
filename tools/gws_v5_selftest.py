#!/usr/bin/env python3
import json
import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import gws_no_website_certifier_v53 as v53

v5 = v53.v5


def w(ok=True, unresolved=False, owned=''):
    if unresolved:
        dh=[{'seed':'https://acme.be/','final':'https://acme.be/','status':403,'ok':False}]
    else:
        dh=[{'seed':f'https://x{i}.be/','final':f'https://x{i}.be/','status':200,'ok':True} for i in range(5)]
    return {'healthy_providers':['google','bing'],'search_queries':2,'search_usable_queries':2,'direct_checked':5,'direct_health':dh,'owned':owned}


def main():
    # Certificate gates still require two healthy adversarial passes.
    c={'r':1,'n':'Acme Studio','p':'1050','a':'Rue Test 1','ph':'+32 2 555 12 12','em':'','cow':''}
    pe={'resolved':True,'phone_exact':True,'name_similarity':1.0,'address_overlap':1.0,'postcode_match':True,'overture_id':'ov-1'}
    cert=v5.certificate(c,pe,w(),w())
    assert cert['verified'] is True
    c2=dict(c); c2['cow']='https://acme.be/'
    cert2=v5.certificate(c2,pe,w(unresolved=True),w())
    assert cert2['verified'] is False and cert2['unresolved_plausible_domains']

    # Regression for the DuckDB parser bug: projection must use explicit AS alias.
    q=v53.overture_query('2026-06-17.0', limit=1)
    assert 'names.primary AS "name"' in q, q
    assert 'LIMIT 1' in q, q

    # A clean SERP with zero external domains is still usable evidence.
    assert v53._serp_parsed('google', '<html><body><div id="search">' + ('x'*1000) + '</div></body></html>')
    assert v53._serp_parsed('bing', '<html><body><ol id="b_results">' + ('x'*1000) + '</ol></body></html>')
    assert v53._serp_parsed('ddg', '<html><body><a class="result__a">' + ('x'*1000) + '</a></body></html>')

    # Authoritative DNS negatives must not be treated like transient network uncertainty.
    assert v53._dns_negative(socket.gaierror(-2, 'Name or service not known')) is True
    assert v53._dns_negative(TimeoutError('timeout')) is False

    # Environment pinning stays deterministic and validated.
    old=os.environ.get('OVERTURE_RELEASE')
    try:
        os.environ['OVERTURE_RELEASE']='2026-06-17.0'
        assert v53.resolve_overture_release()=='2026-06-17.0'
        os.environ['OVERTURE_RELEASE']='broken'
        try:
            v53.resolve_overture_release()
            raise AssertionError('invalid release was accepted')
        except RuntimeError as e:
            assert 'INVALID_OVERTURE_RELEASE' in str(e)
    finally:
        if old is None: os.environ.pop('OVERTURE_RELEASE',None)
        else: os.environ['OVERTURE_RELEASE']=old

    # Canonical negative evidence on any alias must disqualify the whole entity.
    high={'r':1,'candidate':c,'place':pe,'status':'HIGH','reason':'VERIFIED_NO_WEBSITE'}
    reject={'r':2,'candidate':dict(c,r=2,n='Acme Studio SRL'),'place':pe,'status':'REJECT','reason':'OWNED_SITE_SEARCH_CONFIRMED','owned_site':'https://acme.be/'}
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)/'in'; root.mkdir(); out=Path(td)/'out'
        (root/'results.jsonl').write_text(json.dumps(high)+'\n'+json.dumps(reject)+'\n')
        (root/'summary.json').write_text(json.dumps({'attempted':2}))
        v5.aggregate(SimpleNamespace(input_root=str(root),outdir=str(out),expected=2))
        s=json.loads((out/'summary.json').read_text())
        assert s['verified_no_website']==0, s
        assert s['canonical_duplicates']==1, s
    print('GWS_V53_SELFTEST_OK')


if __name__=='__main__':
    main()
