#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from typing import Any

import requests

DECISIONS={"MATCH","PROBABLE","WRONG","UNCERTAIN"}
WEBSITE_STATES={
    "NO_SITE","DEAD_SITE","BROKEN_SITE","PARKED_DOMAIN","FACEBOOK_ONLY","DIRECTORY_ONLY",
    "ANCIENT_SITE","NON_MOBILE_SITE","NO_SSL","ONE_PAGE_BAD_SITE","BAD_CONVERSION_SITE","GOOD_SITE","UNCERTAIN"
}

SYSTEM_PROMPT='''You classify whether one supplied URL is a current FIRST-PARTY website owned by the named local business.
/no_think
Use ONLY supplied evidence. Never invent a URL or fact.
Directories, social/profile/listing/booking/editorial pages are NOT owned sites.
Identity match on a page does NOT prove domain ownership.
legacy_identity never proves ownership.
MATCH/PROBABLE require positive first-party control evidence.
Dead/NXDOMAIN/parked historical domains are not current owned websites.
If no supplied candidate has positive ownership evidence, return candidate_url="" and WRONG or UNCERTAIN.
You are shadow-only and cannot certify VERIFIED_NO_WEBSITE.

Return ONLY JSON:
{"items":[{"business_id":"...","candidate_url":"...","decision":"MATCH|PROBABLE|WRONG|UNCERTAIN","confidence":0.0,"matching_evidence":["..."],"contradictions":["..."],"website_state":"NO_SITE|DEAD_SITE|BROKEN_SITE|PARKED_DOMAIN|FACEBOOK_ONLY|DIRECTORY_ONLY|ANCIENT_SITE|NON_MOBILE_SITE|NO_SSL|ONE_PAGE_BAD_SITE|BAD_CONVERSION_SITE|GOOD_SITE|UNCERTAIN","needs_gpt_review":false,"reason":"short reason"}]}'''

def strip_thinking(text:str)->str:
    x=(text or "").strip()
    x=re.sub(r"(?is)^\s*<think>.*?</think>\s*","",x).strip()
    if x.startswith("```"):
        x=re.sub(r"^```(?:json)?\s*","",x,flags=re.I)
        x=re.sub(r"\s*```$","",x).strip()
    return x

def _allowed_urls(record:dict[str,Any])->set[str]:
    out=set()
    legacy=str(record.get("candidate_url") or "").strip()
    if legacy: out.add(legacy)
    for item in record.get("candidate_set") or []:
        if isinstance(item,dict):
            u=str(item.get("url") or "").strip()
            if u: out.add(u)
    return out

def _compact_probe(p:Any)->dict[str,Any]:
    if not isinstance(p,dict): return {}
    ident=p.get("identity") if isinstance(p.get("identity"),dict) else {}
    return {
        "ok":bool(p.get("ok")),
        "status":int(p.get("status") or 0),
        "dns_negative":bool(p.get("dns_negative")),
        "matched":bool(p.get("matched")),
        "match_mode":p.get("match_mode") or ident.get("match_mode") or "",
        "final_url":str(p.get("final") or "")[:180],
    }

def _compact_ownership(x:Any)->dict[str,Any]:
    if not isinstance(x,dict): return {}
    try: addr=round(float(x.get("address_overlap") or 0.0),2)
    except Exception: addr=0.0
    return {
        "confident":bool(x.get("confident")),
        "reason":str(x.get("reason") or "")[:100],
        "third_party":bool(x.get("third_party")),
        "phone_exact":bool(x.get("phone_exact")),
        "address_overlap":addr,
        "postcode_match":bool(x.get("postcode_match")),
        "branded_host":bool(x.get("branded_host")),
    }

def _candidate_score(c:dict[str,Any], legacy_url:str)->tuple:
    own=c.get("ownership_assessment") if isinstance(c.get("ownership_assessment"),dict) else {}
    probe=c.get("probe") if isinstance(c.get("probe"),dict) else {}
    hc=str(c.get("host_class") or "").upper()
    try: rank=int(c.get("rank") or 999)
    except Exception: rank=999
    return (
        1 if bool(own.get("confident")) else 0,
        1 if str(c.get("url") or "")==legacy_url else 0,
        1 if bool(probe.get("matched")) else 0,
        1 if bool(c.get("plausible")) else 0,
        0 if hc in {"KNOWN_THIRD_PARTY","EDITORIAL_OR_PROFILE_PAGE"} else 1,
        1 if bool(probe.get("ok")) else 0,
        -rank,
    )

def _compact_record(r:dict[str,Any], max_candidates:int=5)->dict[str,Any]:
    legacy=str(r.get("candidate_url") or "").strip()
    raw=[x for x in (r.get("candidate_set") or []) if isinstance(x,dict) and x.get("url")]
    seen=set(); uniq=[]
    for c in raw:
        u=str(c.get("url") or "").strip()
        if not u or u in seen: continue
        seen.add(u); uniq.append(c)
    uniq=sorted(uniq,key=lambda c:_candidate_score(c,legacy),reverse=True)[:max_candidates]
    candidates=[]
    for c in uniq:
        hint=c.get("plausibility_hint") if isinstance(c.get("plausibility_hint"),dict) else {}
        try: overlap=round(float(hint.get("text_overlap") or 0.0),2)
        except Exception: overlap=0.0
        candidates.append({
            "url":str(c.get("url") or "")[:240],
            "host":str(c.get("host") or "")[:100],
            "host_class":str(c.get("host_class") or "")[:50],
            "source":str(c.get("source") or "")[:40],
            "plausible":bool(c.get("plausible")),
            "name_overlap":overlap,
            "phone_snippet":bool(hint.get("phone_snippet")),
            "probe":_compact_probe(c.get("probe")),
            "ownership":_compact_ownership(c.get("ownership_assessment")),
        })
    try: ns=round(float(r.get("name_similarity") or 0.0),2)
    except Exception: ns=0.0
    try: ao=round(float(r.get("address_overlap") or 0.0),2)
    except Exception: ao=0.0
    return {
        "business_id":str(r.get("business_id") or ""),
        "name":str(r.get("name") or "")[:120],
        "address":str(r.get("address") or "")[:180],
        "postcode":str(r.get("postcode") or "")[:20],
        "identity":{
            "overture_name":str(r.get("overture_name") or "")[:120],
            "resolved":bool(r.get("overture_resolved")),
            "name_similarity":ns,
            "address_overlap":ao,
            "postcode_match":bool(r.get("postcode_match")),
            "phone_exact":bool(r.get("phone_exact")),
        },
        "legacy_candidate_url":legacy[:240],
        "legacy_host_class":str(r.get("candidate_host_class") or "")[:50],
        "candidates":candidates,
        "unresolved_domain_count":len(r.get("unresolved_plausible_domains") or []),
        "platform_only_count":len(r.get("platform_only_signals") or []),
    }

def validate_item(item:dict[str,Any], expected:dict[str,set[str]])->dict[str,Any]|None:
    bid=str(item.get("business_id") or "")
    if bid not in expected: return None
    decision=str(item.get("decision") or "UNCERTAIN").upper()
    state=str(item.get("website_state") or "UNCERTAIN").upper()
    if decision not in DECISIONS: decision="UNCERTAIN"
    if state not in WEBSITE_STATES: state="UNCERTAIN"
    candidate_url=str(item.get("candidate_url") or "").strip()
    if candidate_url and candidate_url not in expected[bid]:
        candidate_url=""; decision="UNCERTAIN"; state="UNCERTAIN"
    if decision in {"MATCH","PROBABLE"} and not candidate_url:
        decision="UNCERTAIN"
    try: conf=max(0.0,min(1.0,float(item.get("confidence") or 0.0)))
    except Exception: conf=0.0
    return {
        "business_id":bid,"candidate_url":candidate_url,"decision":decision,"confidence":conf,
        "matching_evidence":[str(x)[:180] for x in (item.get("matching_evidence") or [])[:4]],
        "contradictions":[str(x)[:180] for x in (item.get("contradictions") or [])[:4]],
        "website_state":state,"needs_gpt_review":bool(item.get("needs_gpt_review")),
        "reason":str(item.get("reason") or "")[:350],
    }

def fallback(records:list[dict[str,Any]], error:str)->list[dict[str,Any]]:
    return [{
        "business_id":str(r.get("business_id") or ""),"candidate_url":"",
        "decision":"UNCERTAIN","confidence":0.0,"matching_evidence":[],"contradictions":[],
        "website_state":"UNCERTAIN","needs_gpt_review":True,
        "reason":f"Classifier unavailable or invalid: {error}"[:700],"_classifier_error":error,
    } for r in records]

def health(base_url:str, timeout:float=3)->bool:
    try: return requests.get(base_url.rstrip("/")+"/health",timeout=timeout).status_code<500
    except Exception: return False

def _post_once(base_url:str, body:dict[str,Any], timeout:float)->dict[str,Any]:
    resp=requests.post(base_url.rstrip("/")+"/v1/chat/completions",json=body,timeout=timeout)
    if resp.status_code>=400:
        detail=re.sub(r"\s+"," ",resp.text or "")[:500]
        raise RuntimeError(f"HTTP_{resp.status_code}:{detail}")
    return resp.json()

def classify_batch(records:list[dict[str,Any]], base_url:str, model_label:str, timeout:float=45)->list[dict[str,Any]]:
    if not records: return []
    if not base_url or not health(base_url): return fallback(records,"QWEN_UNAVAILABLE")
    outputs=[]
    for r in records:
        expected={str(r.get("business_id") or ""):_allowed_urls(r)}
        compact=_compact_record(r,max_candidates=5)
        user="/no_think\nJudge the supplied candidates for first-party ownership. INPUT="+json.dumps(compact,ensure_ascii=False,separators=(",",":"))
        base_body={
            "model":model_label,
            "messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user}],
            "temperature":0.1,"top_p":0.8,"max_tokens":320,
        }
        last="UNKNOWN"
        for attempt in range(2):
            try:
                body=dict(base_body)
                if attempt==0:
                    body["response_format"]={"type":"json_object"}
                data=_post_once(base_url,body,timeout)
                parsed=json.loads(strip_thinking(data["choices"][0]["message"]["content"]))
                items=parsed.get("items") if isinstance(parsed,dict) else None
                if not isinstance(items,list): raise ValueError("missing_items_array")
                valid={}
                for item in items:
                    if isinstance(item,dict):
                        v=validate_item(item,expected)
                        if v: valid[v["business_id"]]=v
                bid=str(r.get("business_id") or "")
                if bid not in valid: raise ValueError("missing_valid_item")
                outputs.append(valid[bid]); break
            except Exception as exc:
                last=f"{type(exc).__name__}:{str(exc)[:500]}"
                if attempt==1:
                    outputs.append(fallback([r],last)[0])
    return outputs
