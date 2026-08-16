#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from typing import Any

import requests

ENTITY = {'MATCH', 'PROBABLE', 'WRONG', 'UNCERTAIN'}
BUSINESS = {
    'SHORT_STAY_OPERATOR', 'PROPERTY_MANAGER_SHORT_STAY', 'BOUTIQUE_HOTEL', 'RESORT',
    'SERVICED_ACCOMMODATION', 'OTHER_HOSPITALITY', 'LONG_TERM_PROPERTY_MANAGEMENT', 'UNRELATED', 'UNCERTAIN'
}
FIT = {'STRONG_FIT', 'FIT', 'MAYBE', 'REJECT_OBVIOUS'}

SYSTEM_PROMPT = '''You are a high-recall hospitality account triage classifier for an AI production sales harvester.
/no_think
Your job is NOT to be aggressive. Preserve plausible commercial opportunities.

Important rules:
- Operators, villa managers, vacation-rental agencies, holiday-home operators, boutique hotels, resorts, serviced accommodation and multi-property managers are valuable.
- A novel, unusual, unclear, brokerable, premium or potentially multi-property business should survive as FIT or MAYBE.
- REJECT_OBVIOUS only when the evidence is clear that the business is unrelated to short-stay/hospitality opportunity or is clearly generic long-term property management with no short-stay evidence.
- UNKNOWN or insufficient evidence means MAYBE/UNCERTAIN, not rejection.
- Never invent an email, phone, social account, property count, location, company identity, service, or fact.
- Use only the supplied evidence.
- Entity-match labels: MATCH, PROBABLE, WRONG, UNCERTAIN.
- Business-type labels: SHORT_STAY_OPERATOR, PROPERTY_MANAGER_SHORT_STAY, BOUTIQUE_HOTEL, RESORT, SERVICED_ACCOMMODATION, OTHER_HOSPITALITY, LONG_TERM_PROPERTY_MANAGEMENT, UNRELATED, UNCERTAIN.
- Fit labels: STRONG_FIT, FIT, MAYBE, REJECT_OBVIOUS.

Return ONLY valid JSON in this exact top-level shape:
{"items":[{"account_id":"...","entity_match":"...","business_type":"...","fit_decision":"...","confidence":0.0,"unusual_or_novel":false,"matching_evidence":["..."],"contradictions":["..."],"reason":"short evidence-grounded reason"}]}
Return exactly one item per input account_id.'''


def compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    account = rec.get('account') or rec
    portfolio = rec.get('portfolio') or {}
    search = rec.get('search_results') or []
    raw = account.get('raw') or {}
    return {
        'account_id': account.get('account_id'),
        'name': account.get('name'),
        'country': account.get('country'),
        'region': account.get('region'),
        'city': account.get('city'),
        'website': account.get('website'),
        'public_email_present': bool(account.get('public_email')),
        'public_phone_present': bool(account.get('public_phone')),
        'existing_fit_tier': account.get('fit_tier'),
        'operator_score': account.get('operator_score'),
        'premium_score': account.get('premium_score'),
        'homepage_excerpt': str(portfolio.get('homepage_excerpt') or '')[:2200],
        'property_count_from_public_urls': int(portfolio.get('property_count_known') or 0),
        'pms_fingerprints': portfolio.get('pms_fingerprints') or [],
        'first_party_links': portfolio.get('first_party') or {},
        'search_results': [
            {
                'provider': x.get('provider'), 'query_family': x.get('query_family'),
                'title': str(x.get('title') or '')[:220], 'url': x.get('url'), 'snippet': str(x.get('snippet') or '')[:500]
            }
            for x in search[:8]
        ],
        'source_categories': raw.get('categories') or raw.get('category') or '',
    }


def strip_thinking(text: str) -> str:
    x = (text or '').strip()
    x = re.sub(r'(?is)^\s*<think>.*?</think>\s*', '', x).strip()
    if x.startswith('```'):
        x = re.sub(r'^```(?:json)?\s*', '', x, flags=re.I)
        x = re.sub(r'\s*```$', '', x)
    return x.strip()


def validate_item(item: dict[str, Any], expected_ids: set[str]) -> dict[str, Any] | None:
    aid = str(item.get('account_id') or '')
    if aid not in expected_ids:
        return None
    entity = str(item.get('entity_match') or 'UNCERTAIN').upper()
    business = str(item.get('business_type') or 'UNCERTAIN').upper()
    fit = str(item.get('fit_decision') or 'MAYBE').upper()
    if entity not in ENTITY:
        entity = 'UNCERTAIN'
    if business not in BUSINESS:
        business = 'UNCERTAIN'
    if fit not in FIT:
        fit = 'MAYBE'
    try:
        conf = max(0.0, min(1.0, float(item.get('confidence') or 0.0)))
    except Exception:
        conf = 0.0
    return {
        'account_id': aid,
        'entity_match': entity,
        'business_type': business,
        'fit_decision': fit,
        'confidence': conf,
        'unusual_or_novel': bool(item.get('unusual_or_novel')),
        'matching_evidence': [str(x)[:300] for x in (item.get('matching_evidence') or [])[:8]],
        'contradictions': [str(x)[:300] for x in (item.get('contradictions') or [])[:8]],
        'reason': str(item.get('reason') or '')[:700],
    }


def fallback(records: list[dict[str, Any]], error: str) -> list[dict[str, Any]]:
    return [{
        'account_id': (r.get('account') or r).get('account_id'),
        'entity_match': 'UNCERTAIN', 'business_type': 'UNCERTAIN', 'fit_decision': 'MAYBE',
        'confidence': 0.0, 'unusual_or_novel': False, 'matching_evidence': [], 'contradictions': [],
        'reason': f'Classifier unavailable or invalid: {error}'[:700], '_classifier_error': error,
    } for r in records]


def health(base_url: str, timeout: float = 3) -> bool:
    try:
        return requests.get(base_url.rstrip('/') + '/health', timeout=timeout).status_code < 500
    except Exception:
        return False


def classify_batch(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float = 120) -> list[dict[str, Any]]:
    if not records:
        return []
    if not base_url or not health(base_url):
        return fallback(records, 'QWEN_UNAVAILABLE')
    expected = {str((r.get('account') or r).get('account_id') or '') for r in records}
    payload_records = [compact_record(r) for r in records]
    user = '/no_think\nClassify these hospitality accounts. Preserve uncertain/novel opportunities.\nINPUT=' + json.dumps(payload_records, ensure_ascii=False, separators=(',', ':'))
    body = {
        'model': model_label,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': user}],
        'temperature': 0.3,
        'top_p': 0.8,
        'max_tokens': max(600, min(2400, 260 * len(records))),
        'response_format': {'type': 'json_object'},
        'chat_template_kwargs': {'enable_thinking': False},
    }
    last_error = 'UNKNOWN'
    for _ in range(2):
        try:
            r = requests.post(base_url.rstrip('/') + '/v1/chat/completions', json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = strip_thinking(data['choices'][0]['message']['content'])
            parsed = json.loads(content)
            items = parsed.get('items') if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ValueError('missing_items_array')
            valid = {}
            for item in items:
                if isinstance(item, dict):
                    v = validate_item(item, expected)
                    if v:
                        valid[v['account_id']] = v
            if not valid:
                raise ValueError('no_valid_items')
            out = []
            for rec in records:
                aid = str((rec.get('account') or rec).get('account_id') or '')
                out.append(valid.get(aid) or fallback([rec], 'MISSING_ITEM')[0])
            return out
        except Exception as exc:
            last_error = f'{type(exc).__name__}:{str(exc)[:160]}'
            body['messages'][1]['content'] = '/no_think\nReturn only JSON. ' + user
            body['max_tokens'] = min(int(body['max_tokens']), 1800)
    return fallback(records, last_error)
