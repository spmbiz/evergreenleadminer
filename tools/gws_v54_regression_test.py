#!/usr/bin/env python3
from __future__ import annotations

import inspect

import gws_no_website_certifier_v53 as prod
import gws_search_provider_pool_v54 as providers
import gws_worker_v54 as worker_policy


def main():
    # Belgian national and E.164 forms of the same full number must intersect.
    local = prod.phone_keys('02 521 58 59')
    e164 = prod.phone_keys('+32 2 521 58 59')
    intl00 = prod.phone_keys('0032 2 521 58 59')
    assert local & e164, (local, e164)
    assert local & intl00, (local, intl00)
    # Never use unsafe suffix-only phone equivalence.
    assert not (prod.phone_keys('0471 11 22 33') & prod.phone_keys('02 11 22 33'))

    # A complete source candidate that Overture cannot strongly resolve must
    # continue to web challenge, not terminate before search.
    c = {'r': 1, 'n': 'Acme Brussels', 'p': '1050', 'a': 'Rue Test 1', 'ph': '02 555 12 12', 'em': '', 'cow': ''}
    pe = {'resolved': False, 'overture_id': 'weak-best-guess', 'overture_name': 'Acme'}
    assert prod.preclassify_hardened(c, None, pe, True) is None

    # But unresolved identity can never satisfy the HIGH certificate gate.
    w = {
        'healthy_providers': ['bing', 'ddg'], 'search_queries': 2,
        'search_usable_queries': 2, 'direct_checked': 5,
        'direct_health': [
            {'seed': f'https://x{i}.be/', 'final': f'https://x{i}.be/', 'status': 404, 'ok': False, 'dns_negative': True}
            for i in range(5)
        ],
        'owned': '',
    }
    cert = prod.v5.certificate(c, pe, w, w)
    assert cert['gates']['current_identity_strong'] is False
    assert cert['verified'] is False

    # The same weak/unresolved Overture best guess must NOT merge distinct rows.
    a = {'r': 10, 'candidate': {'n': 'Alpha Architect', 'p': '1070', 'a': 'Rue A 1', 'ph': ''}, 'place': {'resolved': False, 'overture_id': 'weak-shared'}}
    b = {'r': 11, 'candidate': {'n': 'Beta Studio', 'p': '1070', 'a': 'Rue B 9', 'ph': ''}, 'place': {'resolved': False, 'overture_id': 'weak-shared'}}
    assert prod.canonical_key_hardened(a) != prod.canonical_key_hardened(b)
    # Once both rows are strongly resolved to the same Overture entity, they must merge.
    a['place']['resolved'] = True; b['place']['resolved'] = True
    assert prod.canonical_key_hardened(a) == prod.canonical_key_hardened(b) == 'o:weak-shared'

    # Runtime regression: never serialize every search transport behind one global
    # per-worker semaphore again. DDG stays conservative, while independent
    # transports may use the configured bounded concurrency.
    limits = providers.provider_concurrency_plan(2)
    assert limits == {'bing': 2, 'yahoo': 2, 'ddg': 1}, limits
    assert providers.provider_family('yahoo') == 'bing'
    assert providers.provider_family('ddg_lite') == 'ddg'

    # Runtime integrity regression: worker policy must persist durable partial
    # results/progress before the expensive web stage and after each web batch.
    src = inspect.getsource(worker_policy.worker)
    assert 'partial_results.jsonl' in inspect.getsource(worker_policy._checkpoint)
    assert 'progress.json' in inspect.getsource(worker_policy._checkpoint)
    assert 'GWS_V54_PROGRESS=' in inspect.getsource(worker_policy._checkpoint)
    assert 'GWS_WEB_BATCH_SIZE' in src
    assert 'stage="resolved"' in src
    assert 'stage="web_batch_complete"' in src

    print('GWS_V54_REGRESSION_OK')


if __name__ == '__main__':
    main()
