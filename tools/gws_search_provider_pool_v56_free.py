#!/usr/bin/env python3
"""Zero-paid GWS web evidence pool.

Strict candidates use Bing + Yep as independent search-index families. Ghostery
adds discovery only. Empty/parsing-ambiguous SERPs are never silently promoted
into strict negative evidence, and strict candidates receive multiple diverse
query formulations before certification. Local 4get is auto-bootstrapped on
GitHub workers so the production verifier does not depend on hidden YAML setup.
"""
from __future__ import annotations

import asyncio, html, json, os, random, re, subprocess, time, urllib.parse, urllib.request
import gws_legacy_deep_v2 as v2
import gws_legacy_deep_v4 as v4

_NEGATIVE=("did not match any documents","no results","we did not find results","there are no results for","aucun résultat","geen resultaten")


def _explicit_negative(body):
    low=(body or "").lower(); return any(x in low for x in _NEGATIVE)


def _parsed_bing(body):
    low=(body or "").lower()
    if len(low)<800: return False
    return _explicit_negative(body) or "b_results" in low or "b_algo" in low


def _raw_external_hosts(body,base):
    out=set(); base_host=v2.host(base)
    for href in re.findall(r'''href\s*=\s*["']([^"'#]+)''',body or "",re.I):
        u=html.unescape(urllib.parse.urljoin(base,href.strip()))
        if "bing.com/ck/a" in u or "bing.com/aclick" in u: continue
        h=v2.host(u)
        if not h or h==base_host or any(x in h for x in ("bing.com","microsoft.com","msn.com")): continue
        out.add(h)
    return out


def _blocked(status,body):
    low=(body or "").lower()
    return status in (202,403,429,503) or any(x in low for x in ("unusual traffic","captcha","verify you are human","challenge-platform"))


def _dns_negative(exc):
    t=str(exc).lower(); n=type(exc).__name__.lower()
    return "dns" in n or "name or service not known" in t or "nodename nor servname" in t or "no address associated with hostname" in t or ("name resolution" in t and "temporary failure" not in t)


def provider_family(provider):
    p=str(provider)
    if p in {"bing","yahoo"}: return "bing"
    if p=="yep": return "yep"
    if p=="ghostery": return "discovery_only"
    return p


def provider_concurrency_plan(search_conc):
    c=max(1,int(search_conc)); return {"bing":c,"yep":c,"ghostery":c}


def _fourget_healthy(base):
    try:
        req=urllib.request.Request(base.rstrip('/')+"/ami4get",headers={"User-Agent":"GWS-4get-health/1.0"})
        with urllib.request.urlopen(req,timeout=2.5) as r:
            return 200<=int(r.status)<500
    except Exception:
        return False


def _ensure_fourget(base):
    """Start a private localhost 4get when needed, fail-closed otherwise."""
    if _fourget_healthy(base): return
    h=v2.host(base)
    local=h in {"127.0.0.1","localhost","::1"}
    auto=os.getenv("GWS_AUTO_START_FOURGET", "1" if os.getenv("GITHUB_ACTIONS") else "0").strip().lower() not in {"0","false","no"}
    if not local or not auto:
        raise RuntimeError(f"FOURGET_UNAVAILABLE:{base}")
    name="gws-fourget-v56"
    subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
    pull=subprocess.run(["docker","pull","luuul/4get:latest"],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    if pull.returncode!=0: raise RuntimeError("FOURGET_DOCKER_PULL_FAILED:"+(pull.stderr or "")[-300:])
    run=subprocess.run(["docker","run","-d","--name",name,"-p","127.0.0.1:8090:80","-e","FOURGET_SERVER_NAME=localhost","-e","FOURGET_PROTO=http","luuul/4get:latest"],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    if run.returncode!=0: raise RuntimeError("FOURGET_DOCKER_RUN_FAILED:"+(run.stderr or "")[-300:])
    deadline=time.time()+30
    while time.time()<deadline:
        if _fourget_healthy(base):
            print("GWS_V56_FOURGET_AUTOBOOT_OK",flush=True); return
        time.sleep(.75)
    raise RuntimeError("FOURGET_AUTOBOOT_HEALTH_TIMEOUT")


async def webcheck(rows,conc,search_conc):
    import aiohttp
    fourget=os.getenv("FOURGET_URL","http://127.0.0.1:8090").rstrip('/')
    _ensure_fourget(fourget)
    sem=asyncio.Semaphore(max(1,int(conc)))
    gates={k:asyncio.Semaphore(v) for k,v in provider_concurrency_plan(search_conc).items()}
    timeout=aiohttp.ClientTimeout(total=14,connect=4,sock_read=9)
    headers={"User-Agent":v2.UA,"Accept-Language":"fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"}
    ans={}
    async with aiohttp.ClientSession(timeout=timeout,headers=headers) as sess:
        async def get(url,is_search=False,provider="",attempts=1):
            last={}
            for attempt in range(max(1,int(attempts))):
                try:
                    gate=gates[provider] if is_search and provider in gates else sem
                    async with gate:
                        if is_search: await asyncio.sleep(random.uniform(.02,.10))
                        async with sess.get(url,allow_redirects=True,ssl=True) as r:
                            body=(await r.content.read(v2.MAXBODY)).decode(errors="ignore")
                            if is_search and _blocked(int(r.status),body): last={"ok":False,"status":int(r.status),"blocked":True,"url":str(r.url)}
                            else: return {"ok":True,"status":int(r.status),"url":str(r.url),"body":body}
                except Exception as exc:
                    if not is_search and _dns_negative(exc): return {"ok":False,"status":404,"dns_negative":True,"error":""}
                    last={"ok":False,"status":0,"error":type(exc).__name__,"error_detail":str(exc)[:160]}
                if attempt+1<max(1,int(attempts)): await asyncio.sleep(.12+random.uniform(.02,.08))
            return last or {"ok":False,"status":0,"error":"UNKNOWN"}

        async def bing_search(q):
            url="https://www.bing.com/search?count=10&q="+urllib.parse.quote_plus(q)
            r=await get(url,True,"bing",2); body=r.get("body",""); prelim=bool(r.get("ok") and 200<=int(r.get("status") or 999)<300 and _parsed_bing(body))
            links=v4.hrefs(body,r.get("url",url),24) if prelim else []; raw_hosts=_raw_external_hosts(body,r.get("url",url)) if prelim else set(); explicit_zero=bool(prelim and _explicit_negative(body))
            parsed=bool(prelim and (raw_hosts or links or explicit_zero))
            return parsed,links,{"provider":"bing","provider_family":"bing","http_ok":bool(r.get("ok")),"parsed":parsed,"status":r.get("status"),"blocked":bool(r.get("blocked")),"error":r.get("error") if parsed else (r.get("error") or "AMBIGUOUS_ZERO_DOMAIN_SERP"),"external_domains":len({v2.host(x) for x in links if v2.host(x)}),"raw_result_domains":len(raw_hosts),"explicit_negative":explicit_zero}

        async def fourget_search(scraper,q):
            url=fourget+"/api/v1/web?"+urllib.parse.urlencode({"s":q,"scraper":scraper})
            try:
                async with gates[scraper]:
                    async with sess.get(url,ssl=False) as r:
                        raw=await r.content.read(v2.MAXBODY); status=int(r.status)
                data=json.loads(raw.decode(errors="ignore")) if raw else {}; web=data.get("web"); parsed=bool(status==200 and data.get("status")=="ok" and isinstance(web,list))
                links=[]; seen=set(); raw_hosts=set()
                if parsed:
                    for item in web:
                        u=str((item or {}).get("url") or "").strip(); h=v2.host(u)
                        if u.startswith("http") and h:
                            raw_hosts.add(h)
                            if h not in seen and not v2.platform(u): seen.add(h); links.append(u)
                return parsed,links,{"provider":scraper,"provider_family":provider_family(scraper),"http_ok":status==200,"parsed":parsed,"status":status,"blocked":False,"error":"" if parsed else str(data.get("status") or "FOURGET_SCHEMA"),"external_domains":len({v2.host(x) for x in links if v2.host(x)}),"raw_result_domains":len(raw_hosts),"explicit_negative":bool(parsed and not web)}
            except Exception as exc:
                return False,[],{"provider":scraper,"provider_family":provider_family(scraper),"http_ok":False,"parsed":False,"status":0,"blocked":False,"error":type(exc).__name__,"external_domains":0,"raw_result_domains":0,"explicit_negative":False}

        async def search_one(q,strict):
            if strict:
                (bp,bl,bh),(yp,yl,yh),(gp,gl,gh)=await asyncio.gather(bing_search(q),fourget_search("yep",q),fourget_search("ghostery",q)); fam=set()
                if bp: fam.add("bing")
                if yp: fam.add("yep")
                return fam,list(bl)+list(yl)+list(gl),[bh,yh,gh]
            (bp,bl,bh),(gp,gl,gh)=await asyncio.gather(bing_search(q),fourget_search("ghostery",q)); fam={"bing"} if bp else set(); return fam,list(bl)+list(gl),[bh,gh]

        async def one(c):
            strict=bool(c.get("_strict_high_candidate")); ev={"search_queries":0,"search_usable_queries":0,"search_resultful_queries":0,"search_health":[],"search_candidates":[],"healthy_providers":[],"direct_checked":0,"direct_health":[],"owned":"","owned_identity":{},"owned_via":"","candidate_seeds":[],"strict_high_path":strict,"zero_paid_api":True}
            seeds=v4.guesses(c); ev["candidate_seeds"]=seeds[:]; seed_hosts={v2.host(x) for x in seeds if v2.host(x) and not v2.platform(x)}; queries=list(v4.search_queries(c))[:5 if strict else 3]
            for q in queries:
                ev["search_queries"]+=1; fam,links,health=await search_one(q,strict)
                for f in sorted(fam):
                    if f not in ev["healthy_providers"]: ev["healthy_providers"].append(f)
                usable=(len(fam)>=2 if strict else bool(fam))
                if usable: ev["search_usable_queries"]+=1
                raw_resultful=any(int(h.get("raw_result_domains") or 0)>0 for h in health)
                if usable and raw_resultful: ev["search_resultful_queries"]+=1
                ev["search_health"].append({"query":q,"providers":health,"parsed_families":sorted(fam),"external_domains":len({v2.host(x) for x in links if v2.host(x)}),"raw_resultful":raw_resultful})
                have={v2.host(x) for x in ev["search_candidates"] if v2.host(x)}
                for u in links:
                    h=v2.host(u)
                    if h and h not in have: ev["search_candidates"].append(u); have.add(h)
                if strict and ev["search_usable_queries"]>=3 and ev["search_resultful_queries"]>=1 and len(seed_hosts|have)>=5: break
                if not strict and ev["search_queries"]>=2 and len(seed_hosts|have)>=5: break
            seen=set(); cap=20 if strict else 12
            for u in seeds+ev["search_candidates"]:
                h=v2.host(u)
                if not h or h in seen or v2.platform(u): continue
                seen.add(h); r=await get(u,False,"",2); ev["direct_checked"]+=1
                dh={"seed":u,"final":r.get("url",u),"status":r.get("status"),"ok":bool(r.get("ok")),"error":r.get("error"),"dns_negative":bool(r.get("dns_negative"))}
                if r.get("ok") and not v4._dead(int(r.get("status") or 999),r.get("body","")):
                    ide=v4.identity(c,r.get("body",""),r.get("url",u)); dh["identity"]=ide
                    if ide.get("matched") and not v2.platform(r.get("url",u)):
                        ev["owned"]=r.get("url",u); ev["owned_identity"]=ide; ev["owned_via"]="direct_or_free_serp"; ev["direct_health"].append(dh); break
                ev["direct_health"].append(dh)
                if ev["direct_checked"]>=cap: break
            return int(c["r"]),ev
        ans.update(await asyncio.gather(*(one(c) for c in rows)))
    return ans
