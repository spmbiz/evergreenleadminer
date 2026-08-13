#!/usr/bin/env python3
from __future__ import annotations
import argparse,base64,csv,json,re,time,random,unicodedata
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote
import requests
from bs4 import BeautifulSoup

BLOCKED=(
 "facebook.com","instagram.com","tiktok.com","linkedin.com","youtube.com","x.com","twitter.com",
 "treatwell.","mytreatwell.","planity.com","fresha.com","salonkee.",
 "pagesdor.","goudengids.","bizique.","cylex-","opendi.","bolid.","garagebelgique.",
 "selfcity.","heures.be","openingsuren.","tripadvisor.","yelp.","google.","waze.",
 "ubereats.","deliveroo.","takeaway.","booking.com","pappers.","companyweb.","bottin.",
 "nosavis.","localguide.","beautynailhairsalons.","pinterest.","ivof.com",
 "patisseriebelgique.com","institutdebeautebelgique.com","coiffeurbelgique.com",
 "boulangeriebelgique.com","carrosseriebelgique.com",
)
UA="Mozilla/5.0 (compatible; GWS-Brussels-WebGate/1.1; +public-business-verification)"
NO_SITE_PHRASES=(
 "pas de site web","pas de site internet","aucun site web","site internet pas de site",
 "geen website","geen website beschikbaar","no website","website not available","website n a",
)

def norm(s):
    s=unicodedata.normalize("NFKD",str(s or "")).encode("ascii","ignore").decode().lower()
    s=re.sub(r"[^a-z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()
def toks(s): return {x for x in norm(s).split() if len(x)>=3 and x not in {"the","les","des","rue","avenue","boulevard","chaussee","brussels","bruxelles","belgium","belgique"}}
def digits(s): return re.sub(r"\D","",str(s or ""))
def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except:return ""
def blocked(h): return (not h) or any(x in h for x in BLOCKED)
def domain_name_score(h,name):
    stem=re.sub(r"[^a-z0-9]","",h.split(".")[0].lower())
    nts=[x for x in toks(name) if len(x)>=4]
    if not stem or not nts:return 0
    return max((1 if t in stem else 0) for t in nts)

def decode_bing_url(u):
    if "bing.com/ck/a" not in u:return u
    try:
        raw=parse_qs(urlparse(u).query).get("u",[""])[0]
        if raw.startswith("a1"):
            raw=raw[2:]
            raw += "="*((4-len(raw)%4)%4)
            dec=base64.urlsafe_b64decode(raw.encode()).decode("utf-8","ignore")
            if dec.startswith("http"):return dec
    except:pass
    return u

def bing(q,session):
    r=session.get("https://www.bing.com/search",params={"q":q,"count":8},headers={"User-Agent":UA},timeout=10)
    if r.status_code in (429,403): raise RuntimeError("BING_BLOCKED_"+str(r.status_code))
    r.raise_for_status()
    s=BeautifulSoup(r.text,"html.parser"); out=[]
    for a in s.select("li.b_algo h2 a"):
        u=decode_bing_url(a.get("href","")); t=a.get_text(" ",strip=True)
        if u.startswith("http"): out.append((u,t))
    return out[:8]

def ddg(q,session):
    r=session.get("https://html.duckduckgo.com/html/",params={"q":q},headers={"User-Agent":UA},timeout=10)
    if r.status_code in (429,403): raise RuntimeError("DDG_BLOCKED_"+str(r.status_code))
    r.raise_for_status()
    s=BeautifulSoup(r.text,"html.parser"); out=[]
    for a in s.select("a.result__a"):
        u=a.get("href",""); t=a.get_text(" ",strip=True)
        if "uddg=" in u:
            try:u=unquote(parse_qs(urlparse(u).query).get("uddg",[""])[0])
            except:pass
        if u.startswith("http"): out.append((u,t))
    return out[:8]

def page_identity(url,row,session):
    try:
        r=session.get(url,headers={"User-Agent":UA},timeout=9,allow_redirects=True)
        if r.status_code>=400:return False,False,"HTTP_"+str(r.status_code),r.url
        soup=BeautifulSoup(r.text[:700000],"html.parser")
        raw=soup.get_text(" ",strip=True)
        text=norm(raw)
        nt=toks(row["hub_name"])
        name_hits=sum(1 for t in nt if t in text)
        p=digits(row.get("overture_phone",""))
        phone_hit=bool(p and len(p)>=8 and p[-8:] in digits(raw))
        at=toks(row.get("hub_address",""))
        addr_hits=sum(1 for t in at if t in text)
        title=norm(soup.title.get_text(" ",strip=True) if soup.title else "")
        title_hits=sum(1 for t in nt if t in title)
        identity_ok=phone_hit or (name_hits>=max(1,min(2,len(nt))) and addr_hits>=1) or (title_hits>=1 and addr_hits>=2)
        explicit_no_site=identity_ok and any(norm(x) in text for x in NO_SITE_PHRASES)
        return identity_ok,explicit_no_site,f"name={name_hits};addr={addr_hits};phone={int(phone_hit)};title={title_hits}",r.url
    except Exception as e:return False,False,"FETCH_ERR:"+type(e).__name__,url

def title_relevant(title,name):
    a=toks(title);b=toks(name)
    return bool(a & b)

def one(row):
    s=requests.Session()
    name=row["hub_name"]; addr=row["hub_address"]; phone=row.get("overture_phone","")
    q1=f'"{name}" "{addr}"'
    q2=f'"{name}" "{phone}" Brussels' if phone else f'"{name}" Brussels "{row.get("hub_postalcode","")}"'
    engines=[]; links=[]; errors=[]
    # Two independent engines plus a second exact Bing challenge. Bounded and retry-free per query.
    for eng,fn,q in [("bing",bing,q1),("ddg",ddg,q1),("bing2",bing,q2)]:
        try:
            res=fn(q,s);engines.append(eng);links.extend((eng,u,t) for u,t in res)
        except Exception as e:errors.append(f"{eng}:{e}")
        time.sleep(0.05+random.random()*0.10)
    uniq=[];seen=set()
    for eng,u,t in links:
        h=host(u)
        key=(u,h)
        if key in seen:continue
        seen.add(key);uniq.append((eng,u,t,h))

    owned=[]; thirdparty=[]; explicit=[]
    # Fetch at most six plausible result pages to keep verification bounded.
    checked=0
    for eng,u,t,h in uniq:
        if checked>=6:break
        if not title_relevant(t,name) and domain_name_score(h,name)<1:continue
        checked+=1
        ok,no_site,why,final=page_identity(u,row,s)
        if not ok:continue
        fh=host(final) or h
        if blocked(fh):
            thirdparty.append((final,why))
            if no_site:explicit.append((final,why))
        else:
            # A non-directory page is counted as owned only after identity corroboration.
            owned.append((final,why))

    third_domains={host(u) for u,_ in thirdparty if host(u)}
    if owned:
        outcome="REJECT";reason="OWNED_SITE_FOUND"
    elif explicit:
        outcome="HIGH";reason="VERIFIED_NO_WEBSITE_EXPLICIT_DIRECTORY_EVIDENCE"
    elif phone and len(third_domains)>=2 and len(engines)>=1:
        outcome="HIGH";reason="VERIFIED_NO_WEBSITE_MULTI_SOURCE"
    elif phone and len(third_domains)>=1 and ("bing" in engines or "bing2" in engines) and "ddg" in engines:
        outcome="HIGH";reason="VERIFIED_NO_WEBSITE_OVERTURE_PLUS_INDEPENDENT_SEARCH"
    elif len(engines)>=1:
        outcome="MEDIUM";reason="NO_OWNED_SITE_FOUND_BUT_CORROBORATION_BELOW_HIGH_GATE"
    elif errors:
        outcome="ERROR_RETRYABLE";reason="SEARCH_ENGINE_BLOCKER"
    else:
        outcome="UNCERTAIN";reason="INSUFFICIENT_EVIDENCE"
    out=dict(row)
    out.update({
      "outcome":outcome,"reason_code":reason,
      "search_engines_ok":"|".join(engines),"search_errors":"|".join(errors),
      "owned_site_found":owned[0][0] if owned else "",
      "owned_site_evidence":owned[0][1] if owned else "",
      "third_party_domains":"|".join(sorted(third_domains)),
      "explicit_no_site_url":explicit[0][0] if explicit else "",
      "corroborating_urls":" | ".join(u for u,_ in thirdparty[:8]),
      "search_urls":" | ".join(u for _,u,_,_ in uniq[:15]),
    })
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--input",required=True);ap.add_argument("--outdir",required=True);ap.add_argument("--workers",type=int,default=6)
    a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader(open(a.input,encoding="utf-8")))
    done=[]
    with ThreadPoolExecutor(max_workers=max(1,min(a.workers,6))) as ex:
        futs={ex.submit(one,r):r for r in rows}
        for i,f in enumerate(as_completed(futs),1):
            try:done.append(f.result())
            except Exception as e:
                r=dict(futs[f]);r.update({"outcome":"ERROR_HARD","reason_code":"WORKER_EXCEPTION","search_errors":repr(e)});done.append(r)
            if i%25==0:print(f"webgate {i}/{len(rows)}",flush=True)
    rank=lambda r:int(r.get("web_gate_rank") or 999999)
    done.sort(key=rank)
    fields=[]
    for r in done:
        for k in r:
            if k not in fields:fields.append(k)
    with (out/"web_gate_results.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(done)
    counts={}
    for r in done:counts[r["outcome"]]=counts.get(r["outcome"],0)+1
    summary={"attempts":len(done),"outcomes":counts,"high":counts.get("HIGH",0),"reject":counts.get("REJECT",0),"medium":counts.get("MEDIUM",0),"uncertain":counts.get("UNCERTAIN",0),"error_retryable":counts.get("ERROR_RETRYABLE",0),"error_hard":counts.get("ERROR_HARD",0)}
    (out/"web_gate_summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print("GWS_WEB_GATE_SUMMARY="+json.dumps(summary))
if __name__=="__main__":main()
