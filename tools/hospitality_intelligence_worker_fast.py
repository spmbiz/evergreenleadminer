#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import hospitality_intelligence_worker as base

_ORIGINAL_CLASSIFY_BATCH = base.classify_batch
_FAST_CFG: dict[str, Any] = {}


def _arg_value(name: str) -> str:
    try:
        idx = sys.argv.index(name)
        return sys.argv[idx + 1]
    except Exception:
        return ''


def _load_fast_cfg() -> dict[str, Any]:
    cfg_path = _arg_value('--config')
    if not cfg_path:
        return {}
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding='utf-8'))
        return dict(cfg.get('deterministic_fast_path') or {})
    except Exception:
        return {}


def deterministic_classification(bundle: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return only high-confidence positive classifications.

    This path deliberately never emits a negative/reject decision. Ambiguous,
    weak, contradictory or under-evidenced accounts continue to Qwen.
    """
    cfg = dict(cfg or {})
    if not bool(cfg.get('enabled', False)):
        return None
    if not bool(cfg.get('positive_only', True)):
        return None

    account = bundle.get('account') or {}
    portfolio = bundle.get('portfolio') or {}
    aid = str(account.get('account_id') or '')
    if not aid or not account.get('domain') or not account.get('website'):
        return None

    homepage_status = int(portfolio.get('homepage_status') or 0)
    if bool(cfg.get('require_live_homepage', True)) and not (200 <= homepage_status < 400):
        return None

    operator_score = int(account.get('operator_score') or 0)
    premium_score = int(account.get('premium_score') or 0)
    fit_tier = str(account.get('fit_tier') or '').strip().upper()
    property_count = int(portfolio.get('property_count_known') or 0)
    pms = list(portfolio.get('pms_fingerprints') or [])

    min_operator = int(cfg.get('min_operator_score') or 80)
    min_premium = int(cfg.get('min_premium_score') or 60)
    min_properties = int(cfg.get('min_property_count') or 2)
    strong_properties = int(cfg.get('strong_property_count') or 5)
    allow_pms = bool(cfg.get('allow_pms_as_portfolio_evidence', True))

    portfolio_evidence = property_count >= min_properties or (allow_pms and bool(pms))
    canonical_positive = fit_tier in {'A', 'A+', 'HIGH', 'PREMIUM'}
    commercial_positive = operator_score >= min_operator and (premium_score >= min_premium or canonical_positive)
    if not portfolio_evidence or not commercial_positive:
        return None

    strong = (
        property_count >= strong_properties
        or (bool(pms) and operator_score >= max(85, min_operator))
        or (canonical_positive and operator_score >= 90)
    )
    decision = 'STRONG_FIT' if strong else 'FIT'
    confidence = 0.97 if strong else 0.93
    evidence = [
        f'live_first_party_homepage:{homepage_status}',
        f'operator_score:{operator_score}',
        f'premium_score:{premium_score}',
        f'property_count_known:{property_count}',
    ]
    if canonical_positive:
        evidence.append(f'canonical_fit_tier:{fit_tier}')
    if pms:
        evidence.append('pms:' + ','.join(str(x) for x in pms[:4]))

    return {
        'account_id': aid,
        'entity_match': 'MATCH',
        'business_type': 'SHORT_STAY_OPERATOR',
        'fit_decision': decision,
        'confidence': confidence,
        'unusual_or_novel': False,
        'matching_evidence': evidence,
        'contradictions': [],
        'reason': 'deterministic_fast_path: verified first-party portfolio + strong canonical commercial signals',
        '_classifier_error': '',
        '_classifier_model': str(cfg.get('model_label') or 'hospitality-deterministic-positive-v1'),
    }


def selective_classify_batch(batch, qwen_url, model_label, timeout=90):
    deterministic = []
    ambiguous = []
    for bundle in batch:
        result = deterministic_classification(bundle, _FAST_CFG)
        if result is None:
            ambiguous.append(bundle)
        else:
            deterministic.append(result)
    qwen_results = []
    if ambiguous:
        qwen_results = list(_ORIGINAL_CLASSIFY_BATCH(ambiguous, qwen_url, model_label, timeout=timeout))
    return deterministic + qwen_results


def _postprocess_metrics() -> None:
    outdir = _arg_value('--outdir')
    if not outdir:
        return
    out = Path(outdir)
    accounts_path = out / 'accounts.jsonl.gz'
    summary_path = out / 'summary.json'
    if not accounts_path.is_file() or not summary_path.is_file():
        return

    rows = []
    deterministic_count = 0
    fast_model = str(_FAST_CFG.get('model_label') or 'hospitality-deterministic-positive-v1')
    with gzip.open(accounts_path, 'rt', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get('classification_reason') or '').startswith('deterministic_fast_path:'):
                deterministic_count += 1
                row['classifier_model'] = fast_model
                row['classifier_prompt_version'] = 'deterministic-positive-v1'
            rows.append(row)
    with gzip.open(accounts_path, 'wt', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    previously_successful = int(summary.get('qwen_classified') or 0)
    qwen_unavailable = int(summary.get('qwen_unavailable') or 0)
    actual_qwen = max(0, previously_successful - deterministic_count)
    summary['deterministic_classified'] = deterministic_count
    summary['qwen_classified'] = actual_qwen
    summary['qwen_skipped_fast_path'] = deterministic_count
    summary['qwen_attempted'] = actual_qwen + qwen_unavailable
    summary['semantic_classified_total'] = actual_qwen + deterministic_count
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'fast_path': deterministic_count, 'qwen_actual': actual_qwen, 'qwen_unavailable': qwen_unavailable}))


def main() -> None:
    global _FAST_CFG
    _FAST_CFG = _load_fast_cfg()
    base.classify_batch = selective_classify_batch
    base.main()
    _postprocess_metrics()


if __name__ == '__main__':
    main()
