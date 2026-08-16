#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import time
from pathlib import Path
from typing import Any

from hospitality_intelligence_db import asset_id_for_url, utcnow
from hospitality_portfolio_expand import expand_account
from hospitality_qwen_classifier import classify_batch
from hospitality_search_fabric import SearchFabric, query_specs


def load_config(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    with Path(path).open(encoding='utf-8') as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def contactability(account: dict[str, Any], first: dict[str, Any]) -> int:
    raw = account.get('raw') or {}
    email = account.get('public_email') or first.get('public_email')
    phone = account.get('public_phone')
    ig = account.get('instagram') or raw.get('instagram') or first.get('instagram')
    fb = raw.get('facebook') or first.get('facebook')
    wa = raw.get('whatsapp') or first.get('whatsapp')
    cp = raw.get('contact_page') or first.get('contact_page')
    score = 0
    score += 28 if email else 0
    score += 20 if phone else 0
    score += 18 if ig else 0
    score += 10 if fb else 0
    score += 12 if wa else 0
    score += 12 if cp else 0
    return min(100, score)


def leverage(property_count: int, operator_score: int) -> int:
    n = max(0, int(property_count or 0))
    if n >= 100: base = 100
    elif n >= 50: base = 95
    elif n >= 25: base = 85
    elif n >= 10: base = 72
    elif n >= 5: base = 55
    elif n >= 2: base = 35
    elif n == 1: base = 20
    else: base = 10
    return min(100, round(base * 0.8 + min(100, int(operator_score or 0)) * 0.2))


def commercial_score(contact_score: int, leverage_score: int, premium_score: int, operator_score: int, fit_decision: str) -> int:
    base = (
        0.30 * contact_score + 0.35 * leverage_score +
        0.18 * min(100, int(premium_score or 0)) + 0.17 * min(100, int(operator_score or 0))
    )
    bonus = {'STRONG_FIT': 8, 'FIT': 4, 'MAYBE': 0, 'REJECT_OBVIOUS': -20}.get(fit_decision, 0)
    return max(0, min(100, round(base + bonus)))


def needs_search(account: dict[str, Any], portfolio: dict[str, Any]) -> bool:
    raw = account.get('raw') or {}
    first = portfolio.get('first_party') or {}
    has_email = bool(account.get('public_email') or first.get('public_email'))
    has_social = bool(account.get('instagram') or raw.get('instagram') or first.get('instagram') or raw.get('facebook') or first.get('facebook'))
    has_contact = bool(raw.get('contact_page') or first.get('contact_page'))
    return (not has_email) or (not has_social) or (not has_contact) or portfolio.get('status') != 'OK'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--config', required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--qwen-url', default='http://127.0.0.1:8080')
    ap.add_argument('--shard', default='0')
    a = ap.parse_args()

    cfg = load_config(a.config)
    records = read_jsonl(a.input)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    now = utcnow()

    portfolio_cfg = cfg.get('portfolio') or {}
    fetch_workers = max(1, int(portfolio_cfg.get('workers') or 16))
    enriched: list[dict[str, Any] | None] = [None] * len(records)

    def do_expand(idx_rec: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        idx, rec = idx_rec
        try:
            return idx, expand_account(rec, portfolio_cfg)
        except Exception as exc:
            return idx, {'status': 'PARTIAL', 'assets': [], 'pms_fingerprints': [], 'first_party': {}, 'fetch_errors': [f'{type(exc).__name__}:{str(exc)[:120]}']}

    with cf.ThreadPoolExecutor(max_workers=fetch_workers) as ex:
        for idx, portfolio in ex.map(do_expand, enumerate(records)):
            enriched[idx] = {'account': records[idx], 'portfolio': portfolio, 'search_results': [], 'search_events': [], 'search_deferred': False}

    search_cfg = cfg.get('search') or {}
    fabric = SearchFabric(search_cfg)
    search_budget = max(0, int(search_cfg.get('account_budget_per_worker') or 60))
    max_queries = max(0, int(search_cfg.get('max_queries_per_account') or 3))
    searched_accounts = 0
    for bundle in enriched:
        assert bundle is not None
        account = bundle['account']
        portfolio = bundle['portfolio']
        if not needs_search(account, portfolio):
            continue
        if searched_accounts >= search_budget:
            bundle['search_deferred'] = True
            continue
        searched_accounts += 1
        for family, query in query_specs(account, max_queries=max_queries):
            results, events = fabric.search(family, query)
            for r in results:
                r['account_id'] = account['account_id']
            for e in events:
                e['account_id'] = account['account_id']
                e['query_text'] = query
            bundle['search_results'].extend(results)
            bundle['search_events'].extend(events)

    qcfg = cfg.get('qwen') or {}
    qwen_enabled = bool(qcfg.get('enabled', True))
    qbatch = max(1, int(qcfg.get('batch_size') or 8))
    model_label = str(qcfg.get('model_label') or 'qwen3-4b-q4_k_m')
    classifications: dict[str, dict[str, Any]] = {}
    if qwen_enabled:
        for i in range(0, len(enriched), qbatch):
            batch = [x for x in enriched[i:i+qbatch] if x is not None]
            for result in classify_batch(batch, a.qwen_url, model_label, timeout=float(qcfg.get('timeout_seconds') or 150)):
                classifications[str(result.get('account_id') or '')] = result
    else:
        for bundle in enriched:
            assert bundle is not None
            aid = bundle['account']['account_id']
            classifications[aid] = {
                'account_id': aid, 'entity_match': 'UNCERTAIN', 'business_type': 'UNCERTAIN',
                'fit_decision': 'MAYBE', 'confidence': 0.0, 'unusual_or_novel': False,
                'matching_evidence': [], 'contradictions': [], 'reason': 'Qwen disabled', '_classifier_error': 'QWEN_DISABLED'
            }

    account_rows = []
    asset_rows = []
    search_rows = []
    event_rows = []
    qwen_ok = 0
    qwen_unavailable = 0
    errors = 0
    for bundle in enriched:
        assert bundle is not None
        account = bundle['account']
        portfolio = bundle['portfolio']
        aid = account['account_id']
        c = classifications.get(aid) or {}
        classifier_error = str(c.get('_classifier_error') or '')
        if classifier_error:
            qwen_unavailable += 1
        else:
            qwen_ok += 1
        first = portfolio.get('first_party') or {}
        cs = contactability(account, first)
        ls = leverage(int(portfolio.get('property_count_known') or 0), int(account.get('operator_score') or 0))
        fit = str(c.get('fit_decision') or 'MAYBE')
        overall = commercial_score(cs, ls, int(account.get('premium_score') or 0), int(account.get('operator_score') or 0), fit)
        fetch_errors = portfolio.get('fetch_errors') or []
        if fetch_errors:
            errors += 1
        status = 'READY_SHADOW'
        if classifier_error:
            status = 'QWEN_UNAVAILABLE'
        elif bundle['search_deferred']:
            status = 'PARTIAL'
        elif portfolio.get('status') not in {'OK'}:
            status = 'PARTIAL'

        raw = account.get('raw') or {}
        account_rows.append({
            'account_id': aid,
            'domain': account.get('domain'),
            'name': account.get('name'),
            'country': account.get('country'),
            'region': account.get('region'),
            'city': account.get('city'),
            'website': account.get('website'),
            'first_seen': account.get('first_seen') or now,
            'last_seen': account.get('last_seen') or now,
            'material_hash': account.get('material_hash'),
            'queue_reason': account.get('queue_reason'),
            'entity_match': c.get('entity_match') or 'UNCERTAIN',
            'business_type': c.get('business_type') or 'UNCERTAIN',
            'fit_decision': fit,
            'confidence': float(c.get('confidence') or 0),
            'unusual_or_novel': bool(c.get('unusual_or_novel')),
            'matching_evidence': c.get('matching_evidence') or [],
            'contradictions': c.get('contradictions') or [],
            'classification_reason': c.get('reason') or '',
            'classifier_error': classifier_error,
            'public_email': account.get('public_email') or first.get('public_email') or '',
            'public_email_source_url': (portfolio.get('homepage_final_url') or account.get('website')) if first.get('public_email') else '',
            'public_phone': account.get('public_phone') or '',
            'instagram': account.get('instagram') or raw.get('instagram') or first.get('instagram') or '',
            'instagram_first_party': first.get('instagram') or '',
            'facebook': raw.get('facebook') or first.get('facebook') or '',
            'facebook_first_party': first.get('facebook') or '',
            'whatsapp': raw.get('whatsapp') or first.get('whatsapp') or '',
            'whatsapp_first_party': first.get('whatsapp') or '',
            'contact_page': raw.get('contact_page') or first.get('contact_page') or '',
            'contact_page_first_party': first.get('contact_page') or '',
            'portfolio_url': raw.get('portfolio_url') or first.get('portfolio_url') or '',
            'portfolio_url_first_party': first.get('portfolio_url') or '',
            'pms_fingerprints': portfolio.get('pms_fingerprints') or [],
            'property_count_known': int(portfolio.get('property_count_known') or 0),
            'portfolio_hash': portfolio.get('portfolio_hash') or '',
            'sample_property_urls': portfolio.get('sample_property_urls') or [],
            'contactability_score': cs,
            'portfolio_leverage_score': ls,
            'commercial_score': overall,
            'search_deferred': bool(bundle['search_deferred']),
            'search_results_count': len(bundle['search_results']),
            'homepage_status': portfolio.get('homepage_status') or 0,
            'pms_detected': bool(portfolio.get('pms_fingerprints')),
            'status': status,
            'fetch_errors': fetch_errors,
            'classifier_model': model_label,
            'classifier_prompt_version': str(qcfg.get('prompt_version') or 'hospitality-v2-high-recall-1'),
            'processed_at': now,
        })
        for asset in portfolio.get('assets') or []:
            u = str(asset.get('url') or '').strip()
            if not u:
                continue
            asset_rows.append({
                'asset_id': asset_id_for_url(aid, u), 'account_id': aid,
                'property_name': asset.get('property_name') or '', 'url': u,
                'source_type': asset.get('source_type') or 'sitemap_or_first_party',
                'sample_priority': int(asset.get('sample_priority') or 0), 'seen_at': now,
            })
        search_rows.extend(bundle['search_results'])
        event_rows.extend(bundle['search_events'])

    def write_gz(name: str, rows: list[dict[str, Any]]) -> None:
        with gzip.open(out / name, 'wt', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')

    write_gz('accounts.jsonl.gz', account_rows)
    write_gz('assets.jsonl.gz', asset_rows)
    write_gz('search-results.jsonl.gz', search_rows)
    (out / 'search-events.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in event_rows), encoding='utf-8')
    summary = {
        'shard': str(a.shard), 'records_input': len(records), 'accounts_processed': len(account_rows),
        'qwen_classified': qwen_ok, 'qwen_unavailable': qwen_unavailable,
        'search_accounts': searched_accounts, 'search_queries_provider_events': len(event_rows),
        'search_results': len(search_rows), 'assets_found': len(asset_rows), 'errors': errors,
        'elapsed_seconds': round(time.time() - started, 2), 'finished_at': utcnow(),
    }
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
