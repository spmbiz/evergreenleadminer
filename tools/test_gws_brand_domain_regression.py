#!/usr/bin/env python3
from __future__ import annotations

import unittest
import gws_ownership_gate as own


def ev(url: str, *, address: float = 1.0, phone: bool = False, domain: float = 0.0):
    return {
        "owned": url,
        "owned_identity": {
            "domain_name_overlap": domain,
            "address_overlap": address,
            "phone": phone,
            "postcode": True,
        },
    }


class BrandDomainRegression(unittest.TestCase):
    def test_optil_short_brand_domain_is_first_party_with_exact_address(self):
        row = {"hub_name": "OPTIL - Vos opticiens indépendants"}
        a = own.assess(row, ev("https://optil.be/nos-magasins/optil-delta-chirec", address=1.0))
        self.assertTrue(a["leading_brand_match"])
        self.assertTrue(a["confident"])
        self.assertEqual(a["reason"], "OWNERSHIP_CONFIRMED")

    def test_herards_possessive_domain_is_first_party_with_exact_address(self):
        row = {"hub_name": "Herard's Opticien"}
        a = own.assess(row, ev("https://www.herards.com/contact/", address=1.0))
        self.assertTrue(a["leading_brand_match"])
        self.assertTrue(a["confident"])

    def test_short_generic_bar_domain_does_not_gain_brand_ownership(self):
        row = {"hub_name": "Bar a Fidelis"}
        a = own.assess(row, ev("https://bar.com/", address=1.0))
        self.assertFalse(a["leading_brand_match"])
        self.assertFalse(a["confident"])

    def test_distinctive_brand_without_identity_still_cannot_reject(self):
        row = {"hub_name": "OPTIL - Vos opticiens indépendants"}
        a = own.assess(row, ev("https://optil.be/", address=0.0, phone=False))
        self.assertTrue(a["leading_brand_match"])
        self.assertFalse(a["confident"])
        self.assertEqual(a["reason"], "BRANDED_HOST_IDENTITY_NOT_STRONG_ENOUGH")

    def test_third_party_always_wins_even_if_slug_contains_brand(self):
        row = {"hub_name": "OPTIL - Vos opticiens indépendants"}
        a = own.assess(row, ev("https://www.belgoptic.be/fr/opticiens/optil", address=1.0))
        self.assertTrue(a["third_party"])
        self.assertFalse(a["confident"])
        self.assertEqual(a["reason"], "KNOWN_THIRD_PARTY_HOST")


if __name__ == "__main__":
    unittest.main(verbosity=2)
