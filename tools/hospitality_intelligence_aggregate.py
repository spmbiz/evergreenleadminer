#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from hospitality_intelligence_db import connect, search_id_for_result, utcnow


def iter_jsonl_gz(root: Path, name: str):
    for p in root.rglob(name):
        with gzip.open(p, 'rt', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def iter_jsonl(root: Path, name: str):
    for p in root.rglob(name):
        with p.open(encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--results-root', required=True)
    ap.add_argument('--intelligence-db', required=True)
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--outdir', required=True)
    a = ap.parse_args()

    root = Path(a.results_root)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    con = connect(a.intelligence_db)
    account_rows = list(iter_jsonl_gz(root, 'accounts.jsonl.gz'))
    asset_rows = list(iter_jsonl_gz(root, 'assets.jsonl.gz'))
    search_rows = list(iter_jsonl_gz(root, 'search-results.jsonl.gz'))
    search_events = list(iter_jsonl(root, 'search-events.jsonl'))
    worker_summaries = []
    for p in root.rglob('summary.json'):
        try:
            worker_summaries.append(json.loads(p.read_text(encoding='utf-8')))
        except Exception:
            pass

    canonical_delta = []
    review = []
    for r in account_rows:
        aid = str(r.get('account_id') or '')
        domain = str(r.get('domain') or '').strip().lower()
        if not aid or not domain:
            continue
        old = con.execute('SELECT first_seen,material_hash,last_material_change_at FROM accounts WHERE account_id=?', (aid,)).fetchone()
        first_seen = (old['first_seen'] if old else None) or r.get('first_seen') or now
        prior_hash = (old['material_hash'] if old else '') or ''
        new_hash = str(r.get('material_hash') or '')
        changed_at = (old['last_material_change_at'] if old else None) or now
        if prior_hash and new_hash and prior_hash != new_hash:
            changed_at = now
        classifier_error = str(r.get('classifier_error') or '')
        last_classified = None if classifier_error else str(r.get('processed_at') or now)
        raw_payload = {
            'matching_evidence': r.get('matching_evidence') or [],
            'contradictions': r.get('contradictions') or [],
            'classification_reason': r.get('classification_reason') or '',
            'fetch_errors': r.get('fetch_errors') or [],
            'queue_reason': r.get('queue_reason') or '',
            'search_deferred': bool(r.get('search_deferred')),
        }
        con.execute('''
          INSERT INTO accounts(
            account_id,domain,name,country,region,city,website,first_seen,last_seen,material_hash,last_material_change_at,
            last_attempt_at,last_classified_at,classifier_model,classifier_prompt_version,entity_match,business_type,fit_decision,
            confidence,unusual_or_novel,public_email,public_phone,instagram,facebook,whatsapp,contact_page,portfolio_url,
            pms_fingerprints_json,property_count_known,portfolio_hash,sample_property_urls_json,contactability_score,
            portfolio_leverage_score,commercial_score,status,last_error,raw_json
          ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(account_id) DO UPDATE SET
            domain=excluded.domain,name=excluded.name,country=excluded.country,region=excluded.region,city=excluded.city,
            website=excluded.website,last_seen=excluded.last_seen,material_hash=excluded.material_hash,
            last_material_change_at=excluded.last_material_change_at,last_attempt_at=excluded.last_attempt_at,
            last_classified_at=COALESCE(excluded.last_classified_at,accounts.last_classified_at),classifier_model=excluded.classifier_model,
            classifier_prompt_version=excluded.classifier_prompt_version,entity_match=excluded.entity_match,business_type=excluded.business_type,
            fit_decision=excluded.fit_decision,confidence=excluded.confidence,unusual_or_novel=excluded.unusual_or_novel,
            public_email=COALESCE(NULLIF(excluded.public_email,''),accounts.public_email),
            public_phone=COALESCE(NULLIF(excluded.public_phone,''),accounts.public_phone),
            instagram=COALESCE(NULLIF(excluded.instagram,''),accounts.instagram),
            facebook=COALESCE(NULLIF(excluded.facebook,''),accounts.facebook),
            whatsapp=COALESCE(NULLIF(excluded.whatsapp,''),accounts.whatsapp),
            contact_page=COALESCE(NULLIF(excluded.contact_page,''),accounts.contact_page),
            portfolio_url=COALESCE(NULLIF(excluded.portfolio_url,''),accounts.portfolio_url),
            pms_fingerprints_json=excluded.pms_fingerprints_json,property_count_known=MAX(accounts.property_count_known,excluded.property_count_known),
            portfolio_hash=COALESCE(NULLIF(excluded.portfolio_hash,''),accounts.portfolio_hash),
            sample_property_urls_json=excluded.sample_property_urls_json,contactability_score=excluded.contactability_score,
            portfolio_leverage_score=excluded.portfolio_leverage_score,commercial_score=excluded.commercial_score,status=excluded.status,
            last_error=excluded.last_error,raw_json=excluded.raw_json
        ''', (
            aid, domain, r.get('name'), r.get('country'), r.get('region'), r.get('city'), r.get('website'), first_seen,
            r.get('last_seen') or now, new_hash, changed_at, r.get('processed_at') or now, last_classified,
            r.get('classifier_model'), r.get('classifier_prompt_version'), r.get('entity_match'), r.get('business_type'),
            r.get('fit_decision'), float(r.get('confidence') or 0), 1 if r.get('unusual_or_novel') else 0,
            r.get('public_email') or '', r.get('public_phone') or '', r.get('instagram') or '', r.get('facebook') or '',
            r.get('whatsapp') or '', r.get('contact_page') or '', r.get('portfolio_url') or '',
            json.dumps(r.get('pms_fingerprints') or [], ensure_ascii=False), int(r.get('property_count_known') or 0),
            r.get('portfolio_hash') or '', json.dumps(r.get('sample_property_urls') or [], ensure_ascii=False),
            int(r.get('contactability_score') or 0), int(r.get('portfolio_leverage_score') or 0), int(r.get('commercial_score') or 0),
            r.get('status') or 'PARTIAL', classifier_error, json.dumps(raw_payload, ensure_ascii=False)
        ))

        canonical_delta.append({
            'domain': domain,
            'public_email': r.get('public_email') if r.get('public_email_source_url') else '',
            'public_email_source_url': r.get('public_email_source_url') or '',
            'instagram': r.get('instagram_first_party') or '',
            'facebook': r.get('facebook_first_party') or '',
            'whatsapp': r.get('whatsapp_first_party') or '',
            'contact_page': r.get('contact_page_first_party') or '',
            'portfolio_url': r.get('portfolio_url_first_party') or '',
            'account_id': aid,
            'intelligence_business_type': r.get('business_type') or 'UNCERTAIN',
            'intelligence_fit_decision': r.get('fit_decision') or 'MAYBE',
            'intelligence_confidence': float(r.get('confidence') or 0),
            'intelligence_status': r.get('status') or 'PARTIAL',
            'pms_fingerprints': r.get('pms_fingerprints') or [],
            'property_count_known': int(r.get('property_count_known') or 0),
            'sample_property_urls': r.get('sample_property_urls') or [],
            'contactability_score': int(r.get('contactability_score') or 0),
            'portfolio_leverage_score': int(r.get('portfolio_leverage_score') or 0),
            'commercial_score': int(r.get('commercial_score') or 0),
            'intelligence_processed_at': r.get('processed_at') or now,
        })
        if (
            r.get('fit_decision') in {'STRONG_FIT', 'FIT'} or r.get('unusual_or_novel') or
            int(r.get('commercial_score') or 0) >= 70 or r.get('entity_match') == 'UNCERTAIN' or float(r.get('confidence') or 0) < 0.65
        ):
            review.append(r)

    for r in asset_rows:
        aid = str(r.get('account_id') or '')
        asset_id = str(r.get('asset_id') or '')
        url = str(r.get('url') or '')
        if not aid or not asset_id or not url:
            continue
        seen = str(r.get('seen_at') or now)
        con.execute('''
          INSERT INTO assets(asset_id,account_id,property_name,url,source_type,first_seen,last_seen,content_hash,sample_priority,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(asset_id) DO UPDATE SET property_name=excluded.property_name,last_seen=excluded.last_seen,
            sample_priority=MAX(assets.sample_priority,excluded.sample_priority),raw_json=excluded.raw_json
        ''', (asset_id, aid, r.get('property_name') or '', url, r.get('source_type') or '', seen, seen, '', int(r.get('sample_priority') or 0), json.dumps(r, ensure_ascii=False)))

    for r in search_rows:
        aid = str(r.get('account_id') or '')
        provider = str(r.get('provider') or '')
        family = str(r.get('query_family') or '')
        query = str(r.get('query_text') or '')
        url = str(r.get('url') or '')
        rank = int(r.get('rank') or 0)
        if not aid or not provider or not url:
            continue
        sid = search_id_for_result(aid, provider, family, query, url, rank)
        con.execute('''INSERT OR REPLACE INTO search_results(search_id,account_id,query_family,query_text,provider,rank,title,url,snippet,retrieved_at,status,error,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (sid, aid, family, query, provider, rank, r.get('title') or '', url, r.get('snippet') or '', now, r.get('status') or 'OK', r.get('error') or '', json.dumps(r, ensure_ascii=False)))

    for e in search_events:
        provider = str(e.get('provider') or 'unknown')
        family = str(e.get('query_family') or 'unknown')
        status = str(e.get('status') or 'ERROR')
        results = int(e.get('results') or 0)
        con.execute('''INSERT INTO query_yield(provider,query_family,queries,successes,errors,results,useful_results,updated_at)
          VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(provider,query_family) DO UPDATE SET queries=query_yield.queries+1,
          successes=query_yield.successes+excluded.successes,errors=query_yield.errors+excluded.errors,
          results=query_yield.results+excluded.results,updated_at=excluded.updated_at''',
          (provider, family, 1, 1 if status == 'OK' else 0, 0 if status in {'OK','DISABLED'} else 1, results, 0, now))

    totals = {
        'accounts_processed': len(account_rows),
        'assets_found': len(asset_rows),
        'search_results': len(search_rows),
        'search_provider_events': len(search_events),
        'qwen_classified': sum(int(x.get('qwen_classified') or 0) for x in worker_summaries),
        'qwen_unavailable': sum(int(x.get('qwen_unavailable') or 0) for x in worker_summaries),
        'worker_errors': sum(int(x.get('errors') or 0) for x in worker_summaries),
        'gpt_review_candidates': len(review),
    }
    con.execute('''INSERT OR REPLACE INTO runs(run_id,started_at,finished_at,accounts_planned,accounts_processed,qwen_classified,qwen_unavailable,search_queries,assets_found,errors,metrics_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (a.run_id, None, now, len(account_rows), len(account_rows), totals['qwen_classified'], totals['qwen_unavailable'], len(search_events), len(asset_rows), totals['worker_errors'], json.dumps(totals)))
    con.commit()
    con.close()

    with gzip.open(out / 'canonical-intelligence-delta.jsonl.gz', 'wt', encoding='utf-8') as f:
        for r in canonical_delta:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    review.sort(key=lambda r: (-int(r.get('commercial_score') or 0), -float(r.get('confidence') or 0)))
    with (out / 'gpt-review.jsonl').open('w', encoding='utf-8') as f:
        for r in review[:1500]:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    (out / 'summary.json').write_text(json.dumps({'run_id': a.run_id, 'finished_at': now, **totals}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'run_id': a.run_id, **totals}, ensure_ascii=False))


if __name__ == '__main__':
    main()
