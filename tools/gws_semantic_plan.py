#!/usr/bin/env python3
from __future__ import annotations

import argparse, hashlib, json, math, os
from pathlib import Path
from urllib.parse import urlparse

KNOWN_THIRD_PARTY_HOSTS=(
    'facebook.','instagram.','linkedin.','tiktok.','youtube.','planity.','treatwell.','salonkee.',
    'pagesdor.','goudengids.','goldenpages.','infobel.','idgarages.','yelp.','tripadvisor.','booking.com','foursquare.',
    'cybo.','worldplaces.','desocialekaart.','sociaal.brussels','fresha.','trustpilot.',
    'information-bruxelles.be','topcoiffeur.be','openingsurengids.be','localservices.be','beautynailhairsalons.com',
    'lorealprofessionnel.com','bizique.be','heures.be','hours.be','belgique.heures.info','atout-commerces.be','autonomia.org',
    'groupon.','vlan.be','hotfrogbe.be','cleanall.be'
)
PROFILE_PATH_MARKERS=('/author/','/authors/','/profile/','/profiles/','/listing/','/listings/','/directory/','/business-directory/')


def load(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:return default
def dump(path,obj):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8')
def iter_jsonl(pattern):
    for p in sorted(Path('.').glob(pattern)):
        try:
            for line in p.read_text(encoding='utf-8').splitlines():
                if line.strip(): yield json.loads(line)
        except Exception: continue
def host(u):
    try:return (urlparse(str(u or '')).hostname or '').lower().removeprefix('www.')
    except Exception:return ''
def candidate_host_class(u):
    u=str(u or '').strip(); h=host(u)
    try:path=(urlparse(u).path or '').lower()
    except Exception:path=''
    if any(x in h for x in KNOWN_THIRD_PARTY_HOSTS): return 'KNOWN_THIRD_PARTY'
    if any(x in path for x in PROFILE_PATH_MARKERS): return 'EDITORIAL_OR_PROFILE_PAGE'
    return 'UNKNOWN'
def candidate_urls(row):
    out=[]
    def add(u):
        u=str(u or '').strip()
        if u.startswith('http') and u not in out: out.append(u)
    add(row.get('owned_website'))
    for pass_name in ('web_pass1','web_pass2'):
        w=row.get(pass_name) or {}; add(w.get('owned'))
        for u in w.get('search_candidates') or []: add(u)
        for d in w.get('direct_health') or []:
            if (d.get('identity') or {}).get('matched'): add(d.get('final') or d.get('seed'))
    cert=row.get('certificate') or {}
    for d in cert.get('unresolved_plausible_domains') or []:
        h=str(d.get('host') or '').strip()
        if h:add('https://'+h+'/')
    return out[:12]
def semantic_fp(row,model,prompt):
    payload={'source_fingerprint':row.get('fingerprint'),'certificate_digest':row.get('certificate_digest'),'outcome':row.get('outcome'),'reason':row.get('reason'),'verification_status':row.get('verification_status'),'owned_website':row.get('owned_website'),'urls':candidate_urls(row),'model':model,'prompt':prompt}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
def compact(row,model,prompt):
    urls=candidate_urls(row); strict_high=row.get('outcome')=='HIGH' and row.get('reason')=='VERIFIED_NO_WEBSITE'
    candidate='' if strict_high else (urls[0] if urls else '')
    direct=[]; platforms=[]
    for pass_name in ('web_pass1','web_pass2'):
        w=row.get(pass_name) or {}
        for d in (w.get('direct_health') or [])[:10]:
            ide=d.get('identity') or {}; direct.append({'url':d.get('final') or d.get('seed'),'status':d.get('status'),'matched':bool(ide.get('matched')),'match_mode':ide.get('match_mode'),'dns_negative':bool(d.get('dns_negative'))})
        for u in w.get('search_candidates') or []:
            if candidate_host_class(u)!='UNKNOWN': platforms.append(u)
    assessments=[]
    for key in ('ownership_assessment_pass1','ownership_assessment_pass2','aggregate_ownership_assessment'):
        value=row.get(key)
        if isinstance(value,dict) and value:
            assessments.append({k:value.get(k) for k in ('url','confident','reason','third_party','brand_relation','phone_exact','address_overlap') if k in value})
    cert=row.get('certificate') or {}
    rec={'business_id':str(row.get('record_key') or ''),'name':row.get('hub_name') or '','address':row.get('hub_address') or '','postcode':row.get('hub_postalcode') or '','public_phone_present':bool(row.get('hub_phone') or row.get('overture_phone')),'candidate_url':candidate,'candidate_host':host(candidate),'candidate_host_class':candidate_host_class(candidate),'overture_name':row.get('overture_name') or '','overture_resolved':bool(row.get('overture_resolved') or row.get('overture_id')),'name_similarity':row.get('name_similarity') or 0,'address_overlap':row.get('address_overlap') or 0,'postcode_match':bool(row.get('postcode_match')),'phone_exact':bool(row.get('phone_exact')),'search_candidates':urls[:8],'direct_identity_evidence':direct[:12],'ownership_assessments':assessments[:3],'unresolved_plausible_domains':cert.get('unresolved_plausible_domains') or [],'platform_only_signals':platforms[:8],'semantic_fingerprint':semantic_fp(row,model,prompt),'source_outcome':row.get('outcome'),'source_reason':row.get('reason'),'source_verification_status':row.get('verification_status'),'source_verification_provider':row.get('verification_provider'),'territory':row.get('territory') or '','source_fingerprint':row.get('fingerprint') or '','certificate_digest':row.get('certificate_digest') or '','source':row}
    if row.get('outcome')=='REJECT' and row.get('owned_website'):
        rec['benchmark_kind']='OWNED_SITE_POSITIVE'; rec['benchmark_expected']='OWNED_MATCH'
    elif strict_high:
        rec['benchmark_kind']='STRICT_NO_SITE'; rec['benchmark_expected']='NO_FALSE_OWNED'
    return rec
def gather(cfg):
    rows={}
    for pattern in ('data/gws/verification/*.jsonl','gpt/gws_verified_review/*.jsonl'):
        for r in iter_jsonl(pattern):
            k=str(r.get('record_key') or '')
            if k: rows[k]=r
    q=cfg['qwen']; model=q['model_label']; prompt=q['prompt_version']; idx=load('state/gws_semantic_index.json',{'records':{}}).get('records',{})
    live=[]; bench=[]; live_out=set(cfg['selection'].get('live_outcomes') or []); live_st=set(cfg['selection'].get('live_statuses') or []); south={x:i for i,x in enumerate(cfg['selection'].get('south_priority') or [])}
    for r in rows.values():
        rec=compact(r,model,prompt); k=rec['business_id']; prior=idx.get(k) or {}
        if prior.get('semantic_fingerprint')==rec['semantic_fingerprint'] and prior.get('prompt_version')==prompt and prior.get('model')==model: continue
        outcome=str(r.get('outcome') or ''); status=str(r.get('verification_status') or '')
        if outcome in live_out or status in live_st: live.append(rec)
        if rec.get('benchmark_expected'): bench.append(rec)
    live.sort(key=lambda x:(south.get(str(x.get('territory')),99),0 if x.get('candidate_url') else 1,x['business_id']))
    bench.sort(key=lambda x:x['business_id'])
    return live,bench
def balanced_benchmark(bench,n):
    owned=[x for x in bench if x.get('benchmark_kind')=='OWNED_SITE_POSITIVE']
    no_site=[x for x in bench if x.get('benchmark_kind')=='STRICT_NO_SITE']
    half=max(1,n//2); selected=owned[:half]+no_site[:half]
    seen={x['business_id'] for x in selected}
    if len(selected)<n:
        selected.extend(x for x in owned[half:]+no_site[half:] if x['business_id'] not in seen and not seen.add(x['business_id']))
    return selected[:n],len(owned),len(no_site)
def prepare(args):
    cfg=load(args.config,{})
    if not cfg.get('enabled',True): dump(Path(args.outdir)/'plan.json',{'enabled':False}); return
    rollout=load('state/gws_semantic_rollout.json',{}); stage=str(rollout.get('stage') or 'smoke'); live,bench=gather(cfg); rcfg=cfg['rollout']; owned_total=sum(x.get('benchmark_kind')=='OWNED_SITE_POSITIVE' for x in bench); no_site_total=sum(x.get('benchmark_kind')=='STRICT_NO_SITE' for x in bench)
    if stage=='smoke':
        n=int(rcfg.get('smoke_records') or 32); bsel,_,_=balanced_benchmark(bench,max(2,n//4)); selected=(live[:max(1,n-len(bsel))]+bsel)[:n]; desired=1; stage_shard_size=n
    elif stage in {'benchmark','benchmark_waiting'}:
        n=int(rcfg.get('benchmark_records') or 250); stage_shard_size=max(1,int(rcfg.get('benchmark_shard_size') or 20))
        bsel,_,_=balanced_benchmark(bench,n)
        selected=(bsel+live[:max(0,n-len(bsel))])[:n]
        desired=min(int(rcfg.get('benchmark_workers') or 2),max(1,math.ceil(len(selected)/stage_shard_size))) if selected else 0; stage='benchmark'
    else:
        stage_shard_size=max(1,int(rcfg.get('production_shard_size') or 80)); maxw=int(rcfg.get('production_max_workers') or 10); selected=live[:maxw*stage_shard_size]; desired=min(maxw,max(1,math.ceil(len(selected)/stage_shard_size))) if selected else 0; stage='production'
    out=Path(args.outdir);out.mkdir(parents=True,exist_ok=True)
    with (out/'selected.jsonl').open('w',encoding='utf-8') as f:
        for r in selected:f.write(json.dumps(r,ensure_ascii=False,default=str)+'\n')
    selected_benchmark=sum(bool(x.get('benchmark_expected')) for x in selected); selected_live=len(selected)-selected_benchmark
    plan={'enabled':bool(selected),'stage':stage,'eligible_live':len(live),'eligible_benchmark':len(bench),'benchmark_owned_site':owned_total,'benchmark_strict_no_site':no_site_total,'selected':len(selected),'selected_live':selected_live,'selected_benchmark':selected_benchmark,'desired_workers':desired,'stage_shard_size':stage_shard_size,'model':cfg['qwen']['model_label'],'prompt_version':cfg['qwen']['prompt_version']}
    dump(out/'plan.json',plan);print('GWS_SEMANTIC_PLAN='+json.dumps(plan,separators=(',',':')))
    gh=os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh,'a',encoding='utf-8') as f:
            for k in ('stage','selected','desired_workers'):f.write(f'{k}={plan[k]}\n')
def finalize(args):
    out=Path(args.outdir); plan=load(out/'plan.json',{}); rows=[json.loads(x) for x in (out/'selected.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()] if (out/'selected.jsonl').exists() else []
    workers=min(max(0,int(args.capacity)),int(plan.get('desired_workers') or 0),len(rows)) if rows else 0; include=[]
    if workers:
        rows=rows[:workers*max(1,int(plan.get('stage_shard_size') or len(rows)))]
        shards=[[] for _ in range(workers)]
        for i,r in enumerate(rows): shards[i%workers].append(r)
        for i,s in enumerate(shards):
            p=out/f'shard-{i:02d}.jsonl'
            with p.open('w',encoding='utf-8') as f:
                for r in s:f.write(json.dumps(r,ensure_ascii=False,default=str)+'\n')
            include.append({'worker_index':i,'worker_count':workers,'path':p.name,'records':len(s)})
    plan.update({'selected_after_capacity':sum(x['records'] for x in include),'allocated_workers':workers,'include':include,'matrix':{'include':include}});dump(out/'plan.json',plan)
    gh=os.environ.get('GITHUB_OUTPUT')
    if gh:
        with open(gh,'a',encoding='utf-8') as f:f.write('workers='+str(workers)+'\n');f.write('matrix='+json.dumps({'include':include},separators=(',',':'))+'\n')
    print('GWS_SEMANTIC_FINAL='+json.dumps({'stage':plan.get('stage'),'selected':plan.get('selected_after_capacity',0),'workers':workers},separators=(',',':')))
def main():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(dest='cmd',required=True)
    p=sp.add_parser('prepare');p.add_argument('--config',default='config/gws_semantic_v1.json');p.add_argument('--outdir',required=True)
    p=sp.add_parser('finalize');p.add_argument('--outdir',required=True);p.add_argument('--capacity',type=int,required=True)
    a=ap.parse_args(); prepare(a) if a.cmd=='prepare' else finalize(a)
if __name__=='__main__':main()
