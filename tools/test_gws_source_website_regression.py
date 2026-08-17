#!/usr/bin/env python3
from __future__ import annotations

import unittest

import gws_ownership_gate as own
import gws_search_verify_source_safe as source_safe


class SourceWebsiteRegression(unittest.TestCase):
    def setUp(self):
        self.orig_probe = source_safe.safe.base.home.probe_host
        self.orig_classify = source_safe._ORIGINAL_CLASSIFY

    def tearDown(self):
        source_safe.safe.base.home.probe_host = self.orig_probe
        source_safe._ORIGINAL_CLASSIFY = self.orig_classify

    @staticmethod
    def high_result(row, c, pe, fabric, max_queries):
        out = dict(row)
        out.update({
            'outcome':'HIGH',
            'reason':'VERIFIED_NO_WEBSITE',
            'verification_status':'HIGH',
            'verification_provider':'openserp_ci_ownership_safe',
            'needs_gpt_review':True,
            'owned_website':'',
            'final_high_challenge':{'status':'CLEAR'},
            'certificate':{'verified':True,'high_challenge_clear':True,'evidence_digest':'fixture'},
            'certificate_digest':'fixture',
        })
        return out

    @staticmethod
    def dns_negative(url):
        return {'seed':url,'final':url,'status':404,'ok':False,'matched':False,'dns_negative':True,'identity':{}}

    def test_known_booking_platforms_are_third_party(self):
        self.assertTrue(own.is_third_party('https://lecentreducheveu.optios.net/en'))
        self.assertTrue(own.is_third_party('https://restaurant-isabelle-arpin.hey-restaurants.com/'))
        self.assertTrue(own.is_third_party('https://www.planity.com/fr-BE/laurent-amir-bruxelles-1000'))

    def test_the_kooples_live_source_site_blocks_high_even_without_page_address(self):
        source_safe.safe.base.home.probe_host = lambda c, url: {
            'seed':url,'final':'https://www.thekooples.com/fr/fr/','status':200,'ok':True,
            'matched':False,'dns_negative':False,'identity':{},
        }
        source_safe._ORIGINAL_CLASSIFY = self.high_result
        row={'hub_name':'The Kooples','overture_websites':'["http://www.thekooples.com"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'The Kooples'},{},None,5)
        self.assertNotEqual(out['verification_status'],'HIGH')
        self.assertEqual(out['verification_status'],'UNCERTAIN')
        self.assertEqual(out['reason'],'SOURCE_WEBSITE_LIVE_BRANDED_UNRESOLVED')
        self.assertTrue(out['certificate']['source_website_blocks_high'])

    def test_confident_source_site_is_rejected(self):
        source_safe.safe.base.home.probe_host = lambda c, url: {
            'seed':url,'final':'https://tagawa.eu/','status':200,'ok':True,
            'matched':True,'dns_negative':False,
            'identity':{'domain_name_overlap':1.0,'address_overlap':1.0,'phone':True,'postcode':True},
        }
        row={'hub_name':'Tagawa','overture_websites':'["https://tagawa.eu/"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'Tagawa'},{},None,5)
        self.assertEqual(out['verification_status'],'REJECT')
        self.assertEqual(out['reason'],'OWNED_SITE_FIRST_PARTY_CONFIRMED_SOURCE_WEBSITE')

    def test_dns_negative_source_site_can_fall_through_to_normal_high_gate(self):
        source_safe.safe.base.home.probe_host = lambda c, url: self.dns_negative(url)
        source_safe._ORIGINAL_CLASSIFY = self.high_result
        row={'hub_name':'Example Tailor','overture_websites':'["https://exampletailor-invalid.test/"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'Example Tailor'},{},None,5)
        self.assertEqual(out['verification_status'],'HIGH')
        self.assertEqual(out['source_website_precheck']['status'],'CLEAR')
        self.assertEqual(out['direct_domain_high_challenge']['status'],'CLEAR')

    def test_third_party_source_url_does_not_count_as_owned(self):
        called=[]
        def probe(c,url):
            called.append(url)
            return self.dns_negative(url)
        source_safe.safe.base.home.probe_host = probe
        source_safe._ORIGINAL_CLASSIFY = self.high_result
        row={'hub_name':'Fernanda Castillo','overture_websites':'["https://rosa.be/fr/hp/fernanda-castillo/"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'Fernanda Castillo'},{},None,5)
        self.assertEqual(out['verification_status'],'HIGH')
        self.assertNotIn('https://rosa.be/fr/hp/fernanda-castillo/', called)
        self.assertEqual(out['source_website_precheck']['events'][0]['status'],'THIRD_PARTY_OR_PLATFORM')

    def test_transient_branded_source_site_blocks_high_retryably(self):
        source_safe.safe.base.home.probe_host = lambda c, url: {
            'seed':url,'final':url,'status':503,'ok':False,'matched':False,'dns_negative':False,'error':'HTTPError','identity':{},
        }
        source_safe._ORIGINAL_CLASSIFY = self.high_result
        row={'hub_name':'Jhon M Boston','overture_websites':'["http://www.jhonmboston.be/"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'Jhon M Boston'},{},None,5)
        self.assertEqual(out['verification_status'],'ERROR_RETRYABLE')
        self.assertEqual(out['reason'],'SOURCE_WEBSITE_PROBE_INCOMPLETE')

    def test_planity_slug_generates_laurent_amir_alias_queries(self):
        row={'hub_name':'Teamlaurentamir','overture_websites':'["https://www.planity.com/fr-BE/laurent-amir-hairdresser-1000-bruxelles"]'}
        aliases=source_safe.source_listing_aliases(row,{'n':'Teamlaurentamir','a':'Rue Willems 64'})
        self.assertTrue(any('laurent amir' in x['alias'].lower() for x in aliases), aliases)
        queries,_=source_safe._augmented_challenge_queries(row,{'n':'Teamlaurentamir','a':'Rue Willems 64'})
        self.assertTrue(any('laurent amir' in q.lower() for q in queries), queries)

    def test_planity_alias_direct_domain_catches_laurentamir_be_search_miss(self):
        def probe(c,url):
            if 'laurentamir.be' in url:
                return {
                    'seed':url,'final':'https://laurentamir.be/','status':200,'ok':True,
                    'matched':True,'dns_negative':False,
                    'identity':{'domain_name_overlap':1.0,'address_overlap':1.0,'phone':False,'postcode':True},
                }
            return self.dns_negative(url)
        source_safe.safe.base.home.probe_host = probe
        source_safe._ORIGINAL_CLASSIFY = self.high_result
        row={'hub_name':'Teamlaurentamir','overture_websites':'["https://www.planity.com/fr-BE/laurent-amir-hairdresser-1000-bruxelles"]'}
        out=source_safe.classify_strict_source_safe(row,{'n':'Teamlaurentamir','a':'Rue Willems 64'},{},None,5)
        self.assertEqual(out['verification_status'],'REJECT')
        self.assertEqual(out['reason'],'OWNED_SITE_FOUND_BY_DIRECT_DOMAIN_HIGH_CHALLENGE')
        self.assertEqual(out['owned_website'],'https://laurentamir.be/')
        self.assertTrue(out['certificate']['direct_domain_disproved_no_website'])


if __name__=='__main__':
    unittest.main(verbosity=2)
