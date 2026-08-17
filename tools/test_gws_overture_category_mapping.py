#!/usr/bin/env python3
from __future__ import annotations

import unittest

import gws_source_overture_extended as ext


class OvertureCategoryTokenRegression(unittest.TestCase):
    def test_public_plaza_is_not_pub(self):
        self.assertEqual(ext.category_type("", "public_plaza"), "")

    def test_public_utility_company_is_not_pub(self):
        self.assertEqual(ext.category_type("", "public_utility_company"), "")

    def test_public_service_government_is_not_pub(self):
        self.assertEqual(ext.category_type("", "public_service_and_government"), "")

    def test_real_pub_still_maps(self):
        self.assertEqual(ext.category_type("", "pub"), "Pub")

    def test_wine_bar_still_maps_by_bar_token(self):
        self.assertEqual(ext.category_type("", "wine_bar"), "Bar")

    def test_cocktail_bar_keeps_specific_mapping(self):
        self.assertEqual(ext.category_type("", "cocktail_bar"), "Cocktail bar")

    def test_real_clinic_still_maps(self):
        self.assertEqual(ext.category_type("", "medical_clinic"), "Clinic")

    def test_real_fashion_accessory_mapping_still_works(self):
        self.assertEqual(ext.category_type("", "fashion_accessories"), "Accessories")


if __name__ == "__main__":
    unittest.main(verbosity=2)
