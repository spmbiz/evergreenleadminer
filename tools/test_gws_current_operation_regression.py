#!/usr/bin/env python3
from __future__ import annotations

import unittest
from types import SimpleNamespace

import gws_search_verify_current_safe as current


class FakeFabric:
    def __init__(self, mapping):
        self.mapping = mapping

    def _openserp(self, lane, query):
        rows = self.mapping.get(query, [])
        items = [SimpleNamespace(url=x[0], title=x[1], snippet=x[2]) for x in rows]
        return items, {"status": "OK", "meta": {"provider_families": ["bing", "duckduckgo"]}}


def high_row(name="Ratabar", address="Chaussée de Boondael 345", phone=""):
    return {
        "hub_name": name,
        "hub_address": address,
        "overture_name": name,
        "overture_address": address,
        "overture_phone": phone,
        "verification_status": "HIGH",
        "outcome": "HIGH",
        "reason": "VERIFIED_NO_WEBSITE",
        "certificate": {"verified": True, "evidence_digest": "old-digest"},
        "certificate_digest": "old-digest",
    }


class CurrentOperationRegression(unittest.TestCase):
    def test_two_independent_current_identity_sources_corroborate(self):
        row = high_row()
        c = {"n": "Ratabar", "a": "Chaussée de Boondael 345", "ph": ""}
        f = FakeFabric({
            '"Ratabar" "Chaussée de Boondael 345" 2026': [
                ("https://ixelles.city/ratabar", "Ratabar", "345 Chaussée de Boondael. Aujourd'hui 17h00 - 05h00, 2026"),
                ("https://privateaser.com/ratabar", "Ratabar - Réserver", "Ratabar, 345 Chaussée de Boondael. Réservation jusqu'à 80 personnes"),
            ],
            '"Ratabar" "Chaussée de Boondael 345" horaires': [
                ("https://waze.com/ratabar", "Ratabar", "Chaussée de Boondael 345. Closed now. Monday 17:00 - 04:00"),
            ],
            '"Chaussée de Boondael 345" 2026': [],
        })
        result = current.current_operation_challenge(row, c, f)
        self.assertEqual(result["status"], "CORROBORATED")
        self.assertGreaterEqual(len(result["corroborating_hosts"]), 2)
        self.assertTrue(result["current_signal_hosts"])

    def test_current_different_operator_at_address_blocks_high(self):
        row = high_row("Panda Bar", "Chaussée de Waterloo 285")
        c = {"n": "Panda Bar", "a": "Chaussée de Waterloo 285", "ph": ""}
        f = FakeFabric({
            '"Panda Bar" "Chaussée de Waterloo 285" 2026': [
                ("https://old-directory.example/panda", "Panda Bar", "Panda Bar, Chaussée de Waterloo 285"),
            ],
            '"Panda Bar" "Chaussée de Waterloo 285" horaires': [],
            '"Chaussée de Waterloo 285" 2026': [
                ("https://company-current.example/ines", "Inès & Co", "Chaussée de Waterloo 285. Active café/bar company created 2025"),
            ],
        })
        result = current.current_operation_challenge(row, c, f)
        self.assertEqual(result["status"], "CURRENT_OPERATOR_AMBIGUOUS")
        self.assertTrue(result["competing_hosts"])

    def test_one_stale_listing_is_not_enough(self):
        row = high_row("Old Place", "Rue Test 22")
        c = {"n": "Old Place", "a": "Rue Test 22", "ph": ""}
        f = FakeFabric({
            '"Old Place" "Rue Test 22" 2026': [
                ("https://directory.example/old", "Old Place", "Rue Test 22"),
            ],
            '"Old Place" "Rue Test 22" horaires': [],
            '"Rue Test 22" 2026': [],
        })
        result = current.current_operation_challenge(row, c, f)
        self.assertEqual(result["status"], "NOT_CORROBORATED")

    def test_high_wrapper_keeps_high_only_after_corroboration(self):
        old = current._ORIGINAL_CLASSIFY
        try:
            current._ORIGINAL_CLASSIFY = lambda row, c, pe, fabric, max_queries: dict(row)
            row = high_row()
            c = {"n": "Ratabar", "a": "Chaussée de Boondael 345", "ph": ""}
            f = FakeFabric({
                '"Ratabar" "Chaussée de Boondael 345" 2026': [
                    ("https://one.example/ratabar", "Ratabar", "Chaussée de Boondael 345. Open today 17:00"),
                    ("https://two.example/ratabar", "Ratabar", "345 Chaussée de Boondael. Réservation 2026"),
                ],
                '"Ratabar" "Chaussée de Boondael 345" horaires': [],
                '"Chaussée de Boondael 345" 2026': [],
            })
            out = current.classify_strict_current_safe(row, c, {}, f, 3)
            self.assertEqual(out["verification_status"], "HIGH")
            self.assertTrue(out["certificate"]["current_operation_verified"])
            self.assertNotEqual(out["certificate_digest"], "old-digest")
        finally:
            current._ORIGINAL_CLASSIFY = old

    def test_high_wrapper_withholds_without_current_operation(self):
        old = current._ORIGINAL_CLASSIFY
        try:
            current._ORIGINAL_CLASSIFY = lambda row, c, pe, fabric, max_queries: dict(row)
            row = high_row("Old Place", "Rue Test 22")
            c = {"n": "Old Place", "a": "Rue Test 22", "ph": ""}
            f = FakeFabric({
                '"Old Place" "Rue Test 22" 2026': [],
                '"Old Place" "Rue Test 22" horaires': [],
                '"Rue Test 22" 2026': [],
            })
            out = current.classify_strict_current_safe(row, c, {}, f, 3)
            self.assertEqual(out["verification_status"], "UNCERTAIN")
            self.assertEqual(out["reason"], "CURRENT_OPERATION_NOT_INDEPENDENTLY_CORROBORATED")
            self.assertFalse(out["certificate"]["verified"])
            self.assertEqual(out["certificate_digest"], "")
        finally:
            current._ORIGINAL_CLASSIFY = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
