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

SYSTEM_PROMPT='''You are the semantic ambiguity resolver for a high-recall local-business website harvester.
/no_think
You receive ONLY compact public/deterministic evidence already collected by other workers.

Rules:
- Decide whether candidate_url belongs to the named business: MATCH, PROBABLE, WRONG, or UNCERTAIN.
- Classify website_state only from supplied evidence.
- Search absence NEVER proves NO_SITE. If there is no positive website evidence, prefer UNCERTAIN unless supplied deterministic page evidence supports another state.
- Facebook, Instagram, directories, booking aggregators and listing pages are not owned websites.
- Strong phone/address/name contradictions outweigh weak name resemblance.
- Preserve unusual or potentially valuable businesses for review.
- Never invent or output an email, phone, address, social profile, domain, URL, company identity, or fact.
- You are SHADOW ONLY. You cannot certify VERIFIED_NO_WEBSITE and cannot override deterministic HIGH/REJECT decisions.

Allowed website_state values:
NO_SITE, DEAD_SITE, BROKEN_SITE, PARKED_DOMAIN, FACEBOOK_ONLY, DIRECTORY_ONLY, ANCIENT_SITE, NON_MOBILE_SITE, NO_SSL, ONE_PAGE_BAD_SITE, BAD_CONVERSION_SITE, GOOD_SITE, UNCERTAIN.

Return ONLY JSON in this exact shape:
{"items":[{"business_id":"...","candidate_url":"...","decision":"MATCH|PROBABLE|WRONG|UNCERTAIN","confidence":0.0,"matching_evidence":["..."],"contradictions":["..."],"website_state":"...","needs_gpt_review":false,"reason":"short evidence-grounded reason"}]}
Return exactly one item for every business_id.'''


def strip_thinking(text:str)->str:
    x=(text or "").strip()
    x=re.sub(r"(?is)^\s*<think>.*?</think>\s*","",x).strip()
    if x.startswith("```"):
        x=re.sub(r"^```(?:json)?\s*","",x,flags=re.I)
        x=re.sub(r"\s*```$","",x).strip()
    return x


def validate_item(item:dict[str,Any], expected:dict[str,str])->dict[str,Any]|None:
    bid=str(item.get("business_id") or "")
    if bid not in expected: return None
    decision=str(item.get("decision") or "UNCERTAIN").upper()
    state=str(item.get("website_state") or "UNCERTAIN").upper()
    if decision not in DECISIONS: decision="UNCERTAIN"
    if state not in WEBSITE_STATES: state="UNCERTAIN"
    # Candidate URL is identity-bearing input, never model-created output.
    candidate_url=expected[bid]
    try: conf=max(0.0,min(1.0,float(item.get("confidence") or 0.0)))
    except Exception: conf=0.0
    return {
        "business_id":bid,"candidate_url":candidate_url,"decision":decision,"confidence":conf,
        "matching_evidence":[str(x)[:300] for x in (item.get("matching_evidence") or [])[:8]],
        "contradictions":[str(x)[:300] for x in (item.get("contradictions") or [])[:8]],
        "website_state":state,"needs_gpt_review":bool(item.get("needs_gpt_review")),
        "reason":str(item.get("reason") or "")[:700],
    }


def fallback(records:list[dict[str,Any]], error:str)->list[dict[str,Any]]:
    return [{
        "business_id":str(r.get("business_id") or ""),"candidate_url":str(r.get("candidate_url") or ""),
        "decision":"UNCERTAIN","confidence":0.0,"matching_evidence":[],"contradictions":[],
        "website_state":"UNCERTAIN","needs_gpt_review":True,
        "reason":f"Classifier unavailable or invalid: {error}"[:700],"_classifier_error":error,
    } for r in records]


def health(base_url:str, timeout:float=3)->bool:
    try: return requests.get(base_url.rstrip("/")+"/health",timeout=timeout).status_code<500
    except Exception: return False


def classify_batch(records:list[dict[str,Any]], base_url:str, model_label:str, timeout:float=150)->list[dict[str,Any]]:
    if not records: return []
    if not base_url or not health(base_url): return fallback(records,"QWEN_UNAVAILABLE")
    expected={str(r.get("business_id") or ""):str(r.get("candidate_url") or "") for r in records}
    payload=[]
    for r in records:
        payload.append({k:r.get(k) for k in (
            "business_id","name","address","postcode","public_phone_present","candidate_url","candidate_host",
            "overture_name","overture_resolved","name_similarity","address_overlap","postcode_match","phone_exact",
            "search_candidates","direct_identity_evidence","unresolved_plausible_domains","platform_only_signals"
        )})
    user="/no_think\nResolve these ambiguous GWS business/site cases. Use only supplied evidence.\nINPUT="+json.dumps(payload,ensure_ascii=False,separators=(",",":"))
    body={
        "model":model_label,
        "messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user}],
        "temperature":0.2,"top_p":0.8,
        "max_tokens":max(700,min(2800,280*len(records))),
        "response_format":{"type":"json_object"},
        "chat_template_kwargs":{"enable_thinking":False},
    }
    last="UNKNOWN"
    for _ in range(2):
        try:
            resp=requests.post(base_url.rstrip("/")+"/v1/chat/completions",json=body,timeout=timeout)
            resp.raise_for_status(); data=resp.json()
            parsed=json.loads(strip_thinking(data["choices"][0]["message"]["content"]))
            items=parsed.get("items") if isinstance(parsed,dict) else None
            if not isinstance(items,list): raise ValueError("missing_items_array")
            valid={}
            for item in items:
                if isinstance(item,dict):
                    v=validate_item(item,expected)
                    if v: valid[v["business_id"]]=v
            if not valid: raise ValueError("no_valid_items")
            return [valid.get(str(r.get("business_id") or "")) or fallback([r],"MISSING_ITEM")[0] for r in records]
        except Exception as exc:
            last=f"{type(exc).__name__}:{str(exc)[:160]}"
            body["messages"][1]["content"]="/no_think\nReturn only valid JSON. "+user
            body["max_tokens"]=min(int(body["max_tokens"]),2000)
    return fallback(records,last)
