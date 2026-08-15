#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import osmium

STOP={"the","de","la","le","les","du","des","et","and","a","au","aux","sa","sprl","srl","bv","nv","bruxelles","brussels","brussel","belgium","belgique"}
WEBSITE_KEYS=("website","contact:website","operator:website","brand:website","url")
NAME_KEYS=("name","name:fr","name:nl","name:en","official_name","alt_name","short_name","operator","brand")
PHONE_KEYS=("phone","contact:phone","mobile","contact:mobile")


def t(v): return re.sub(r"\s+"," ",str(v or "")).strip()
def n(v):
    s=unicodedata.normalize("NFKD",t(v)).encode("ascii","ignore").decode().lower()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()
def toks(v): return {x for x in n(v).split() if len(x)>1 and x not in STOP}
def sim(a,b):
    a,b=n(a),n(b)
    if not a or not b: return 0.0
    s=SequenceMatcher(None,a,b).ratio(); A,B=toks(a),toks(b); j=len(A&B)/max(1,len(A|B))
    return max(s,0.62*s+0.38*j)
def ov(a,b):
    A,B=toks(a),toks(b); return len(A&B)/max(1,len(A))
def digits(v):
    d=re.sub(r"\D+","",t(v))
    if d.startswith("0032"): d=d[2:]
    if d.startswith("32") and len(d)>=10: return {d,"0"+d[2:]}
    if d.startswith("0") and len(d)>=9: return {d,"32"+d[1:]}
    return {d} if d else set()
def postcode(v):
    m=re.search(r"\b(1\d{3})\b",t(v)); return m.group(1) if m else ""
def addr_text(tags):
    bits=[]
    for k in ("addr:street","addr:housenumber","addr:postcode","addr:city","addr:place","contact:street"):
        if tags.get(k): bits.append(tags[k])
    return " ".join(bits)
def urls(tags):
    out=[]
    for k in WEBSITE_KEYS:
        v=t(tags.get(k))
        if v and v not in out: out.append(v)
    return out
def names(tags):
    out=[]
    for k in NAME_KEYS:
        for v in re.split(r"\s*;\s*",t(tags.get(k))):
            if v and v not in out: out.append(v)
    return out

def load_queue(path):
    rows=[]
    for p in sorted(Path(path).glob("queue_*.jsonl.gz.b64")):
        compact="".join(p.read_text(encoding="utf-8").split())
        raw=gzip.decompress(base64.b64decode(compact,validate=True)).decode("utf-8")
        for line in raw.splitlines():
            if line.strip(): rows.append(json.loads(line))
    by={int(x["r"]):x for x in rows}
    return [by[k] for k in sorted(by)]

class Collector(osmium.SimpleHandler):
    def __init__(self): super().__init__(); self.rows=[]
    def _take(self,obj,typ):
        tags={k:v for k,v in obj.tags}
        ns=names(tags); us=urls(tags); ph=[]
        for k in PHONE_KEYS:
            if tags.get(k): ph.extend(re.split(r"\s*[;/]\s*",tags[k]))
        if not (ns or us or ph): return
        self.rows.append({"osm_type":typ,"osm_id":int(obj.id),"names":ns,"urls":us,"phones":ph,"address":addr_text(tags),"postcode":t(tags.get("addr:postcode")),"tags":{k:tags[k] for k in set(NAME_KEYS+WEBSITE_KEYS+PHONE_KEYS+("addr:street","addr:housenumber","addr:postcode","addr:city","addr:place")) if tags.get(k)}})
    def node(self,o): self._take(o,"node")
    def way(self,o): self._take(o,"way")
    def relation(self,o): self._take(o,"relation")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--queue",required=True); ap.add_argument("--pbf",required=True); ap.add_argument("--expected",type=int,required=True); ap.add_argument("--outdir",required=True); a=ap.parse_args()
    out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    Q=load_queue(a.queue)
    if len(Q)!=a.expected: raise SystemExit(f"QUEUE_COUNT_MISMATCH:{len(Q)}")
    c=Collector(); c.apply_file(a.pbf,locations=False)
    P=c.rows
    ex=defaultdict(list); tk=defaultdict(list); ph=defaultdict(list); pc=defaultdict(list)
    for i,p in enumerate(P):
        for nm in p["names"]:
            ex[n(nm)].append(i)
            for x in list(toks(nm))[:5]: tk[x].append(i)
        for x in p["phones"]:
            for d in digits(x): ph[d].append(i)
        ppc=postcode(p.get("postcode") or p.get("address"))
        if ppc: pc[ppc].append(i)
    results=[]; counts=Counter(); sites=[]; resolved=[]
    for q in Q:
        qn=n(q.get("n")); qpc=t(q.get("p"))[:4]; qph=set()
        for d in digits(q.get("ph")): qph.add(d)
        ids=set(ex.get(qn,[]))
        for d in qph: ids.update(ph.get(d,[]))
        for x in list(toks(q.get("n")))[:4]: ids.update(tk.get(x,[])[:1500])
        if qpc: ids.update(pc.get(qpc,[])[:3000])
        best=None
        for i in ids:
            p=P[i]; ns=max([sim(q.get("n"),z) for z in p["names"]] or [0]); pp=set()
            for z in p["phones"]: pp |= digits(z)
            px=bool(qph & pp)
            ao=ov(q.get("a"),p.get("address")); pm=bool(qpc and qpc==postcode(p.get("postcode") or p.get("address")))
            score=(1.5 if px else 0)+0.9*ns+0.22*ao+(0.15 if pm else 0)
            if best is None or score>best[0]: best=(score,p,px,ns,ao,pm)
        rec={"r":int(q["r"]),"candidate":q,"resolved":False,"strong":False,"owned_site":""}
        if best:
            _,p,px,ns,ao,pm=best
            ok=(px and (ns>=0.35 or ao>=0.30 or pm)) or (ns>=0.93 and (pm or ao>=0.20)) or (ns>=0.82 and pm and ao>=0.25)
            strong=ok and ((px and (ns>=0.55 or ao>=0.45)) or (ns>=0.93 and (pm or ao>=0.35)))
            rec.update(resolved=ok,strong=strong,osm=p,evidence={"phone_exact":px,"name_similarity":round(ns,3),"address_overlap":round(ao,3),"postcode_match":pm})
            if ok: resolved.append(rec); counts["resolved"]+=1
            if strong: counts["strong"]+=1
            if ok and p.get("urls"):
                rec["owned_site"]=p["urls"][0]; sites.append(rec); counts["owned_site"]+=1
        if not rec["resolved"]: counts["unresolved"]+=1
        results.append(rec)
    def dump(name,rows):
        (out/name).write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"))+"\n" for x in rows),encoding="utf-8")
    dump("results.jsonl",results); dump("resolved.jsonl",resolved); dump("owned_sites.jsonl",sites)
    summary={"schema":"gws-osm-direct-resolver-v55","source_candidates":len(Q),"osm_features":len(P),"counts":dict(counts),"new_owned_site_candidates":len(sites),"note":"OSM matches are independent candidate evidence; owned-site URLs require final HTTP identity validation before REJECT."}
    (out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print("GWS_OSM_DIRECT_SUMMARY="+json.dumps(summary,separators=(",",":")),flush=True)

if __name__=="__main__": main()
