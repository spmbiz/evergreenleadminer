#!/usr/bin/env python3
"""Residential OpenSERP evidence worker for GWS strict verification.

Never writes canonical MASTER and never declares HIGH. It emits immutable,
certificate-compatible two-pass evidence from a self-hosted residential runner.
"""
from __future__ import annotations

import argparse, json, random, re, socket, time
import urllib.error, urllib.parse, urllib.request
from collections import Counter
from pathlib import Path

import gws_legacy_deep_v2 as v2
import gws_no_website_certifier_v5 as v5
import gws_no_website_certifier_v53 as prod
import gws_reference_mesh_v57 as ref

FAMILY={"google":"google","duckduckgo":"duckduckgo","yandex":"yandex","baidu":"baidu","bing":"bing","ecosia":"bing"}


def load_rows(path: Path):
    out=[]
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip(): continue
        x=json.loads(line); c=dict(x.get("candidate") or x); pe=dict(x.get("place") or {})
        c.setdefault("alias",pe.get("overture_name") or x.get("alias") or "")
        out.append({"r":int(c.get("r") or x.get("r")),"candidate":c,"place":pe})
    return out


def request_json(url,timeout=25):
    req=urllib.request.Request(url,headers={"User-Agent":"GWS-Home-OpenSERP/5.7","Accept":"application/json"}); z=time.time()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return {"ok":200<=int(r.status)<300,"status":int(r.status),"seconds":round(time.time()-z,2),"data":json.loads(r.read(4_000_000).decode(errors="ignore"))}
    except urllib.error.HTTPError as e:
        body=b""
        try: body=e.read(200_000)
        except Exception: pass
        return {"ok":False,"status":int(e.code),"seconds":round(time.time()-z,2),"error":"HTTPError","detail":body.decode(errors="ignore")[:300]}
    except Exception as e:
        return {"ok":False,"status":0,"seconds":round(time.time()-z,2),"error":type(e).__name__,"detail":str(e)[:300]}


def result_urls(data):
    out=[]; seen=set()
    for r in (data or {}).get("results") or []:
        u=str(r.get("url") or r.get("link") or "").strip(); h=v2.host(u)
        if not u.startswith("http") or not h or h in seen or v2.platform(u): continue
        seen.add(h); out.append({"url":u,"host":h,"title":str(r.get("title") or ""),"description":str(r.get("description") or r.get("snippet") or "")})
    return out


def plausible(c,item):
    text=" ".join((item.get("title") or "",item.get("description") or "",item["host"].replace("."," ")))
    nt=set(v2.toks(c.get("n"))); at=set(v2.toks(c.get("alias"))); ht=set(v2.toks(item["host"].replace("-"," ").replace("."," "))); bt=set(v2.toks(text))
    base=nt or at; dom=len((nt|at)&ht)/max(1,len(base)); txt=max(len(nt&bt)/max(1,len(nt)),len(at&bt)/max(1,len(at)) if at else 0)
    ph=re.sub(r"\D+","",str(c.get("ph") or "")); phit=bool(ph and len(ph)>=8 and ph[-8:] in re.sub(r"\D+","",text))
    return bool(dom>=.45 or txt>=.55 or phit),{"domain_overlap":round(dom,3),"text_overlap":round(txt,3),"phone_snippet":phit}


def direct_fetch(c,url):
    req=urllib.request.Request(url,headers={"User-Agent":v2.UA,"Accept-Language":"fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"})
    try:
        with urllib.request.urlopen(req,timeout=12) as r:
            body=r.read(v2.MAXBODY).decode(errors="ignore"); final=str(r.url); ide=prod.web_identity_hardened(c,body,final)
            return {"url":url,"final":final,"status":int(r.status),"ok":True,"identity":ide,"dns_negative":False}
    except urllib.error.HTTPError as e:
        return {"url":url,"final":url,"status":int(e.code),"ok":False,"dns_negative":False,"error":"HTTPError"}
    except urllib.error.URLError as e:
        reason=getattr(e,"reason",None); dns=isinstance(reason,socket.gaierror) or "name or service not known" in str(e).lower() or "getaddrinfo" in str(e).lower()
        return {"url":url,"final":url,"status":404 if dns else 0,"ok":False,"dns_negative":dns,"error":"" if dns else type(e).__name__,"error_detail":str(e)[:180]}
    except Exception as e:
        return {"url":url,"final":url,"status":getattr(e,"code",0) or 0,"ok":False,"dns_negative":False,"error":type(e).__name__,"error_detail":str(e)[:180]}


def probe_host(c,seed):
    variants=[]
    if seed.startswith('http') and not ref.is_reference(seed): variants.append(seed)
    for u in ref.direct_variants(seed):
        if u not in variants: variants.append(u)
    attempts=[]; best=None
    for u in variants:
        ev=direct_fetch(c,u); attempts.append(ev)
        if ev.get('ok') and (ev.get('identity') or {}).get('matched') and not v2.platform(ev.get('final') or u):
            return {"seed":seed,"final":ev.get('final') or u,"status":ev.get('status'),"ok":True,"identity":ev.get('identity') or {},"matched":True,"dns_negative":False,"attempts":attempts}
        if ev.get('ok'): best=ev
        if ev.get('dns_negative'): break
    if best:
        return {"seed":seed,"final":best.get('final') or seed,"status":best.get('status'),"ok":True,"identity":best.get('identity') or {},"matched":False,"dns_negative":False,"attempts":attempts}
    transient=next((x for x in attempts if x.get('error') and int(x.get('status') or 0) not in {404,410}),None)
    if transient:
        return {"seed":seed,"final":transient.get('final') or seed,"status":transient.get('status'),"ok":False,"error":transient.get('error'),"dns_negative":False,"attempts":attempts}
    last=attempts[-1] if attempts else {"status":0,"final":seed}
    return {"seed":seed,"final":last.get('final') or seed,"status":last.get('status'),"ok":False,"error":"","dns_negative":bool(last.get('dns_negative')),"attempts":attempts}


def engine_probe(endpoint,engine,query):
    params=urllib.parse.urlencode({"text":query,"limit":10,"lang":"FR","region":"BE"}); resp=request_json(f"{endpoint.rstrip('/')}/{engine}/search?{params}",30)
    data=resp.get("data") if resp.get("ok") else {}; rs=result_urls(data); parsed=bool(resp.get("ok") and isinstance((data or {}).get("results"),list))
    return {"provider":engine,"provider_family":FAMILY.get(engine,engine),"http_ok":bool(resp.get("ok")),"parsed":parsed,"status":resp.get("status"),"blocked":resp.get("status") in (202,403,429,503),"error":resp.get("error"),"seconds":resp.get("seconds"),"external_domains":len(rs),"results":rs}


def seeds_for_pass(c,pass_no):
    out=list(prod.guesses_hardened(c))
    if pass_no==2 and c.get("alias") and v2.n(c.get("alias"))!=v2.n(c.get("n")):
        cc=dict(c); cc["n"]=c.get("alias"); out.extend(prod.guesses_hardened(cc))
    seen=set(); ans=[]
    for u in out:
        h=v2.host(u)
        if h and h not in seen and not v2.platform(u): seen.add(h); ans.append(u)
        if len(ans)>=20: break
    return ans


def run_pass(c,endpoint,engines,pass_no,sleep_min,sleep_max):
    queries=list(v5.query_set(c,pass_no))[:5]; search_health=[]; healthy=set(); usable=0; resultful=0; serp_candidates=[]; seen=set()
    for q in queries:
        qh=[]; fam=set(); q_resultful=False
        for engine in engines:
            ev=engine_probe(endpoint,engine,q); qh.append({k:v for k,v in ev.items() if k!="results"})
            if ev["parsed"]:
                fam.add(ev["provider_family"]); healthy.add(ev["provider_family"])
                if ev.get('external_domains',0)>0: q_resultful=True
                for item in ev["results"]:
                    if item["host"] not in seen: seen.add(item["host"]); serp_candidates.append(item)
            time.sleep(random.uniform(sleep_min,sleep_max))
        if len(fam)>=2: usable+=1
        if len(fam)>=2 and q_resultful: resultful+=1
        search_health.append({"query":q,"providers":qh,"parsed_families":sorted(fam),"external_domains":len({x["host"] for x in serp_candidates}),"raw_resultful":q_resultful})
        if usable>=3 and resultful>=1: break
    direct_health=[]; owned=""; owned_identity={}; owned_via=""; checked=set()
    for u in seeds_for_pass(c,pass_no)[:12]:
        h=v2.host(u)
        if not h or h in checked: continue
        checked.add(h); de=probe_host(c,u); direct_health.append(de)
        if de.get('matched'):
            owned=de.get("final") or u; owned_identity=de.get("identity") or {}; owned_via="residential_direct_lattice_variants"; break
    if not owned:
        for item in serp_candidates:
            if item["host"] in checked: continue
            yes,why=plausible(c,item)
            if not yes: continue
            checked.add(item['host']); de=probe_host(c,item["url"]); de["serp_hint"]=why; direct_health.append(de)
            if de.get('matched'):
                owned=de.get("final") or item["url"]; owned_identity=de.get("identity") or {}; owned_via="residential_serp_variants"; break
            if len(direct_health)>=20: break
    return {"search_queries":len(search_health),"search_usable_queries":usable,"search_resultful_queries":resultful,"search_health":search_health,"search_candidates":[x["url"] for x in serp_candidates],"healthy_providers":sorted(healthy),"direct_checked":len(direct_health),"direct_health":direct_health,"owned":owned,"owned_identity":owned_identity,"owned_via":owned_via,"candidate_seeds":seeds_for_pass(c,pass_no),"residential_pass":pass_no}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--input",required=True); ap.add_argument("--outdir",required=True); ap.add_argument("--endpoint",default="http://127.0.0.1:7000"); ap.add_argument("--engines",default="google,duckduckgo,yandex,bing,ecosia"); ap.add_argument("--shard-index",type=int,default=0); ap.add_argument("--shard-count",type=int,default=1); ap.add_argument("--max-candidates",type=int,default=0); ap.add_argument("--sleep-min",type=float,default=.15); ap.add_argument("--sleep-max",type=float,default=.45); a=ap.parse_args()
    endpoint=a.endpoint.rstrip('/'); health=request_json(endpoint+"/health",8)
    if not health.get("ok"): raise SystemExit("OPENSERP_HOME_UNHEALTHY:"+json.dumps(health,separators=(",",":"),default=str))
    engines=[x.strip().lower() for x in a.engines.split(',') if x.strip()]; bad=[x for x in engines if x not in FAMILY]
    if bad: raise SystemExit("UNSUPPORTED_ENGINES:"+','.join(bad))
    all_rows=load_rows(Path(a.input)); rows=[x for i,x in enumerate(all_rows) if i%max(1,a.shard_count)==a.shard_index]
    if a.max_candidates>0: rows=rows[:a.max_candidates]
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True); z=time.time(); results=[]; counts=Counter(); fam=Counter()
    for i,row in enumerate(rows,1):
        c=row["candidate"]; pe=row["place"]
        if prod.obvious_non_independent_entity(c):
            results.append({"schema":"gws-home-openserp-observation-v3","r":row['r'],"candidate":c,"place":pe,"status":"REJECT","reason":"OUT_OF_SCOPE_NON_INDEPENDENT_PUBLIC_ENTITY","certificate_eligible":False}); counts['REJECT']+=1; continue
        p1=run_pass(c,endpoint,engines,1,a.sleep_min,a.sleep_max); p2=run_pass(c,endpoint,engines,2,a.sleep_min,a.sleep_max)
        for p in (p1,p2):
            for f in p["healthy_providers"]: fam[f]+=1
        owned=p1.get("owned") or p2.get("owned"); cert=prod.v5.certificate(c,pe,p1,p2)
        if owned: status,reason="REJECT","OWNED_SITE_RESIDENTIAL_CONFIRMED"
        elif cert.get("verified"): status,reason="EVIDENCE_COMPLETE","RESIDENTIAL_CERTIFICATE_GATES_COMPLETE"
        elif cert.get("unresolved_plausible_domains"): status,reason="EVIDENCE_INCOMPLETE","PLAUSIBLE_DOMAIN_UNRESOLVED"
        else: status,reason="EVIDENCE_INCOMPLETE","RESIDENTIAL_CERTIFICATE_GATES_INCOMPLETE"
        rec={"schema":"gws-home-openserp-observation-v3","r":row["r"],"candidate":c,"place":pe,"pass1":p1,"pass2":p2,"certificate":cert,"owned_site":owned or "","status":status,"reason":reason,"certificate_eligible":bool(cert.get("verified"))}
        results.append(rec); counts[status]+=1
        (d/"partial_results.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),default=str)+"\n" for x in results),encoding="utf-8")
        prog={"processed":i,"total":len(rows),"statuses":dict(counts),"elapsed_seconds":round(time.time()-z,2)}; (d/"progress.json").write_text(json.dumps(prog,indent=2)+"\n",encoding="utf-8"); print("GWS_HOME_PROGRESS="+json.dumps(prog,separators=(",",":")),flush=True)
    (d/"results.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),default=str)+"\n" for x in results),encoding="utf-8")
    summ={"schema":"gws-home-openserp-worker-v3","input_rows":len(all_rows),"attempted":len(rows),"shard_index":a.shard_index,"shard_count":a.shard_count,"engines":engines,"statuses":dict(counts),"family_pass_observations":dict(fam),"owned_sites_found":counts.get("REJECT",0),"certificate_eligible":counts.get("EVIDENCE_COMPLETE",0),"elapsed_seconds":round(time.time()-z,2),"final_high":0,"note":"Evidence only. Downstream single-writer must independently re-evaluate certificate before any HIGH persistence."}; (d/"summary.json").write_text(json.dumps(summ,indent=2)+"\n",encoding="utf-8"); print("GWS_HOME_SUMMARY="+json.dumps(summ,separators=(",",":")),flush=True)

if __name__=="__main__": main()
