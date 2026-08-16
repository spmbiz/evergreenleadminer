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
Your job is NOT to be aggressive. Preserve plausible commercial opportunities.

Rules:
- Operators, villa managers, vacation-rental agencies, holiday-home operators, boutique hotels, resorts, serviced accommodation and multi-property managers are valuable.
- A novel, unusual, unclear, brokerable, premium or potentially multi-property business should survive as FIT or MAYBE.
- REJECT_OBVIOUS only when supplied evidence clearly shows the business is unrelated to short-stay/hospitality opportunity, or clearly generic long-term property management with no short-stay evidence.
- Insufficient evidence means MAYBE/UNCERTAIN, not rejection.
- Never invent contact information, property counts, company identity, location, services or facts.
- Use only supplied evidence.
- Entity: MATCH, PROBABLE, WRONG, UNCERTAIN.
- Business: SHORT_STAY_OPERATOR, PROPERTY_MANAGER_SHORT_STAY, BOUTIQUE_HOTEL, RESORT, SERVICED_ACCOMMODATION, OTHER_HOSPITALITY, LONG_TERM_PROPERTY_MANAGEMENT, UNRELATED, UNCERTAIN.
- Fit: STRONG_FIT, FIT, MAYBE, REJECT_OBVIOUS.
Return ONLY JSON: {"items":[{"account_id":"...","entity_match":"...","business_type":"...","fit_decision":"...","confidence":0.0,"unusual_or_novel":false,"matching_evidence":["..."],"contradictions":["..."],"reason":"short evidence-grounded reason"}]}
Return exactly one item per input account_id.'''


def _short(value: Any, limit: int) -> str:
    return str(value or '')[:limit]


def compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    account = rec.get('account') or rec
    portfolio = rec.get('portfolio') or {}
    search = rec.get('search_results') or []
    raw = account.get('raw') or {}
    first = portfolio.get('first_party') or {}
    return {
        'account_id': _short(account.get('account_id'), 160),
        'name': _short(account.get('name'), 220),
        'country': _short(account.get('country'), 80),
        'region': _short(account.get('region'), 120),
        'city': _short(account.get('city'), 120),
        'website': _short(account.get('website'), 300),
        'public_email_present': bool(account.get('public_email')),
        'public_phone_present': bool(account.get('public_phone')),
        'existing_fit_tier': _short(account.get('fit_tier'), 40),
        'operator_score': account.get('operator_score'),
        'premium_score': account.get('premium_score'),
        # Keep batches inside the 8k llama.cpp context even when Search Fabric
        # returns verbose snippets. Full evidence stays in the durable ledger.
        'homepage_excerpt': _short(portfolio.get('homepage_excerpt'), 1100),
        'property_count_from_public_urls': int(portfolio.get('property_count_known') or 0),
        'pms_fingerprints': [_short(x, 80) for x in (portfolio.get('pms_fingerprints') or [])[:8]],
        'first_party_links': {str(k)[:80]: _short(v, 300) for k, v in list(first.items())[:10]},
        'search_results': [
            {
                'provider': _short(x.get('provider'), 40),
                'query_family': _short(x.get('query_family'), 60),
                'title': _short(x.get('title'), 160),
                'url': _short(x.get('url'), 300),
                'snippet': _short(x.get('snippet'), 260),
            }
            for x in search[:4]
        ],
        'source_categories': _short(raw.get('categories') or raw.get('category'), 300),
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
        'reason': f'Classifier unavailable or invalid: {error}'[:700], '_classifier_error': error[:500],
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


def _request_once(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float, constrained: bool) -> tuple[dict[str, dict[str, Any]] | None, str]:
    expected = {str((r.get('account') or r).get('account_id') or '') for r in records}
    payload_records = [compact_record(r) for r in records]
    user = 'Classify these hospitality accounts. Preserve uncertain and novel opportunities. Return only the requested JSON.\nINPUT=' + json.dumps(payload_records, ensure_ascii=False, separators=(',', ':'))
    body: dict[str, Any] = {
        'model': model_label,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': user}],
        'temperature': 0.2,
        'top_p': 0.8,
        'max_tokens': max(450, min(1800, 180 * len(records) + 260)),
        'stream': False,
    }
    # llama-server is already started with --reasoning off. Keep the first
    # request deliberately minimal for maximum OpenAI-route compatibility.
    if constrained:
        body['response_format'] = {'type': 'json_object'}
    try:
        r = requests.post(base_url.rstrip('/') + '/v1/chat/completions', json=body, timeout=timeout)
        if r.status_code >= 400:
            detail = _short(getattr(r, 'text', ''), 320).replace('\n', ' ')
            return None, f'HTTP_{r.status_code}:{detail}'
        data = r.json()
        return _parse_response(data, expected), ''
    except Exception as exc:
        return None, f'{type(exc).__name__}:{str(exc)[:320]}'


def _classify_adaptive(records: list[dict[str, Any]], base_url: str, model_label: str, timeout: float) -> list[dict[str, Any]]:
    if not records:
        return []

    # 1) Minimal OpenAI-compatible request. 2) JSON-constrained retry if the
    # model answered but formatting was bad. Both profiles are supported by
    # current llama.cpp; minimal first avoids optional-field regressions.
    valid, err1 = _request_once(records, base_url, model_label, timeout, constrained=False)
    if valid is None and not err1.startswith('HTTP_4'):
        valid, err2 = _request_once(records, base_url, model_label, timeout, constrained=True)
    elif valid is None and ('json' in err1.lower() or 'parse' in err1.lower()):
        valid, err2 = _request_once(records, base_url, model_label, timeout, constrained=True)
    else:
        err2 = ''

    if valid is not None:
        out = []
        for rec in records:
            aid = str((rec.get('account') or rec).get('account_id') or '')
            out.append(valid.get(aid) or fallback([rec], 'MISSING_ITEM')[0])
        return out

    # A 400 on a multi-account batch is commonly context/grammar pressure.
    # Split recursively rather than marking the entire shard unavailable.
    if len(records) > 1:
        mid = max(1, len(records) // 2)
        return _classify_adaptive(records[:mid], base_url, model_label, timeout) + _classify_adaptive(records[mid:], base_url, model_label, timeout)

    # Last single-record attempt with JSON constraint covers a model that
    # requires grammar to emit parseable JSON but rejects larger batches.
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
