#!/usr/bin/env python3
"""Recall-safe additive wrapper around the token-safe Overture mapper.

The v2 token-boundary fix correctly removed substring false positives such as
``pub`` -> ``public_plaza`` and ``deli`` -> ``food_delivery_service``. A diff of
one fixed Overture release also showed a small set of genuine business categories
that had previously been included only by accidental substring matching.

This wrapper restores those legitimate categories explicitly, one exact category
at a time. It never re-enables substring matching and never changes strict website
verification or canonical HIGH semantics.
"""
from __future__ import annotations

import re
from typing import Any

import gws_source_overture_extended as token_safe

base = token_safe.base
_previous_category_type = base.category_type


def _cat(value: Any) -> str:
    return re.sub(r"_+", "_", base.norm(value).replace(" ", "_")).strip("_")


# Exact structured Overture category -> existing/business-safe discovery label.
# Every entry was observed in the 2026-06-17 Brussels release and reviewed as a
# plausible local business category; no public-place/government category appears.
EXPLICIT_RECALL = {
    "gastropub": "Pub",
    "architectural_designer": "Architect",
    "auto_detailing": "Car detailing",
    "gymnastics_center": "Gym fitness",
    "movers": "Moving",
    "notary_public": "Notary",
    "public_relations": "Public relations",
    "vehicle_shipping": "Vehicle shipping",
    "food_delivery_service": "Food delivery service",
}


def category_type(basic: Any, primary: Any) -> str:
    primary_cat = _cat(primary)
    basic_cat = _cat(basic)
    if primary_cat in EXPLICIT_RECALL:
        return EXPLICIT_RECALL[primary_cat]
    if basic_cat in EXPLICIT_RECALL:
        return EXPLICIT_RECALL[basic_cat]
    return _previous_category_type(basic, primary)


base.category_type = category_type


if __name__ == "__main__":
    raise SystemExit(base.main())
