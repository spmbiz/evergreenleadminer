#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

import gws_ownership_gate as own
import gws_home_openserp_worker_ownership_safe as residential_safe
import gws_fleet_preaggregate_ownership_guard as preagg
import gws_residential_ingress_adapter as ingress_adapter
import gws_search_verify_source_safe as source_safe


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

    def test_planity_profile_can_never_be_owned_site(self):
        row = {"hub_name": "Laurent Amir", "candidate": {"n": "Laurent Amir"}}
        a = own.assess(row, evidence("https://www.planity.com/laurent-amir-hairdresser"))
        self.assertTrue(a["third_party"])
        self.assertFalse(a["confident"])

    def test_optios_client_page_can_never_be_owned_site(self):
        row = {"hub_name": "Le Centre du Cheveu", "candidate": {"n": "Le Centre du Cheveu"}}
        a = own.assess(row, evidence("https://lecentreducheveu.optios.net/en"))
        self.assertTrue(a["third_party"])
        self.assertFalse(a["confident"])

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


class SourceSafeRegression(unittest.TestCase):
    def setUp(self):
        self.probe = source_safe.safe.base.home.probe_host
        self.original_classify = source_safe._ORIGINAL_CLASSIFY

    def tearDown(self):
        source_safe.safe.base.home.probe_host = self.probe
        source_safe._ORIGINAL_CLASSIFY = self.original_classify

    def test_overture_first_party_site_is_checked_before_no_site_classification(self):
        row = {
            "hub_name": "The Kooples",
            "overture_websites": '["http://www.thekooples.com"]',
        }
        c = {"n": "The Kooples", "a": "Avenue Louise 72", "p": "1050", "ph": ""}
        source_safe.safe.base.home.probe_host = lambda candidate, url: {
            "final": "https://www.thekooples.com/",
            "ok": True,
            "matched": True,
            "dns_negative": False,
            "status": 200,
            "error": "",
            "identity": {
                "domain_name_overlap": 1.0,
                "address_overlap": 1.0,
                "phone": False,
                "postcode": True,
            },
        }
        check = source_safe.source_website_precheck(row, c)
        self.assertEqual(check["status"], "OWNED_CONFIRMED")
        self.assertEqual(check["owned"], "https://www.thekooples.com/")

    def test_source_site_confirmation_short_circuits_would_be_high(self):
        row = {
            "hub_name": "The Kooples",
            "overture_websites": '["http://www.thekooples.com"]',
        }
        c = {"n": "The Kooples", "a": "Avenue Louise 72", "p": "1050", "ph": ""}
        source_safe.safe.base.home.probe_host = lambda candidate, url: {
            "final": "https://www.thekooples.com/",
            "ok": True,
            "matched": True,
            "dns_negative": False,
            "status": 200,
            "error": "",
            "identity": {
                "domain_name_overlap": 1.0,
                "address_overlap": 1.0,
                "phone": False,
                "postcode": True,
            },
        }
        source_safe._ORIGINAL_CLASSIFY = lambda *args, **kwargs: {
            **row,
            "verification_status": "HIGH",
            "outcome": "HIGH",
            "reason": "VERIFIED_NO_WEBSITE",
            "certificate": {"verified": True},
        }
        out = source_safe.classify_strict_source_safe(row, c, {}, object(), 4)
        self.assertEqual(out["verification_status"], "REJECT")
        self.assertEqual(out["reason"], "OWNED_SITE_FIRST_PARTY_CONFIRMED_SOURCE_WEBSITE")
        self.assertEqual(out["owned_website"], "https://www.thekooples.com/")

    def test_third_party_source_url_never_becomes_owned_site(self):
        row = {
            "hub_name": "Fernanda Castillo",
            "overture_websites": '["https://rosa.be/fr/hp/fernanda-castillo/"]',
        }
        c = {"n": "Fernanda Castillo", "a": "Chaussée de Bruxelles 277", "p": "1190", "ph": "+32472965962"}
        check = source_safe.source_website_precheck(row, c)
        self.assertEqual(check["status"], "CLEAR")
        self.assertEqual(check["events"][0]["status"], "THIRD_PARTY_OR_PLATFORM")


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


class ResidentialIngressRegression(unittest.TestCase):
    def test_nested_worker_result_normalizes_record_key_and_fingerprint(self):
        ev = {
            "candidate": {
                "record_key": "overture:test-1",
                "fingerprint": "fp-test-1",
                "n": "Darmal épicerie",
                "a": "Place Test 8",
                "p": "1160",
                "observed_at": "2026-08-17T00:00:00+00:00",
            },
            "pass1": {"healthy_providers": ["bing", "duckduckgo"], "search_health": [], "direct_health": []},
            "pass2": {"healthy_providers": ["bing", "duckduckgo"], "search_health": [], "direct_health": []},
            "certificate": {"verified": False},
            "status": "EVIDENCE_INCOMPLETE",
            "reason": "RESIDENTIAL_CERTIFICATE_GATES_INCOMPLETE",
            "owned_site": "",
        }
        row = ingress_adapter.normalize_event(ev)
        self.assertIsNotNone(row)
        self.assertEqual(row["record_key"], "overture:test-1")
        self.assertEqual(row["source_fingerprint"], "fp-test-1")
        self.assertEqual(row["hub_name"], "Darmal épicerie")
        self.assertEqual(row["status"], "SEARCH_INCOMPLETE")
        self.assertFalse(row["final_high"])

    def test_confident_worker_reject_becomes_owned_candidate_not_high(self):
        ev = {
            "candidate": {"record_key": "overture:test-2", "fingerprint": "fp-test-2", "n": "Tagawa Delta"},
            "pass1": {"healthy_providers": ["bing", "duckduckgo"], "search_health": [], "direct_health": []},
            "pass2": {},
            "status": "REJECT",
            "reason": "OWNED_SITE_CONFIRMED",
            "owned_site": "https://tagawa.eu/",
        }
        row = ingress_adapter.normalize_event(ev)
        self.assertEqual(row["status"], "OWNED_SITE_CONFIRMED")
        self.assertEqual(row["owned_site"], "https://tagawa.eu/")
        self.assertFalse(row["final_high"])


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


class RecoveryQueueIntegrity(unittest.TestCase):
    @staticmethod
    def _pending():
        return json.loads(Path("gpt/gws_pending_batches.json").read_text(encoding="utf-8"))

    def test_active_ownership_recovery_has_no_duplicate_record_fingerprint(self):
        pending = self._pending()
        seen = {}
        duplicates = []
        for batch in pending.get("batches") or []:
            if batch.get("status") != "pending" or batch.get("provider") != "ownership_recovery":
                continue
            path = Path(str(batch.get("batch") or ""))
            self.assertTrue(path.exists(), f"Missing recovery batch file: {path}")
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                pair = (str(row.get("record_key") or ""), str(row.get("fingerprint") or ""))
                self.assertTrue(all(pair), f"Recovery row missing key/fingerprint in {path}")
                if pair in seen:
                    duplicates.append((pair, seen[pair], str(path)))
                else:
                    seen[pair] = str(path)
        self.assertFalse(duplicates, f"Duplicate active recovery pairs: {duplicates[:5]}")

    def test_pending_records_metadata_matches_active_backlog(self):
        pending = self._pending()
        expected = 0
        for batch in pending.get("batches") or []:
            if batch.get("status") != "pending":
                continue
            remaining = batch.get("verification_remaining")
            expected += int(batch.get("records") or 0) if remaining is None else int(remaining or 0)
        self.assertEqual(int(pending.get("pending_records") or 0), expected)


class WiringRegression(unittest.TestCase):
    def test_autonomous_uses_source_safe_strict_verifier(self):
        text = Path(".github/workflows/gws-autonomous-fleet.yml").read_text(encoding="utf-8")
        self.assertIn("gws_search_verify_source_safe.py", text)
        self.assertNotIn("python tools/gws_search_verify.py --shard-dir", text)
        self.assertNotIn("python tools/gws_search_verify_ownership_safe.py --shard-dir", text)

    def test_autonomous_runs_preaggregate_ownership_guard(self):
        text = Path(".github/workflows/gws-autonomous-fleet.yml").read_text(encoding="utf-8")
        guard = "gws_fleet_preaggregate_ownership_guard.py --root results/shards"
        aggregate = "gws_fleet_aggregate.py --provider github"
        self.assertIn(guard, text)
        self.assertIn(aggregate, text)
        self.assertLess(text.index(guard), text.index(aggregate))


if __name__ == "__main__":
    unittest.main(verbosity=2)
