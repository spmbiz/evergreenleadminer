#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from gws_qwen_semantic import classify_batch


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--qwen-url',default='http://127.0.0.1:8080')
    a=ap.parse_args()

    owned_url='https://canarybarber.example/'
    directory_url='https://directory.example/canary-barber'
    records=[
        {
            'business_id':'canary:owned',
            'name':'Canary Barber Brussels',
            'address':'1 Rue du Test, Brussels',
            'postcode':'1000',
            'candidate_url':owned_url,
            'candidate_host_class':'UNKNOWN',
            'overture_name':'Canary Barber Brussels',
            'overture_resolved':True,
            'name_similarity':1.0,
            'address_overlap':1.0,
            'postcode_match':True,
            'phone_exact':True,
            'candidate_set':[
                {
                    'url':owned_url,'host':'canarybarber.example','host_class':'UNKNOWN','source':'serp_result','rank':1,'plausible':True,
                    'plausibility_hint':{'text_overlap':1.0,'phone_snippet':True},
                    'probe':{'ok':True,'status':200,'dns_negative':False,'matched':True,'match_mode':'strict_identity','final':owned_url},
                    'ownership_assessment':{'confident':True,'reason':'OWNERSHIP_CONFIRMED','third_party':False,'phone_exact':True,'address_overlap':1.0,'postcode_match':True,'branded_host':True},
                }
            ],
            'unresolved_plausible_domains':[], 'platform_only_signals':[],
        },
        {
            'business_id':'canary:directory',
            'name':'Canary Barber Brussels',
            'address':'1 Rue du Test, Brussels',
            'postcode':'1000',
            'candidate_url':directory_url,
            'candidate_host_class':'KNOWN_THIRD_PARTY',
            'overture_name':'Canary Barber Brussels',
            'overture_resolved':True,
            'name_similarity':1.0,
            'address_overlap':1.0,
            'postcode_match':True,
            'phone_exact':False,
            'candidate_set':[
                {
                    'url':directory_url,'host':'directory.example','host_class':'KNOWN_THIRD_PARTY','source':'serp_result','rank':1,'plausible':True,
                    'plausibility_hint':{'text_overlap':1.0,'phone_snippet':False},
                    'probe':{'ok':True,'status':200,'dns_negative':False,'matched':True,'match_mode':'legacy_identity','final':directory_url},
                    'ownership_assessment':{'confident':False,'reason':'KNOWN_THIRD_PARTY_HOST','third_party':True,'phone_exact':False,'address_overlap':1.0,'postcode_match':True,'branded_host':False},
                }
            ],
            'unresolved_plausible_domains':[], 'platform_only_signals':[directory_url],
        },
    ]
    out=classify_batch(records,a.qwen_url,'qwen3-4b-q4_k_m',timeout=45)
    print('GWS_QWEN_CONTRACT='+json.dumps(out,ensure_ascii=False,separators=(',',':')))
    assert len(out)==2, out
    by={x.get('business_id'):x for x in out}
    for bid in ('canary:owned','canary:directory'):
        assert bid in by, out
        assert not by[bid].get('_classifier_error'), by[bid]
    assert by['canary:owned'].get('decision') in {'MATCH','PROBABLE'}, by['canary:owned']
    assert by['canary:owned'].get('candidate_url')==owned_url, by['canary:owned']
    assert by['canary:directory'].get('decision') not in {'MATCH','PROBABLE'}, by['canary:directory']
    print('GWS_QWEN_CONTRACT_OK=1')


if __name__=='__main__':
    main()
