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

SYSTEM_PROMPT = '''High-recall hospitality sales triage. Preserve plausible opportunities.
Valuable: vacation-rental operators, villa/holiday-home managers, boutique hotels, resorts, serviced accommodation and multi-property hospitality managers.
REJECT_OBVIOUS only with clear supplied evidence of irrelevance. Unclear or insufficient evidence => MAYBE/UNCERTAIN. Never invent facts or contacts.
Entity: MATCH|PROBABLE|WRONG|UNCERTAIN.
Business: SHORT_STAY_OPERATOR|PROPERTY_MANAGER_SHORT_STAY|BOUTIQUE_HOTEL|RESORT|SERVICED_ACCOMMODATION|OTHER_HOSPITALITY|LONG_TERM_PROPERTY_MANAGEMENT|UNRELATED|UNCERTAIN.
Fit: STRONG_FIT|FIT|MAYBE|REJECT_OBVIOUS.
Return ONLY JSON {"items":[...]}. Exactly one item per account_id. For each item: account_id, entity_match, business_type, fit_decision, confidence 0..1, unusual_or_novel, matching_evidence (max 2 short strings), contradictions (max 1 short string), reason (max 14 words).'''


def _short(value: Any, limit: int) -> str:
    return str(value or '')[:limit]


def compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    account = rec.get('account') or rec
    portfolio = rec.get('portfolio') or {}
    search = rec.get('search_results') or []
    raw = account.get('raw') or {}
    first = portfolio.get('first_party') or {}
    keep_link_keys = ('contact_page', 'portfolio_url', 'instagram', 'facebook', 'whatsapp', 'public_email')
    compact_links = {k: _short(first.get(k), 180) for k in keep_link_keys if first.get(k)}
    return {
        'account_id': _short(account.get('account_id'), 160),
        'name': _short(account.get('name'), 180),
        'country': _short(account.get('country'), 60),
        'region': _short(account.get('region'), 90),
        'city': _short(account.get('city'), 90),
        'website': _short(account.get('website'), 220),
        'email': bool(account.get('public_email')),
        'phone': bool(account.get('public_phone')),
        'fit_tier': _short(account.get('fit_tier'), 24),
        'operator_score': account.get('operator_score'),
        'premium_score': account.get('premium_score'),
        'homepage_excerpt': _short(portfolio.get('homepage_excerpt'), 650),
        'public_property_urls': int(portfolio.get('property_count_known') or 0),
        'pms': [_short(x, 60) for x in (portfolio.get('pms_fingerprints') or [])[:5]],
        'first_party': compact_links,
        'search': [
            {
                'p': _short(x.get('provider'), 24),
                'f': _short(x.get('query_family'), 40),
                't': _short(x.get('title'), 110),
                'u': _short(x.get('url'), 220),
                's': _short(x.get('snippet'), 180),
            }
            for x in search[:3]
        ],
        'categories': _short(raw.get('categories') or raw.get('category'), 180),
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
        'matching_evidence': [str(x)[:180] for x in (item.get('matching_evidence') or [])[:2]],
        'contradictions': [str(x)[:180] for x in (item.get('contradictions') or [])[:1]],
        'reason': str(item.get('reason') or '')[:240],
    }


def fallback(records: list[dict[str, Any]], error: str) -> list[dict[str, Any]]:
    return [{
        'account_id': (r.get('account') or r).get('account_id'),
        'entity_match': 'UNCERTAIN', 'business_type': 'UNCERTAIN', 'fit_decision': 'MAYBE',
        'confidence': 0.0, 'unusual_or_novel': False, 'matching_evidence': [], 'contradictions': [],
        'reason': f'Classifier unavailable or invalid: {error}'[:240], '_classifier_error': error[:500],
    } for r in records]


def health(base_url: str, timeout: float = 3) -> bool:
    try:
        return requests.get(base_url.rstrip('/') + '/health', timeout=timeout).status_code == 200
    except Exception:
        return False


def _parse_response(data: dict[str, Any], expected: set[str]) -> dict[str, dict[str, Any]]:
    content = strip_thinking(data['choices'][0]['message']['content'])
    parsed = json.loads(content)
    items = parsed.get('items') if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        raise ValueError('missing_items_array')
    valid: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            v = validate_item(item, expected)
            if v:
                valid[v['account_id']] = v
    if not valid:
        raise ValueError('no_valid_items')
    return valid


def _payload(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    payload_records = [compact_record(r) for r in records]
    encoded = json.dumps(payload_records, ensure_ascii=False, separators=(',', ':'))
    return payload_records, encoded


def _request_once(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float, constrained: bool) -> tuple[dict[str, dict[str, Any]] | None, str]:
    expected = {str((r.get('account') or r).get('account_id') or '') for r in records}
    _, encoded = _payload(records)
    user = 'Classify. Preserve uncertain/novel opportunities. JSON only. INPUT=' + encoded
    body: dict[str, Any] = {
        'model': model_label,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': user}],
        'temperature': 0.15,
        'top_p': 0.8,
        # Keep output bounded: labels + terse evidence are enough for routing.
        'max_tokens': max(260, min(1050, 105 * len(records) + 160)),
        'stream': False,
    }
    if constrained:
        body['response_format'] = {'type': 'json_object'}
    try:
        r = requests.post(base_url.rstrip('/') + '/v1/chat/completions', json=body, timeout=timeout)
        if r.status_code >= 400:
            detail = _short(getattr(r, 'text', ''), 320).replace('\n', ' ')
            return None, f'HTTP_{r.status_code}:{detail}'
        return _parse_response(r.json(), expected), ''
    except Exception as exc:
        return None, f'{type(exc).__name__}:{str(exc)[:320]}'


def _classify_adaptive(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float) -> list[dict[str, Any]]:
    if not records:
        return []

    # Avoid a known-bad oversized request entirely. 18k UTF-8-ish characters
    # leaves ample headroom for system prompt and <=1050 output tokens inside
    # the single 8192-token llama.cpp slot.
    _, encoded = _payload(records)
    if len(records) > 1 and len(encoded) > 18000:
        mid = max(1, len(records) // 2)
        return _classify_adaptive(records[:mid], base_url, model_label, timeout) + _classify_adaptive(records[mid:], base_url, model_label, timeout)

    valid, err1 = _request_once(records, base_url, model_label, timeout, constrained=False)
    err2 = ''
    if valid is None and (not err1.startswith('HTTP_4') or 'json' in err1.lower() or 'parse' in err1.lower()):
        valid, err2 = _request_once(records, base_url, model_label, timeout, constrained=True)

    if valid is not None:
        out = []
        for rec in records:
            aid = str((rec.get('account') or rec).get('account_id') or '')
            out.append(valid.get(aid) or fallback([rec], 'MISSING_ITEM')[0])
        return out

    if len(records) > 1:
        mid = max(1, len(records) // 2)
        return _classify_adaptive(records[:mid], base_url, model_label, timeout) + _classify_adaptive(records[mid:], base_url, model_label, timeout)

    valid, err3 = _request_once(records, base_url, model_label, timeout, constrained=True)
    if valid is not None:
        aid = str((records[0].get('account') or records[0]).get('account_id') or '')
        return [valid.get(aid) or fallback(records, 'MISSING_ITEM')[0]]
    return fallback(records, err3 or err2 or err1 or 'UNKNOWN')


def classify_batch(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float = 120) -> list[dict[str, Any]]:
    if not records:
        return []
    if not base_url or not health(base_url):
        return fallback(records, 'QWEN_UNAVAILABLE')
    return _classify_adaptive(records, base_url, model_label, timeout)
