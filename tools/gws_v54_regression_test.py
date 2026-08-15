#!/usr/bin/env python3
from __future__ import annotations

import inspect

import gws_no_website_certifier_v53 as prod
import gws_search_provider_pool_v54 as providers
import gws_worker_v54 as worker_policy
import gws_identity_resolver_v54 as identity
import gws_legacy_deep_v4 as v4


def main():
    # Belgian national and E.164 forms of the same full number must intersect.
    local = prod.phone_keys('02 521 58 59')
    e164 = prod.phone_keys('+32 2 521 58 59')
    intl00 = prod.phone_keys('0032 2 521 58 59')
    assert local & e164, (local, e164)
    assert local & intl00, (local, intl00)
    assert not (prod.phone_keys('0471 11 22 33') & prod.phone_keys('02 11 22 33'))

    # Exact phone must NOT overpower a materially conflicting current identity.
    bad_places = [{
        'id': 'wrong-current', 'name': 'Western Union', 'phones': ['+32 2 527 16 35'],
        'addresses': [{'freeform': 'HEYVAERTSTRAAT 135, 1080 Brussels'}],
        'websites': [], 'operating_status': '',
    }]
    bad_c = {'r': 90, 'n': 'Mitra Mercury', 'p': '1080', 'a': 'HEYVAERTSTRAAT 135', 'ph': '+32 2 527 16 35'}
    bp, be = identity.resolve(bad_c, bad_places, identity.indexes(bad_places))
    assert bp is None and be['phone_exact'] is True and be['phone_corroborated'] is False, (bp, be)

    # Matching phone plus corroborating name remains resolvable.
    good_places = [{
        'id': 'good-current', 'name': 'Garage Louis & Fils', 'phones': ['+32 2 523 47 80'],
        'addresses': [{'freeform': 'Chaussée de Ninove 732, 1070 Anderlecht'}],
        'websites': [], 'operating_status': '',
    }]
    good_c = {'r': 91, 'n': 'GARAGE LOUIS EN ZONEN', 'p': '1070', 'a': 'Chaussée de Ninove 732', 'ph': '+3225234780'}
    gp, ge = identity.resolve(good_c, good_places, identity.indexes(good_places))
    assert gp is not None and ge['resolved'] and ge['phone_corroborated'], (gp, ge)

    # Known historical false negatives must be in the deterministic domain lattice.
    kanoff_hosts = {prod.v2.host(u) for u in v4.guesses({'n': 'KANOFF LEGAL', 'em': '', 'cow': ''})}
    idcite_hosts = {prod.v2.host(u) for u in v4.guesses({'n': 'ID.CITE ARCHITECTS', 'em': '', 'cow': ''})}
    assert 'kanofflegal.com' in kanoff_hosts, kanoff_hosts
    assert 'idcite.be' in idcite_hosts, idcite_hosts

    # A complete source candidate that Overture cannot strongly resolve must
    # continue to web challenge, not terminate before search.
    c = {'r': 1, 'n': 'Acme Brussels', 'p': '1050', 'a': 'Rue Test 1', 'ph': '02 555 12 12', 'em': '', 'cow': ''}
    pe = {'resolved': False, 'overture_id': 'weak-best-guess', 'overture_name': 'Acme'}
    assert prod.preclassify_hardened(c, None, pe, True) is None

    # But unresolved identity can never satisfy the HIGH certificate gate.
    w = {
        'healthy_providers': ['bing', 'exa'], 'search_queries': 2,
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
    a['place']['resolved'] = True; b['place']['resolved'] = True
    assert prod.canonical_key_hardened(a) == prod.canonical_key_hardened(b) == 'o:weak-shared'

    # Production search topology: no DDG dependency. Exa is an independent family
    # and is only reachable through the strict-HIGH marker in the provider pool.
    limits = providers.provider_concurrency_plan(2)
    assert limits == {'bing': 2, 'yahoo': 2, 'exa': 2}, limits
    assert providers.provider_family('yahoo') == 'bing'
    assert providers.provider_family('exa') == 'exa'
    psrc = inspect.getsource(providers.webcheck)
    assert 'EXA_API_KEY' in psrc
    assert '_strict_high_candidate' in psrc
    assert 'duckduckgo' not in psrc.lower()

    # Runtime integrity: checkpoint before web and after every batch; strict-HIGH
    # marker controls second-pass eligibility.
    src = inspect.getsource(worker_policy.worker)
    checkpoint_src = inspect.getsource(worker_policy._checkpoint)
    assert 'partial_results.jsonl' in checkpoint_src
    assert 'progress.json' in checkpoint_src
    assert 'GWS_V55_PROGRESS=' in checkpoint_src
    assert 'GWS_WEB_BATCH_SIZE' in src
    assert 'stage="resolved"' in src
    assert 'stage="web_batch_complete"' in src
    assert '_strict_high_candidate' in src
    assert 'IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH_AFTER_BOUNDED_WEB_CHALLENGE' in src

    print('GWS_V54_REGRESSION_OK')


if __name__ == '__main__':
    main()
