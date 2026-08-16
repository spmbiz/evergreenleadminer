#!/usr/bin/env python3
from __future__ import annotations

import unittest

import gws_search_verify as mod


class EmptyFallback:
    def search(self, family, query):
        return [], [{"provider":"ddgs","query_family":family,"status":"OK","results":0}]


class OwnedFallback:
    def search(self, family, query):
        return ([{"provider":"ddgs","title":"Acme Brussels","url":"https://acme.example/","snippet":"Acme Brussels official site"}],
                [{"provider":"ddgs","query_family":family,"status":"OK","results":1}])


class GwsSearchVerifyTests(unittest.TestCase):
    def test_fallback_absence_never_high(self):
        row={"outcome":"REVIEW","reason":"CURRENT_ENTITY_RESOLVED_NO_OWNED_SITE_IN_OVERTURE","needs_gpt_review":True}
        c={"r":1,"n":"Example Business","p":"1050","a":"Rue Exemple 1","ph":"","alias":""}
        out=mod.classify_fallback(row,c,EmptyFallback(),3)
        self.assertNotEqual(out["outcome"],"HIGH")
        self.assertEqual(out["verification_status"],"ERROR_RETRYABLE")
        self.assertEqual(out["reason"],"FALLBACK_SEARCH_SURVIVED_REQUIRES_STRICT_RETRY")

    def test_fallback_can_reject_confirmed_owned_site(self):
        old_plausible=mod.home.plausible; old_probe=mod.home.probe_host
        try:
            mod.home.plausible=lambda c,item:(True,{"domain_overlap":1.0})
            mod.home.probe_host=lambda c,u:{"matched":True,"final":"https://acme.example/","identity":{"matched":True}}
            row={"outcome":"REVIEW","needs_gpt_review":True}
            c={"r":2,"n":"Acme","p":"1050","a":"Rue Exemple 1","ph":"","alias":""}
            out=mod.classify_fallback(row,c,OwnedFallback(),3)
            self.assertEqual(out["outcome"],"REJECT")
            self.assertEqual(out["verification_status"],"REJECT")
            self.assertEqual(out["owned_website"],"https://acme.example/")
        finally:
            mod.home.plausible=old_plausible; mod.home.probe_host=old_probe

    def test_place_mapping_preserves_strict_identity_fields(self):
        row={"overture_id":"ov1","overture_name":"Acme","overture_resolved":True,"name_similarity":.96,
             "address_overlap":.5,"postcode_match":True,"phone_exact":False,"overture_operating_status":"open"}
        pe=mod.place_from_row(row)
        self.assertTrue(pe["resolved"])
        self.assertTrue(pe["postcode_match"])
        self.assertEqual(pe["address_overlap"],.5)


if __name__=="__main__":
    unittest.main()
