#!/usr/bin/env python3
"""Autonomous strict no-website certifier v5.

GPT is NOT part of this verifier. This module is designed to certify
VERIFIED_NO_WEBSITE deterministically when mandatory identity, source-health,
adversarial-search and canonical-dedupe gates all pass. Any missing/blocked
mandatory evidence fails closed to MEDIUM/UNCERTAIN/ERROR_RETRYABLE.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import gws_legacy_deep_v2 as v2
import gws_legacy_deep_v4 as v4

CERT_VERSION = "gws-no-website-v5.0"
BASE_GUESSES = v4.guesses


def complete_identity(c):
    name = v2.t(c.get("n"))
    pc = re.sub(r"\D", "", v2.t(c.get("p")))[:4]
    phone = v2.dg(c.get("ph"))
    addr = v2.t(c.get("a"))
    return bool(name and pc and (phone or addr)), {
        "name": bool(name), "postcode": bool(pc), "phone": bool(phone), "address": bool(addr)
    }


def strong_place_identity(pe):
    if not pe.get("resolved"):
        return False
    if pe.get("phone_exact"):
        return True
    ns = float(pe.get("name_similarity") or 0)
    ao = float(pe.get("address_overlap") or 0)
    pm = bool(pe.get("postcode_match"))
    return bool(pm and ((ns >= 0.94 and ao >= 0.18) or (ns >= 0.88 and ao >= 0.34)))


def query_set(c, pass_no=1):
    name = v2.t(c.get("n")); pc = v2.t(c.get("p"))[:4]
    addr = v2.t(c.get("a")); ph = v2.t(c.get("ph")); alias = v2.t(c.get("alias"))
    street = " ".join(v2.n(addr).split()[:7])
    out = []
    def add(q):
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out: out.append(q)
    if pass_no == 1:
        if name and pc: add(f'"{name}" {pc}')
        if name and street: add(f'"{name}" "{street}"')
        if ph: add(f'"{ph}"')
        if ph:
            d = re.sub(r"\D", "", ph)
            if d and d != ph: add(f'"{d}"')
        if name: add(f'{name} official website')
        if name: add(f'{name} site officiel website officiële website')
    else:
        if alias and v2.n(alias) != v2.n(name):
            add(f'"{alias}" {pc}')
            if street: add(f'"{alias}" "{street}"')
        if name and pc: add(f'{name} {pc} contact')
        if name: add(f'{name} Brussels contact website')
        if name: add(f'{name} instagram facebook website')
        if street and pc: add(f'"{street}" {pc} "{name}"')
        if ph: add(f'{re.sub(r"\D", "", ph)} {name}')
    return out[:7]


def guesses_b(c):
    out = list(BASE_GUESSES(c))
    alias = v2.t(c.get("alias"))
    if alias:
        cc = dict(c); cc["n"] = alias
        out.extend(BASE_GUESSES(cc))
    seen = []
    for u in out:
        h = v2.host(u)
        if h and h not in seen and not v2.platform(u):
            seen.append(h)
    return ["https://" + h + "/" for h in seen[:20]]


def coverage(w):
    healthy = len(set(w.get("healthy_providers") or []))
    searched = int(w.get("search_queries") or 0)
    usable = int(w.get("search_usable_queries") or 0)
    checked = int(w.get("direct_checked") or 0)
    return {
        "healthy_engines": healthy,
        "searched_queries": searched,
        "usable_query_families": usable,
        "direct_domains_checked": checked,
        "ok": healthy >= 2 and searched >= 2 and usable >= 2 and checked >= 5,
    }


def plausible_unresolved(c, w):
    high_risk = set()
    cow = v2.t(c.get("cow"))
    if cow and not v2.platform(cow): high_risk.add(v2.host(cow))
    for d in v4._email_domains(c): high_risk.add(v2.host(d))
    name_tokens = set(v2.toks(c.get("n")))
    unresolved = []
    for h in w.get("direct_health") or []:
        if h.get("ok") and int(h.get("status") or 999) < 400:
            continue
        host = v2.host(h.get("final") or h.get("seed") or "")
        if not host: continue
        dom_tokens = set(v2.toks(host.replace(".", " ")))
        overlap = len(name_tokens & dom_tokens) / max(1, len(name_tokens))
        status = int(h.get("status") or 0)
        risky = host in high_risk or overlap >= 0.5
        if risky and (h.get("error") or status in {0,401,403,408,425,429} or status >= 500):
            unresolved.append({"host": host, "status": status, "error": h.get("error"), "name_overlap": round(overlap,3)})
    by = {x["host"]:x for x in unresolved}
    return [by[k] for k in sorted(by)]


def certificate(c, pe, w1, w2):
    ident_ok, ident_fields = complete_identity(c)
    c1, c2 = coverage(w1), coverage(w2)
    unresolved = plausible_unresolved(c, w1) + plausible_unresolved(c, w2)
    unresolved = {x["host"]:x for x in unresolved}
    gates = {
        "source_identity_complete": ident_ok,
        "current_identity_strong": strong_place_identity(pe),
        "overture_current_resolved": bool(pe.get("resolved")),
        "pass1_search_coverage": c1["ok"],
        "pass2_search_coverage": c2["ok"],
        "no_owned_site_found_pass1": not bool(w1.get("owned")),
        "no_owned_site_found_pass2": not bool(w2.get("owned")),
        "no_plausible_unresolved_domain": not bool(unresolved),
    }
    payload = {
        "certificate_version": CERT_VERSION,
        "identity_fields": ident_fields,
        "identity": pe,
        "pass1": c1,
        "pass2": c2,
        "unresolved_plausible_domains": list(unresolved.values()),
        "gates": gates,
    }
    raw = json.dumps({"place":pe,"pass1":w1,"pass2":w2}, sort_keys=True, separators=(",", ":"), default=str).encode()
    payload["evidence_digest"] = hashlib.sha256(raw).hexdigest()
    payload["verified"] = all(gates.values())
    return payload


def preclassify(c, p, pe, ovok):
    base = {"r": int(c["r"]), "candidate": c, "place": pe}
    ok, _ = complete_identity(c)
    if not v2.in_scope(c): return {**base, "status":"REJECT", "reason":"OUT_OF_SCOPE"}
    if not ok: return {**base, "status":"UNCERTAIN", "reason":"SOURCE_IDENTITY_INCOMPLETE"}
    if not ovok: return {**base, "status":"ERROR_RETRYABLE", "reason":"OVERTURE_UNAVAILABLE"}
    if not pe.get("resolved"): return {**base, "status":"UNCERTAIN", "reason":"CURRENT_IDENTITY_NOT_RESOLVED"}
    if p:
        site = v2.owned(p.get("websites"))
        if site: return {**base, "status":"REJECT", "reason":"OWNED_SITE_OVERTURE", "owned_site":site}
        if v2.t(p.get("operating_status")).lower() in {"closed","permanently_closed"}:
            return {**base, "status":"REJECT", "reason":"CLOSED_OVERTURE"}
    return None


async def run_web(rows, http_conc, search_conc, pass_no):
    old_q, old_g = v4.search_queries, v4.guesses
    try:
        v4.search_queries = lambda c: query_set(c, pass_no)
        v4.guesses = guesses_b if pass_no == 2 else old_g
        return await v4.webcheck(rows, http_conc, search_conc)
    finally:
        v4.search_queries, v4.guesses = old_q, old_g


def worker(a):
    rows, qmeta = v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    part = [x for i,x in enumerate(rows) if i % a.worker_count == a.worker_index]
    z=time.time(); ovok=True; scan_error=""
    try:
        P, scan = v2.load_places(a.threads); I=v2.indexes(P)
    except Exception as e:
        P=[]; I=(defaultdict(list),defaultdict(list),defaultdict(list)); scan=-1; ovok=False
        scan_error=f"{type(e).__name__}:{e}"

    resolved={}; pending=[]; out={}
    for c in part:
        p,pe=v2.resolve(c,P,I) if ovok and v2.in_scope(c) else (None,{"resolved":False})
        cc=dict(c); cc["alias"]=v2.t(pe.get("overture_name"))
        resolved[int(c["r"])]=(cc,p,pe)
        early=preclassify(cc,p,pe,ovok)
        if early: out[int(c["r"])]=early
        else: pending.append(cc)

    W1=asyncio.run(run_web(pending,a.http_concurrency,a.search_concurrency,1)) if pending else {}
    second=[]
    for c in pending:
        r=int(c["r"]); p,pe=resolved[r][1:]
        w=W1.get(r,{})
        if w.get("owned"):
            out[r]={"r":r,"candidate":c,"place":pe,"web_pass1":w,"status":"REJECT","reason":"OWNED_SITE_SEARCH_CONFIRMED","owned_site":w["owned"]}
        elif not coverage(w)["ok"]:
            out[r]={"r":r,"candidate":c,"place":pe,"web_pass1":w,"status":"ERROR_RETRYABLE","reason":"SEARCH_COVERAGE_INSUFFICIENT_PASS1"}
        else:
            second.append(c)

    W2=asyncio.run(run_web(second,a.http_concurrency,a.search_concurrency,2)) if second else {}
    for c in second:
        r=int(c["r"]); p,pe=resolved[r][1:]; w1=W1[r]; w2=W2.get(r,{})
        if w2.get("owned"):
            out[r]={"r":r,"candidate":c,"place":pe,"web_pass1":w1,"web_pass2":w2,"status":"REJECT","reason":"OWNED_SITE_SECOND_PASS_CONFIRMED","owned_site":w2["owned"]}
            continue
        cert=certificate(c,pe,w1,w2)
        if not coverage(w2)["ok"]:
            st,reason="ERROR_RETRYABLE","SEARCH_COVERAGE_INSUFFICIENT_PASS2"
        elif cert["unresolved_plausible_domains"]:
            st,reason="UNCERTAIN","PLAUSIBLE_DOMAIN_UNRESOLVED"
        elif not cert["gates"]["current_identity_strong"]:
            st,reason="MEDIUM","IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH"
        elif cert["verified"]:
            st,reason="HIGH","VERIFIED_NO_WEBSITE"
        else:
            st,reason="MEDIUM","SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE"
        out[r]={"r":r,"candidate":c,"place":pe,"web_pass1":w1,"web_pass2":w2,"certificate":cert,"status":st,"reason":reason}

    final=[out[int(c["r"])] for c in part]
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True); v2.dump(d/"results.jsonl",final)
    S=Counter(x["status"] for x in final); reasons=Counter(x.get("reason") for x in final)
    summ={"worker":a.worker_index,"attempted":len(part),"statuses":dict(S),"reasons":dict(reasons),
          "high_verified_no_website":S.get("HIGH",0),"owned_sites_found":sum(str(x.get("reason","")).startswith("OWNED_SITE") for x in final),
          "scan_seconds":scan,"scan_error":scan_error,"queue_files":len(qmeta["files"]),"elapsed_seconds":round(time.time()-z,2),"cert_version":CERT_VERSION}
    (d/"summary.json").write_text(json.dumps(summ,indent=2)+"\n",encoding="utf-8")
    print("GWS_V5_WORKER="+json.dumps(summ,separators=(",",":")))


def canonical_key(x):
    pe=x.get("place") or {}; c=x.get("candidate") or {}
    oid=v2.t(pe.get("overture_id")); ph=v2.dg(c.get("ph")); name=v2.n(c.get("n")); pc=v2.t(c.get("p"))[:4]
    addr=" ".join(v2.n(c.get("a")).split()[:8])
    if oid: return "o:"+oid
    if ph: return "p:"+ph
    return "n:"+name+"|"+pc+"|"+addr


def aggregate(a):
    root=Path(a.input_root); rows=[]; sums=[]
    for p in root.rglob("results.jsonl"):
        rows.extend(json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
    for p in root.rglob("summary.json"):
        try:sums.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:pass
    by={}; dup_rows=0
    for x in rows:
        r=int(x["r"]); dup_rows += r in by; by[r]=x
    rows=[by[k] for k in sorted(by)]
    if len(rows)!=a.expected:
        raise SystemExit(f"INCOMPLETE_AGGREGATE expected={a.expected} got={len(rows)}")

    groups=defaultdict(list)
    for x in rows: groups[canonical_key(x)].append(x)
    canonical_dups=0; reconciled=[]
    rank={"HIGH":5,"MEDIUM":4,"UNCERTAIN":3,"ERROR_RETRYABLE":2,"ERROR_HARD":1,"REJECT":0}
    for key,members in groups.items():
        site_bad=[x for x in members if str(x.get("reason","")).startswith("OWNED_SITE") or str(x.get("reason","")).startswith("CLOSED_")]
        if site_bad:
            canonical=min(members,key=lambda x:int(x["r"])); evidence=site_bad[0]
            canonical["status"]="REJECT"
            canonical["reason"]="CANONICAL_ENTITY_DISQUALIFIED"
            canonical["group_disqualifier"]=evidence.get("reason")
            if evidence.get("owned_site"): canonical["owned_site"]=evidence.get("owned_site")
        else:
            canonical=max(members,key=lambda x:(rank.get(x.get("status"),-1),-int(x["r"])))
        cr=int(canonical["r"]); reconciled.append(canonical)
        for x in members:
            if x is canonical: continue
            canonical_dups+=1; x["status"]="DUPLICATE"; x["reason"]="CANONICAL_DUPLICATE"; x["duplicate_of_r"]=cr
            if site_bad: x["group_disqualifier"]=site_bad[0].get("reason")
            reconciled.append(x)
    rows=sorted(reconciled,key=lambda x:int(x["r"]))

    high=[x for x in rows if x.get("status")=="HIGH"]
    known_owned={"kanoff legal","kanoff co","id cite architects"}
    breached=[x for x in high if v2.n((x.get("candidate") or {}).get("n")) in known_owned]
    if breached:
        raise SystemExit("KNOWN_OWNED_REGRESSION_BREACH:" + ",".join(str(x.get("r")) for x in breached))
    exceptions=[x for x in rows if x.get("status")!="HIGH"]
    sites=[x for x in rows if str(x.get("reason","")).startswith("OWNED_SITE") or x.get("group_disqualifier"," ").startswith("OWNED_SITE")]
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    blind=[]
    for x in high:
        c=x.get("candidate") or {}
        blind.append({"r":x.get("r"),"business_name":c.get("n"),"postal_code":c.get("p"),"street_address":c.get("a"),"phone":c.get("ph"),"email":c.get("em")})
    v2.dump(d/"verified_no_website.jsonl",high); v2.dump(d/"gpt_redteam_blind.jsonl",blind); v2.dump(d/"exceptions.jsonl",exceptions); v2.dump(d/"owned_site_hits.jsonl",sites)
    import base64,gzip
    raw=("\n".join(json.dumps(x,ensure_ascii=False,separators=(",",":")) for x in rows)+"\n").encode()
    (d/"results.jsonl.gz.b64").write_text(base64.b64encode(gzip.compress(raw,9)).decode()+"\n",encoding="utf-8")
    S=Counter(x["status"] for x in rows); reasons=Counter(x.get("reason") for x in rows)
    summ={"schema_version":3,"cert_version":CERT_VERSION,"expected":a.expected,"attempted_unique":len(rows),"statuses":dict(S),"reasons":dict(reasons),
          "verified_no_website":len(high),"owned_sites_found":len(sites),"exceptions":len(exceptions),"duplicate_results":dup_rows,
          "canonical_duplicates":canonical_dups,"worker_summaries":len(sums),"run_id":os.getenv("GITHUB_RUN_ID","local"),
          "updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    (d/"summary.json").write_text(json.dumps(summ,indent=2)+"\n",encoding="utf-8")
    print("GWS_V5_AGG="+json.dumps(summ,separators=(",",":")))


def main():
    ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest="cmd",required=True)
    p=sp.add_parser("preflight"); p.add_argument("--queue",required=True); p.add_argument("--expected",type=int,default=5047)
    w=sp.add_parser("worker"); w.add_argument("--queue",required=True); w.add_argument("--worker-index",type=int,required=True); w.add_argument("--worker-count",type=int,required=True); w.add_argument("--threads",type=int,default=12); w.add_argument("--http-concurrency",type=int,default=32); w.add_argument("--search-concurrency",type=int,default=2); w.add_argument("--expected",type=int,default=5047); w.add_argument("--outdir",required=True)
    g=sp.add_parser("aggregate"); g.add_argument("--input-root",required=True); g.add_argument("--outdir",required=True); g.add_argument("--expected",type=int,default=5047)
    a=ap.parse_args()
    if a.cmd=="preflight": v2.preflight(a)
    elif a.cmd=="worker": worker(a)
    else: aggregate(a)

if __name__=="__main__": main()
