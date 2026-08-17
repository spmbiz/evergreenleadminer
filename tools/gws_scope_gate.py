#!/usr/bin/env python3
from __future__ import annotations

import re
from typing import Any

# Exact source categories that are structurally outside the target:
# independent local commercial businesses. Keep this intentionally narrow.
NON_BUSINESS_SOURCE_CATEGORIES = {
    "public_plaza",
    "public_square",
    "government_office",
    "city_hall",
    "town_hall",
    "municipal_office",
    "municipality",
    "public_utility_company",
    "monument",
    "memorial",
    "public_park",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def assess(row: dict[str, Any]) -> dict[str, Any]:
    source_category = _norm(row.get("overture_category") or "")
    out_of_scope = source_category in NON_BUSINESS_SOURCE_CATEGORIES
    return {
        "in_scope": not out_of_scope,
        "reason": "SOURCE_CATEGORY_NON_BUSINESS" if out_of_scope else "NO_DETERMINISTIC_SCOPE_BLOCK",
        "source_category": source_category,
        "business_name": str(row.get("hub_name") or row.get("overture_name") or ""),
    }
