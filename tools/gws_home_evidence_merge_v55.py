#!/usr/bin/env python3
"""Re-evaluate residential observations into strict serious-verification outcomes.

The merger distrusts the worker's HIGH-like verdict and recomputes the production
certificate from raw pass evidence. Deterministic out-of-scope/public-entity and
owned-site rejects are independently re-applied before certificate evaluation.
It never writes the canonical spreadsheet.
"""
from __future__ import annotations

import argparse, json
from collections import Counter
from pathlib import Path

import gws_no_website_certifier_v53 as prod


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--input",required=True); ap.add_argument("--outdir",required=True); a=ap.parse_args()
    rows=[json.loads(x) for x in Path(a.input).read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    out=[]; counts=Counter(); reasons=Counter(); highs=[]
    for x in rows:
        c=dict(x.get("candidate") or {}); pe=dict(x.get("place") or {}); w1=dict(x.get("pass1") or {}); w2=dict(x.get("pass2") or {})
        owned=w1.get("owned") or w2.get("owned") or x.get("owned_site") or ""
        cert=prod.v5.certificate(c,pe,w1,w2) if w1 or w2 else {"verified":False,"gates":{},"unresolved_plausible_domains":[]}
        c1=prod.v5.coverage(w1) if w1 else {"ok":False}; c2=prod.v5.coverage(w2) if w2 else {"ok":False}
        if not prod.v2.in_scope(c):
            status,reason="REJECT","OUT_OF_SCOPE"
        elif prod.obvious_non_independent_entity(c):
            status,reason="REJECT","OUT_OF_SCOPE_NON_INDEPENDENT_PUBLIC_ENTITY"
        elif owned:
            status,reason="REJECT","OWNED_SITE_RESIDENTIAL_CONFIRMED"
        elif not c1.get("ok"):
            status,reason="ERROR_RETRYABLE","RESIDENTIAL_SEARCH_COVERAGE_INSUFFICIENT_PASS1"
        elif not c2.get("ok"):
            status,reason="ERROR_RETRYABLE","RESIDENTIAL_SEARCH_COVERAGE_INSUFFICIENT_PASS2"
        elif cert.get("unresolved_plausible_domains"):
            status,reason="UNCERTAIN","PLAUSIBLE_DOMAIN_UNRESOLVED"
        elif not cert.get("gates",{}).get("current_identity_strong"):
            status,reason="MEDIUM","IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH"
        elif cert.get("verified"):
            status,reason="HIGH","VERIFIED_NO_WEBSITE"
        else:
            status,reason="MEDIUM","SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE"
        rec={"r":int(x.get("r")),"candidate":c,"place":pe,"web_pass1":w1,"web_pass2":w2,"certificate":cert,"status":status,"reason":reason}
        if owned: rec["owned_site"]=owned
        out.append(rec); counts[status]+=1; reasons[reason]+=1
        if status=="HIGH": highs.append(rec)
    d=Path(a.outdir); d.mkdir(parents=True,exist_ok=True)
    def dump(name,arr): (d/name).write_text("".join(json.dumps(z,ensure_ascii=False,separators=(",",":"),default=str)+"\n" for z in arr),encoding="utf-8")
    dump("certified_results.jsonl",out); dump("high_candidates.jsonl",highs)
    summary={"schema":"gws-home-evidence-merge-v2","attempted":len(out),"statuses":dict(counts),"reasons":dict(reasons),"verified_no_website":counts.get("HIGH",0),"owned_sites_found":sum(1 for x in out if x.get('reason')=='OWNED_SITE_RESIDENTIAL_CONFIRMED'),"integrity":{"input_unique_r":len({int(x.get('r')) for x in rows})==len(rows),"output_rows":len(out),"high_rows":len(highs)},"note":"HIGH here is deterministic pre-canonical certification only; canonical MASTER append/readback and one-time blind GPT red-team remain required before final rollout."}
    (d/"summary.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print("GWS_HOME_MERGE="+json.dumps(summary,separators=(",",":")),flush=True)

if __name__=="__main__": main()
