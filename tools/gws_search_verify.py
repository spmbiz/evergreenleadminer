#!/usr/bin/env python3
"""Search verification stage for the autonomous GWS fleet.

OpenSERP is the preferred strict evidence transport. If it is unavailable, the
stage falls through to DDGS/SearXNG only to discover and reject owned sites. A
fallback absence can never certify VERIFIED_NO_WEBSITE.

This stage produces pre-canonical serious outcomes only. HIGH still requires the
existing downstream canonical dedupe/persistence/readback contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import gws_home_openserp_worker_v55 as home
import gws_legacy_deep_v2 as v2
import gws_no_website_certifier_v53 as prod
from gws_search_fabric import SearchFabric, query_specs


FAMILY = {
    "google": "google", "duckduckgo": "duckduckgo", "ddg": "duckduckgo",
    "yandex": "yandex", "baidu": "baidu", "bing": "bing", "ecosia": "bing",
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def stable_r(row: dict[str, Any], index: int) -> int:
    raw = str(row.get("hub_objectid") or "")
    digits = "".join(x for x in raw if x.isdigit())
    if digits:
        try:
            return int(digits[-12:])
        except Exception:
            pass
    key = "|".join(str(row.get(k) or "") for k in ("record_key","hub_name","hub_address","hub_postalcode"))
    return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16) + index


def candidate_from_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "r": stable_r(row, index),
        "n": str(row.get("hub_name") or ""),
        "p": str(row.get("hub_postalcode") or ""),
        "a": str(row.get("hub_address") or ""),
        "ph": str(row.get("hub_phone") or ""),
        "em": str(row.get("hub_email") or ""),
        "alias": str(row.get("overture_name") or ""),
    }


def place_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "overture_id": str(row.get("overture_id") or ""),
        "overture_name": str(row.get("overture_name") or ""),
        "resolved": bool(row.get("overture_resolved") or row.get("overture_id")),
        "phone_exact": bool(row.get("phone_exact")),
        "name_similarity": float(row.get("name_similarity") or 0),
        "address_overlap": float(row.get("address_overlap") or 0),
        "postcode_match": bool(row.get("postcode_match")),
        "operating_status": str(row.get("overture_operating_status") or ""),
    }


def provider_families(meta: dict[str, Any]) -> set[str]:
    raw = meta.get("engines_responded") or meta.get("engines") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    out=set()
    for p in raw if isinstance(raw, list) else []:
        name = str(p.get("name") if isinstance(p,dict) else p).lower().strip()
        fam=FAMILY.get(name,name)
        if fam:
            out.add(fam)
    return out


def strict_pass(c: dict[str, Any], fabric: SearchFabric, pass_no: int, max_queries: int) -> dict[str, Any]:
    queries=list(prod.v5.query_set(c,pass_no))[:max_queries]
    search_health=[]; healthy=set(); usable=0; resultful=0; serp=[]; seen=set()
    for query in queries:
        results,event=fabric._openserp("strict_pass_%d" % pass_no,query)  # deliberate strict transport: no DDGS mixing
        fams=provider_families(event.get("meta") or {}) if event.get("status") == "OK" else set()
        healthy.update(fams)
        external=[]
        for item in results:
            u=str(item.url); h=v2.host(u)
            if h and not v2.platform(u) and h not in seen:
                seen.add(h); external.append(u); serp.append({"url":u,"host":h,"title":item.title,"description":item.snippet})
        if len(fams)>=2: usable+=1
        if len(fams)>=2 and external: resultful+=1
        search_health.append({
            "query":query,
            "providers":[{"provider":"openserp","http_ok":event.get("status")=="OK","parsed":event.get("status")=="OK","status":event.get("http_status",200 if event.get("status")=="OK" else 0),"families":sorted(fams),"error":event.get("error")}],
            "parsed_providers":len(fams),
            "raw_resultful":bool(external),
            "external_domains":len(external),
        })
        if usable>=3 and resultful>=1:
            break

    direct=[]; checked=set(); owned=""; owned_identity={}; owned_via=""
    for seed in home.seeds_for_pass(c,pass_no)[:12]:
        h=v2.host(seed)
        if not h or h in checked: continue
        checked.add(h); ev=home.probe_host(c,seed); direct.append(ev)
        if ev.get("matched"):
            owned=ev.get("final") or seed; owned_identity=ev.get("identity") or {}; owned_via="openserp_direct_lattice"; break
    if not owned:
        for item in serp:
            if item["host"] in checked: continue
            plausible,why=home.plausible(c,item)
            if not plausible: continue
            checked.add(item["host"]); ev=home.probe_host(c,item["url"]); ev["serp_hint"]=why; direct.append(ev)
            if ev.get("matched"):
                owned=ev.get("final") or item["url"]; owned_identity=ev.get("identity") or {}; owned_via="openserp_serp_candidate"; break
            if len(direct)>=20: break

    return {
        "search_queries":len(search_health),
        "search_usable_queries":usable,
        "search_resultful_queries":resultful,
        "search_health":search_health,
        "search_candidates":[x["url"] for x in serp],
        "healthy_providers":sorted(healthy),
        "direct_checked":len(direct),
        "direct_health":direct,
        "owned":owned,
        "owned_identity":owned_identity,
        "owned_via":owned_via,
        "candidate_seeds":home.seeds_for_pass(c,pass_no),
        "openserp_strict_pass":pass_no,
    }


def fallback_owned_search(c: dict[str, Any], fabric: SearchFabric, max_queries: int) -> tuple[str, list[dict], list[dict]]:
    events=[]; evidence=[]; checked=set()
    for family,query in query_specs(c,max_queries=max_queries):
        results,evs=fabric.search(family,query); events.extend(evs)
        for item in results:
            u=str(item.get("url") or ""); h=v2.host(u)
            if not h or h in checked or v2.platform(u): continue
            probe_item={"url":u,"host":h,"title":str(item.get("title") or ""),"description":str(item.get("snippet") or "")}
            plausible,why=home.plausible(c,probe_item)
            if not plausible: continue
            checked.add(h); de=home.probe_host(c,u); de["serp_hint"]=why; de["search_provider"]=item.get("provider"); evidence.append(de)
            if de.get("matched"):
                return str(de.get("final") or u),events,evidence
            if len(evidence)>=12:
                return "",events,evidence
    return "",events,evidence


def classify_strict(row: dict[str, Any], c: dict[str, Any], pe: dict[str, Any], fabric: SearchFabric, max_queries: int) -> dict[str, Any]:
    p1=strict_pass(c,fabric,1,max_queries)
    p2=strict_pass(c,fabric,2,max_queries)
    owned=p1.get("owned") or p2.get("owned")
    cert=prod.v5.certificate(c,pe,p1,p2)
    if owned:
        outcome,reason,verify="REJECT","OWNED_SITE_SEARCH_CONFIRMED","REJECT"
        review=False
    elif not prod.v5.coverage(p1).get("ok"):
        outcome,reason,verify="REVIEW","SEARCH_COVERAGE_INSUFFICIENT_PASS1","ERROR_RETRYABLE"
        review=True
    elif not prod.v5.coverage(p2).get("ok"):
        outcome,reason,verify="REVIEW","SEARCH_COVERAGE_INSUFFICIENT_PASS2","ERROR_RETRYABLE"
        review=True
    elif cert.get("unresolved_plausible_domains"):
        outcome,reason,verify="UNCERTAIN","PLAUSIBLE_DOMAIN_UNRESOLVED","UNCERTAIN"
        review=True
    elif not cert.get("gates",{}).get("current_identity_strong"):
        outcome,reason,verify="MEDIUM","IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH","MEDIUM"
        review=True
    elif cert.get("verified"):
        outcome,reason,verify="HIGH","VERIFIED_NO_WEBSITE","HIGH"
        review=True
    else:
        outcome,reason,verify="MEDIUM","SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE","MEDIUM"
        review=True
    row.update({
        "outcome":outcome,"reason":reason,"needs_gpt_review":review,
        "verification_status":verify,"verification_provider":"openserp_ci",
        "owned_website":str(owned or row.get("owned_website") or ""),
        "web_pass1":p1,"web_pass2":p2,"certificate":cert,
        "certificate_digest":str(cert.get("evidence_digest") or ""),
    })
    return row


def classify_fallback(row: dict[str, Any], c: dict[str, Any], fabric: SearchFabric, max_queries: int) -> dict[str, Any]:
    owned,events,evidence=fallback_owned_search(c,fabric,max_queries)
    if owned:
        row.update({
            "outcome":"REJECT","reason":"OWNED_SITE_FALLBACK_SEARCH_CONFIRMED","needs_gpt_review":False,
            "verification_status":"REJECT","verification_provider":"fallback_search_fabric","owned_website":owned,
        })
    else:
        # Fail closed: fallback search can disprove no-site, never prove no-site.
        if row.get("outcome") not in {"UNCERTAIN","MEDIUM"}:
            row["outcome"]="REVIEW"
        row.update({
            "reason":"FALLBACK_SEARCH_SURVIVED_REQUIRES_STRICT_RETRY","needs_gpt_review":True,
            "verification_status":"ERROR_RETRYABLE","verification_provider":"fallback_search_fabric",
        })
    row["fallback_search_events"]=events
    row["fallback_direct_evidence"]=evidence
    return row


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--shard-dir",required=True)
    ap.add_argument("--config",default="config/gws_search_verify.json")
    args=ap.parse_args()
    shard=Path(args.shard_dir); records_path=shard/"records.jsonl"; metrics_path=shard/"metrics.json"
    cfg=load_json(Path(args.config),{})
    if not cfg.get("enabled",True) or not records_path.exists():
        print(json.dumps({"status":"noop","reason":"disabled_or_no_records"}))
        return 0

    rows=[json.loads(x) for x in records_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    search_cfg=dict(cfg.get("search") or {})
    fabric=SearchFabric(search_cfg)
    openserp_ready=fabric.openserp_healthy()
    max_candidates=max(0,int(cfg.get("max_candidates_per_shard") or 0))
    max_queries=max(1,int(search_cfg.get("max_queries_per_candidate") or 5))
    counts=Counter(); verified=0

    for idx,row in enumerate(rows,1):
        if row.get("outcome")=="REJECT":
            row.setdefault("verification_status","REJECT")
            row.setdefault("verification_provider","deterministic_presearch")
            counts["presearch_reject"]+=1
            continue
        if max_candidates and verified>=max_candidates:
            row.update({"verification_status":"ERROR_RETRYABLE","verification_provider":"deferred_budget","reason":"SEARCH_VERIFY_BUDGET_DEFERRED","needs_gpt_review":True})
            counts["budget_deferred"]+=1
            continue
        verified+=1
        c=candidate_from_row(row,idx); pe=place_from_row(row)
        ident_ok,_=prod.v5.complete_identity(c)
        if not ident_ok:
            row.update({"outcome":"UNCERTAIN","reason":"SOURCE_IDENTITY_INCOMPLETE","verification_status":"UNCERTAIN","verification_provider":"identity_gate","needs_gpt_review":True})
            counts["identity_incomplete"]+=1
            continue
        if openserp_ready:
            classify_strict(row,c,pe,fabric,max_queries)
        else:
            classify_fallback(row,c,fabric,max_queries)
        counts[str(row.get("verification_status") or "UNKNOWN")]+=1

    records_path.write_text("".join(json.dumps(r,ensure_ascii=False,sort_keys=True,default=str)+"\n" for r in rows),encoding="utf-8")
    metrics=load_json(metrics_path,{})
    outcomes=Counter(str(r.get("outcome") or "") for r in rows)
    providers=Counter(str(r.get("verification_provider") or "") for r in rows)
    review=sum(1 for r in rows if r.get("needs_gpt_review") and r.get("outcome")!="REJECT")
    metrics.update({
        "review_candidates":review,
        "uncertain":int(outcomes.get("UNCERTAIN",0)),
        "owned_site_or_chain_rejects":int(outcomes.get("REJECT",0)),
        "strict_high_precertified":int(outcomes.get("HIGH",0)),
        "search_verification_attempted":verified,
        "search_verification_statuses":dict(counts),
        "search_verification_providers":dict(providers),
        "openserp_ready":bool(openserp_ready),
        "fallback_no_high_guard":True,
    })
    metrics_path.write_text(json.dumps(metrics,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    summary={"status":"ok","records":len(rows),"attempted":verified,"openserp_ready":openserp_ready,"statuses":dict(counts),"outcomes":dict(outcomes),"providers":dict(providers),"strict_high_precertified":int(outcomes.get("HIGH",0)),"fallback_no_high_guard":True}
    (shard/"search_verify_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("GWS_SEARCH_VERIFY="+json.dumps(summary,separators=(",",":")))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
