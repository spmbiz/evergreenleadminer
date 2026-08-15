#!/usr/bin/env python3
"""Residential OpenSERP evidence worker for GWS strict no-website verification.

This worker never writes the canonical MASTER and never declares HIGH by itself.
It emits immutable two-pass evidence from a residential/self-hosted runner.
A downstream single-writer can combine this evidence with current identity and
other deterministic evidence.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import gws_legacy_deep_v2 as v2
import gws_no_website_certifier_v5 as v5
import gws_no_website_certifier_v53 as prod

FAMILY = {
    "google": "google",
    "duckduckgo": "duckduckgo",
    "yandex": "yandex",
    "baidu": "baidu",
    "bing": "bing",
    # Ecosia is intentionally NOT counted as an independent family from Bing.
    "ecosia": "bing",
}


def load_rows(path: Path):
    rows=[]
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip(): continue
        x=json.loads(line)
        if isinstance(x.get("candidate"),dict):
            c=dict(x["candidate"]); pe=dict(x.get("place") or {})
        else:
            c=dict(x); pe={k:x.get(k) for k in (
                "overture_id","overture_name","resolved","phone_exact","phone_corroborated",
                "name_similarity","address_overlap","postcode_match","operating_status"
            ) if k in x}
        c.setdefault("alias", pe.get("overture_name") or x.get("alias") or "")
        rows.append({"r":int(c.get("r") or x.get("r")),"candidate":c,"place":pe})
    return rows


def request_json(url: str, timeout: int = 25):
    req=urllib.request.Request(url,headers={"User-Agent":"GWS-Home-OpenSERP/5.5","Accept":"application/json"})
    started=time.time()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read(4_000_000)
            data=json.loads(raw.decode(errors="ignore"))
            return {"ok":200<=int(r.status)<300,"status":int(r.status),"seconds":round(time.time()-started,2),"data":data}
    except urllib.error.HTTPError as exc:
        body=b""
        try: body=exc.read(200_000)
        except Exception: pass
        detail=""
        try:
            detail=json.loads(body.decode(errors="ignore"))
        except Exception:
            detail=body.decode(errors="ignore")[:240]
        return {"ok":False,"status":int(exc.code),"seconds":round(time.time()-started,2),"error":"HTTPError","detail":detail}
    except Exception as exc:
        return {"ok":False,"status":0,"seconds":round(time.time()-started,2),"error":type(exc).__name__,"detail":str(exc)[:240]}


def result_urls(data):
    out=[]
    for r in (data or {}).get("results") or []:
        u=str(r.get("url") or r.get("link") or "").strip()
        if not u.startswith("http"):
            continue
        h=v2.host(u)
        if h and not v2.platform(u):
            out.append({"url":u,"host":h,"title":str(r.get("title") or ""),"description":str(r.get("description") or r.get("snippet") or ""),"engine":str(r.get("engine") or "")})
    seen=set(); ded=[]
    for x in out:
        if x["host"] in seen: continue
        seen.add(x["host"]); ded.append(x)
    return ded


def plausible(c, item):
    host=item["host"]
    text=" ".join((item.get("title") or "",item.get("description") or "",host.replace("."," ")))
    name_tokens=set(v2.toks(c.get("n")))
    alias_tokens=set(v2.toks(c.get("alias")))
    host_tokens=set(v2.toks(host.replace("-"," ").replace("."," ")))
    body_tokens=set(v2.toks(text))
    base=name_tokens or alias_tokens
    dom_ov=len((name_tokens|alias_tokens)&host_tokens)/max(1,len(base))
    text_ov=max(
        len(name_tokens&body_tokens)/max(1,len(name_tokens)),
        len(alias_tokens&body_tokens)/max(1,len(alias_tokens)) if alias_tokens else 0,
    )
    phone=re.sub(r"\D+","",str(c.get("ph") or ""))
    phone_hit=bool(phone and len(phone)>=8 and phone[-8:] in re.sub(r"\D+","",text))
    return bool(dom_ov>=0.45 or text_ov>=0.55 or phone_hit), {"domain_overlap":round(dom_ov,3),"text_overlap":round(text_ov,3),"phone_snippet":phone_hit}


def direct_fetch_identity(c, url):
    req=urllib.request.Request(url,headers={"User-Agent":v2.UA,"Accept-Language":"fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"})
    try:
        with urllib.request.urlopen(req,timeout=12) as r:
            body=r.read(v2.MAXBODY).decode(errors="ignore")
            ide=prod.web_identity_hardened(c,body,str(r.url))
            return {"ok":True,"status":int(r.status),"final":str(r.url),"identity":ide}
    except Exception as exc:
        return {"ok":False,"status":getattr(exc,"code",0) or 0,"final":url,"error":type(exc).__name__,"error_detail":str(exc)[:180]}


def engine_probe(endpoint, engine, query, region="BE"):
    params=urllib.parse.urlencode({"text":query,"limit":10,"lang":"FR","region":region})
    url=f"{endpoint.rstrip('/')}/{engine}/search?{params}"
    resp=request_json(url,timeout=30)
    data=resp.get("data") if resp.get("ok") else {}
    results=result_urls(data)
    meta=(data or {}).get("meta") or {}
    parsed=bool(resp.get("ok") and isinstance((data or {}).get("results"),list))
    return {
        "engine":engine,
        "family":FAMILY.get(engine,engine),
        "ok":bool(resp.get("ok")),
        "status":resp.get("status"),
        "parsed":parsed,
        "seconds":resp.get("seconds"),
        "error":resp.get("error") or (meta.get("engine_errors") if isinstance(meta,dict) else None),
        "result_count":len(results),
        "results":results,
    }


def run_pass(c, endpoint, engines, pass_no, sleep_min, sleep_max):
    queries=list(v5.query_set(c,pass_no))[:2]
    observed=[]; healthy=set(); usable_queries=0; candidates=[]; seen_hosts=set()
    for qi,q in enumerate(queries):
        qobs=[]; qfamilies=set()
        for engine in engines:
            ev=engine_probe(endpoint,engine,q)
            qobs.append(ev)
            if ev["parsed"]:
                qfamilies.add(ev["family"]); healthy.add(ev["family"])
                for item in ev["results"]:
                    if item["host"] not in seen_hosts:
                        seen_hosts.add(item["host"]); candidates.append(item)
            time.sleep(random.uniform(sleep_min,sleep_max))
        if len(qfamilies)>=2:
            usable_queries+=1
        observed.append({"query":q,"engines":qobs,"families":sorted(qfamilies)})
    return {
        "pass_no":pass_no,
        "queries":observed,
        "search_queries":len(queries),
        "search_usable_queries":usable_queries,
        "healthy_providers":sorted(healthy),
        "search_candidates":candidates,
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True)
    ap.add_argument("--outdir",required=True)
    ap.add_argument("--endpoint",default="http://127.0.0.1:7000")
    ap.add_argument("--engines",default="google,duckduckgo,yandex,bing,ecosia")
    ap.add_argument("--shard-index",type=int,default=0)
    ap.add_argument("--shard-count",type=int,default=1)
    ap.add_argument("--max-candidates",type=int,default=0)
    ap.add_argument("--sleep-min",type=float,default=0.15)
    ap.add_argument("--sleep-max",type=float,default=0.45)
    a=ap.parse_args()

    endpoint=a.endpoint.rstrip('/')
    health=request_json(endpoint+"/health",timeout=8)
    if not health.get("ok"):
        raise SystemExit("OPENSERP_HOME_UNHEALTHY:"+json.dumps(health,separators=(",",":"),default=str))

    engines=[x.strip().lower() for x in a.engines.split(",") if x.strip()]
    bad=[x for x in engines if x not in FAMILY]
    if bad: raise SystemExit("UNSUPPORTED_ENGINES:"+",".join(bad))

    all_rows=load_rows(Path(a.input))
    rows=[x for i,x in enumerate(all_rows) if i % max(1,a.shard_count)==a.shard_index]
    if a.max_candidates>0: rows=rows[:a.max_candidates]
    outdir=Path(a.outdir); outdir.mkdir(parents=True,exist_ok=True)
    started=time.time(); results=[]; counts=Counter(); family_health=Counter()

    for idx,row in enumerate(rows,1):
        c=row["candidate"]; pe=row["place"]
        p1=run_pass(c,endpoint,engines,1,a.sleep_min,a.sleep_max)
        p2=run_pass(c,endpoint,engines,2,a.sleep_min,a.sleep_max)
        owned=""; owned_ev={}; checked=0; seen=set()
        pool=p1["search_candidates"]+p2["search_candidates"]
        for item in pool:
            h=item["host"]
            if h in seen: continue
            seen.add(h)
            yes,why=plausible(c,item)
            if not yes: continue
            checked+=1
            de=direct_fetch_identity(c,item["url"])
            de["serp_hint"]=why
            if de.get("ok") and (de.get("identity") or {}).get("matched") and not v2.platform(de.get("final") or item["url"]):
                owned=de.get("final") or item["url"]; owned_ev=de; break
            if checked>=10: break

        for ps in (p1,p2):
            for fam in ps["healthy_providers"]: family_health[fam]+=1
        c1_ok=len(set(p1["healthy_providers"]))>=2 and p1["search_queries"]>=2 and p1["search_usable_queries"]>=2
        c2_ok=len(set(p2["healthy_providers"]))>=2 and p2["search_queries"]>=2 and p2["search_usable_queries"]>=2
        status="REJECT" if owned else ("EVIDENCE_COMPLETE" if c1_ok and c2_ok else "EVIDENCE_INCOMPLETE")
        reason="OWNED_SITE_RESIDENTIAL_SERP_CONFIRMED" if owned else ("RESIDENTIAL_TWO_PASS_COMPLETE" if status=="EVIDENCE_COMPLETE" else "RESIDENTIAL_SEARCH_COVERAGE_INSUFFICIENT")
        rec={
            "schema":"gws-home-openserp-observation-v1","r":row["r"],"candidate":c,"place":pe,
            "pass1":p1,"pass2":p2,"direct_candidates_checked":checked,"owned_site":owned,
            "owned_identity":owned_ev,"status":status,"reason":reason,
            "certificate_eligible":bool((not owned) and c1_ok and c2_ok),
        }
        results.append(rec); counts[status]+=1
        (outdir/"partial_results.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),default=str)+"\n" for x in results),encoding="utf-8")
        progress={"processed":idx,"total":len(rows),"statuses":dict(counts),"elapsed_seconds":round(time.time()-started,2)}
        (outdir/"progress.json").write_text(json.dumps(progress,indent=2)+"\n",encoding="utf-8")
        print("GWS_HOME_PROGRESS="+json.dumps(progress,separators=(",",":")),flush=True)

    (outdir/"results.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),default=str)+"\n" for x in results),encoding="utf-8")
    summ={
        "schema":"gws-home-openserp-worker-v1","input_rows":len(all_rows),"attempted":len(rows),
        "shard_index":a.shard_index,"shard_count":a.shard_count,"engines":engines,
        "statuses":dict(counts),"family_pass_observations":dict(family_health),
        "owned_sites_found":counts.get("REJECT",0),"certificate_eligible":counts.get("EVIDENCE_COMPLETE",0),
        "elapsed_seconds":round(time.time()-started,2),"final_high":0,
        "note":"Residential worker produces evidence only; canonical HIGH requires downstream deterministic merge and readback."
    }
    (outdir/"summary.json").write_text(json.dumps(summ,indent=2)+"\n",encoding="utf-8")
    print("GWS_HOME_SUMMARY="+json.dumps(summ,separators=(",",":")),flush=True)

if __name__=="__main__": main()
