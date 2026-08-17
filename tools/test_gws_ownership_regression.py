#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

import gws_ownership_gate as own
import gws_home_openserp_worker_ownership_safe as residential_safe
import gws_fleet_preaggregate_ownership_guard as preagg


def evidence(url: str, *, domain: float = 1.0, address: float = 1.0, phone: bool = True):
    return {
        "owned": url,
        "owned_identity": {
            "domain_name_overlap": domain,
            "address_overlap": address,
            "phone": phone,
            "postcode": True,
        },
        "owned_via": "regression_fixture",
    }


class OwnershipGateRegression(unittest.TestCase):
    def test_booking_profile_can_never_be_owned_site(self):
        row = {"hub_name": "Fernanda Castillo", "candidate": {"n": "Fernanda Castillo"}}
        a = own.assess(row, evidence("https://rosa.be/fr/hp/fernanda-castillo/"))
        self.assertTrue(a["third_party"])
        self.assertFalse(a["confident"])
        self.assertEqual(a["reason"], "KNOWN_THIRD_PARTY_HOST")

    def test_directory_identity_page_can_never_be_owned_site(self):
        row = {"hub_name": "Darmal", "candidate": {"n": "Darmal"}}
        a = own.assess(row, evidence("https://combook.be/darmal", domain=1.0))
        self.assertTrue(a["third_party"])
        self.assertFalse(a["confident"])

    def test_unknown_unbranded_identity_page_is_not_owned_site(self):
        row = {"hub_name": "Fernanda Castillo", "candidate": {"n": "Fernanda Castillo"}}
        a = own.assess(row, evidence("https://example-directory.be/profile/fernanda", domain=0.0))
        self.assertFalse(a["third_party"])
        self.assertFalse(a["branded_host"])
        self.assertFalse(a["confident"])
        self.assertEqual(a["reason"], "HOST_NOT_BRANDED_TO_BUSINESS")

    def test_real_first_party_site_is_still_rejectable(self):
        row = {"hub_name": "Tagawa Delta", "candidate": {"n": "Tagawa Delta"}}
        a = own.assess(row, evidence("https://tagawa.eu/", domain=0.8))
        self.assertFalse(a["third_party"])
        self.assertTrue(a["branded_host"])
        self.assertTrue(a["confident"])
        self.assertEqual(a["reason"], "OWNERSHIP_CONFIRMED")

    def test_real_practice_site_is_still_rejectable(self):
        row = {"hub_name": "Curatia", "candidate": {"n": "Curatia"}}
        a = own.assess(row, evidence("https://curatia.be/", domain=1.0))
        self.assertTrue(a["confident"])


class ResidentialWrapperRegression(unittest.TestCase):
    def setUp(self):
        self.original = residential_safe._ORIGINAL_PROBE_HOST

    def tearDown(self):
        residential_safe._ORIGINAL_PROBE_HOST = self.original

    def _probe(self, candidate, url, identity):
        residential_safe._ORIGINAL_PROBE_HOST = lambda c, seed: {
            "seed": seed,
            "final": url,
            "ok": True,
            "matched": True,
            "identity": identity,
            "dns_negative": False,
        }
        return residential_safe._safe_probe_host(candidate, url)

    def test_residential_withholds_third_party_match_and_keeps_searching(self):
        ev = self._probe(
            {"n": "Fernanda Castillo"},
            "https://rosa.be/fr/hp/fernanda-castillo/",
            {"domain_name_overlap": 1.0, "address_overlap": 1.0, "phone": True},
        )
        self.assertFalse(ev["matched"])
        self.assertIn("identity_match_withheld", ev)
        self.assertFalse(ev["ownership_assessment"]["confident"])

    def test_residential_retains_confident_first_party_match(self):
        ev = self._probe(
            {"n": "Tagawa Delta"},
            "https://tagawa.eu/",
            {"domain_name_overlap": 0.8, "address_overlap": 1.0, "phone": True},
        )
        self.assertTrue(ev["matched"])
        self.assertTrue(ev["ownership_assessment"]["confident"])
        self.assertNotIn("identity_match_withheld", ev)


class PreAggregateRegression(unittest.TestCase):
    def test_preaggregate_quarantines_unproven_directory_reject(self):
        row = {
            "hub_name": "Darmal",
            "outcome": "REJECT",
            "reason": "OWNED_SITE_FOUND_PASS1",
            "verification_status": "REJECT",
            "owned_website": "https://combook.be/darmal",
            "web_pass1": evidence("https://combook.be/darmal"),
            "certificate": {"verified": False},
        }
        guarded, changed = preagg.guard(row)
        self.assertTrue(changed)
        self.assertEqual(guarded["outcome"], "UNCERTAIN")
        self.assertEqual(guarded["verification_status"], "UNCERTAIN")
        self.assertEqual(guarded["owned_website"], "")
        self.assertTrue(guarded["needs_gpt_review"])

    def test_preaggregate_preserves_true_first_party_reject(self):
        row = {
            "hub_name": "Tagawa Delta",
            "outcome": "REJECT",
            "reason": "OWNED_SITE_FOUND_PASS1",
            "verification_status": "REJECT",
            "owned_website": "https://tagawa.eu/",
            "web_pass1": evidence("https://tagawa.eu/", domain=0.8),
        }
        guarded, changed = preagg.guard(row)
        self.assertFalse(changed)
        self.assertEqual(guarded["outcome"], "REJECT")
        self.assertTrue(guarded["preaggregate_ownership_guard_passed"])


class WiringRegression(unittest.TestCase):
    def test_autonomous_uses_ownership_safe_strict_verifier(self):
        text = Path(".github/workflows/gws-autonomous-fleet.yml").read_text(encoding="utf-8")
        self.assertIn("gws_search_verify_ownership_safe.py", text)
        self.assertNotIn("python tools/gws_search_verify.py --shard-dir", text)

    def test_autonomous_runs_preaggregate_ownership_guard(self):
        text = Path(".github/workflows/gws-autonomous-fleet.yml").read_text(encoding="utf-8")
        guard = "gws_fleet_preaggregate_ownership_guard.py --root results/shards"
        aggregate = "gws_fleet_aggregate.py --provider github"
        self.assertIn(guard, text)
        self.assertIn(aggregate, text)
        self.assertLess(text.index(guard), text.index(aggregate))


if __name__ == "__main__":
    unittest.main(verbosity=2)
