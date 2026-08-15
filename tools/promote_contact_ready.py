#!/usr/bin/env python3
"""Promote recovery candidates with any explicit public contact route to live verify.

The recovery crawler historically required a recovered email before a candidate
could reach the current-site identity gate. That discarded useful first-party
Instagram/Facebook/contact-page/phone observations. This step is deliberately
recall-first but still bounded: candidates already passed the hospitality cheap
screen and first-party crawl; the existing live verifier remains the final gate.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROUTE_FIELDS = ("public_email", "public_phone", "instagram", "facebook", "contact_page")


def truthy(v) -> bool:
    return bool(str(v or "").strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--summary", default="")
    a = ap.parse_args()

    src = Path(a.input)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    fields = []
    if src.exists():
        with src.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fields = list(reader.fieldnames or [])
            rows = list(reader)

    ready = [r for r in rows if any(truthy(r.get(k)) for k in ROUTE_FIELDS)]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if fields:
            w.writeheader()
            w.writerows(ready)

    payload = {
        "input_candidates": len(rows),
        "contact_ready": len(ready),
        "email_ready": sum(truthy(r.get("public_email")) for r in ready),
        "phone_ready": sum(truthy(r.get("public_phone")) for r in ready),
        "instagram_ready": sum(truthy(r.get("instagram")) for r in ready),
        "facebook_ready": sum(truthy(r.get("facebook")) for r in ready),
        "contact_page_ready": sum(truthy(r.get("contact_page")) for r in ready),
        "social_or_contact_without_email": sum(
            not truthy(r.get("public_email")) and any(truthy(r.get(k)) for k in ("public_phone", "instagram", "facebook", "contact_page"))
            for r in ready
        ),
    }
    if a.summary:
        Path(a.summary).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
