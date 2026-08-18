#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("browser_exact_scene300")
PHOTOS = OUT / "photos"

QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POV thumbnail',
    '"Anna Claire Clouds" POV video',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" "Happy Little Clouds"',
    '"Anna Claire Clouds" "A Girl and Her Canvas"',
    '"Anna Claire Clouds" "Intimately POV"',
    '"Anna Claire Clouds" "POV Hookups"',
    '"Anna Claire Clouds" "Mr Big POV"',
    '"Anna Claire Clouds" "Manuel\'s Fucking POV 14"',
    '"Anna Claire Clouds" "VR Bangers"',
    '"Anna Claire Clouds" "A Huge Fan"',
    '"Anna Claire Clouds" "Double Delight"',
    '"Anna Claire Clouds" VR scene',
    '"Anna Claire Clouds" VR preview',
    '"Anna Claire Clouds" VR trailer',
    '"Anna Claire Clouds" virtual reality',
    '"Anna Claire Clouds" scene preview',
    '"Anna Claire Clouds" scene thumbnail',
    '"Anna Claire Clouds" video still',
    '"Anna Claire Clouds" trailer',
    '"Anna Claire Clouds" preview',
    '"Anna Claire Clouds" scene',
    '"Anna Claire Clouds" Dark Side trailer',
    '"Anna Claire Clouds" Cassex trailer',
    '"Ana Clouds" POV',
    '"Anna Clouds" POV',
]

KNOWN = [
    "happy little clouds", "a girl and her canvas", "intimately pov", "pov hookups",
    "mr big pov", "manuels fucking pov 14", "vr bangers", "a huge fan",
    "double delight", "dark side", "cassex",
]

BAD = [
    "airplane", "aircraft", "cloudscape", "cloud computing", "cloud storage", "weather",
    "cumulus", "aviation", "flight", "sky wallpaper", "cake", "dessert", "kitten", "cat photo",
]


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", html.unescape(s).lower())


def relevance(meta: dict, query: str) -> int:
    text = html.unescape(" ".join(str(meta.get(k) or "") for k in ("t", "purl", "murl", "desc"))).lower()
    ct = compact(text)
    score = 0
    if "annaclaireclouds" in ct or "annaclairclouds" in ct:
        score += 10
    elif "anaclouds" in ct or "annaclouds" in ct:
        score += 7
    for title in KNOWN:
        if compact(title) in ct:
            score += 7
    if "pov" in text:
        score += 3
    if "vr" in text or "virtual reality" in text:
        score += 2
    if any(x in text for x in BAD) and score < 10:
        return -20
    return score


def save_sheet(rows):
    tw, th, lh, cols, gap = 180, 112, 26, 8, 6
    rc = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (tw + gap), gap + rc * (th + lh + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + lh + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                canvas.paste(ImageOps.fit(im.convert("RGB"), (tw, th), method=Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 3, y + th + 3), f"#{i+1:03d} q{row['query_index']} r{row['rank']}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    rows, hashes, exact = [], [], set()

    with sync_playwright() as p:
        chrome = next((x for x in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser") if Path(x).exists()), None)
        browser = p.chromium.launch(
            headless=True,
            executable_path=chrome,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1.35,
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(7000)

        for qi, query in enumerate(QUERIES, 1):
            if len(rows) >= TARGET:
                break
            url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2&adlt=off"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(900)
            except Exception as exc:
                print(f"NAV_FAIL q{qi}: {exc}", flush=True)
                continue

            for label in ("Accept", "Agree", "I agree", "Accept all"):
                try:
                    page.get_by_role("button", name=label).click(timeout=400)
                except Exception:
                    pass

            last_count, stagnant = 0, 0
            for _ in range(18):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(260)
                count = page.locator("a.iusc").count()
                stagnant = stagnant + 1 if count <= last_count else 0
                last_count = count
                if stagnant >= 4 or count >= 170:
                    break
                try:
                    more = page.get_by_text("See more images", exact=False)
                    if more.count():
                        more.last.click(timeout=500)
                except Exception:
                    pass

            cards = page.locator("a.iusc")
            count = min(cards.count(), 175)
            kept_this_query = 0
            print(f"QUERY q{qi} cards={count} {query}", flush=True)

            for rank in range(count):
                if len(rows) >= TARGET:
                    break
                card = cards.nth(rank)
                try:
                    raw_meta = card.get_attribute("m") or "{}"
                    meta = json.loads(raw_meta)
                except Exception:
                    continue
                score = relevance(meta, query)
                if score < 9:
                    continue
                img = card.locator("img.mimg, img").first
                try:
                    if not img.is_visible():
                        img.scroll_into_view_if_needed(timeout=1000)
                        page.wait_for_timeout(60)
                    box = img.bounding_box()
                    if not box or box["width"] < 100 or box["height"] < 70:
                        continue
                    raw = img.screenshot(type="jpeg", quality=93, timeout=4000)
                except Exception:
                    continue

                sha = hashlib.sha256(raw).hexdigest()
                if sha in exact:
                    continue
                try:
                    with Image.open(BytesIO(raw)) as im:
                        im.load(); im = im.convert("RGB")
                        if im.width < 100 or im.height < 70:
                            continue
                        ph = imagehash.phash(im)
                        if any((ph - old) <= 2 for old in hashes):
                            continue
                        ratio = im.width / im.height
                        idx = len(rows) + 1
                        fn = f"anna_claire_clouds_pov_{idx:03d}.jpg"
                        if im.width < 560:
                            scale = 560 / im.width
                            im = im.resize((560, max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
                        im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue

                exact.add(sha); hashes.append(ph); kept_this_query += 1
                rows.append({
                    "index": idx, "filename": fn, "query_index": qi, "query": query,
                    "rank": rank + 1, "score": score, "title": str(meta.get("t") or ""),
                    "source_page": str(meta.get("purl") or ""), "image_url": str(meta.get("murl") or ""),
                    "ratio": f"{ratio:.3f}",
                })
            print(f"KEPT q{qi}={kept_this_query} TOTAL={len(rows)}", flush=True)
        browser.close()

    fields = ["index", "filename", "query_index", "query", "rank", "score", "title", "source_page", "image_url", "ratio"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    save_sheet(rows)
    (OUT / "README.txt").write_text(
        f"Fresh browser capture of Bing Images thumbnails. Collected: {len(rows)}.\n"
        "No files from previous harvests were used. Each image records the exact query and visible result rank.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_fresh_browser_pov_300", "zip", OUT)
    Path("FRESH_BROWSER_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)

if __name__ == "__main__":
    main()
