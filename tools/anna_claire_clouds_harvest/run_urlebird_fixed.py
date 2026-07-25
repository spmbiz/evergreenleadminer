#!/usr/bin/env python3
"""Compatibility wrapper for the current Urlebird thumbnail markup."""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

import urlebird_harvest as harvest


def parse_entries_fixed(fragment: str) -> list[harvest.Entry]:
    soup = BeautifulSoup(fragment, "html.parser")
    out: list[harvest.Entry] = []
    for thumb in soup.select("#thumbs div.thumb, div.thumb"):
        # The first link in .info3 points to the author profile. Select the
        # actual video link explicitly so it cannot be mistaken for the user URL.
        link = thumb.select_one("a[href*='/video/']")
        if not link:
            continue
        href = urljoin("https://urlebird.com", str(link.get("href") or ""))
        img = thumb.select_one("div.img img") or thumb.select_one("img")
        img_url = ""
        if img:
            for attr in ("data-src", "data-original", "src"):
                value = img.get(attr)
                if value and str(value).startswith("http"):
                    img_url = str(value)
                    break
        caption_link = thumb.select_one("div.info3 a[href*='/video/']") or link
        caption = caption_link.get_text(" ", strip=True)
        out.append(harvest.Entry(href, img_url, caption))
    return out


harvest.parse_entries = parse_entries_fixed
raise SystemExit(harvest.main())
