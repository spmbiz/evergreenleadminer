#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import random
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("site_locked_output")
PHOTOS = OUT / "photos"

SEARCHES = [
    ("pornhub.com", 'site:pornhub.com/view_video.php "Anna Claire Clouds"'),
    ("xhamster.com", 'site:xhamster.com/videos "Anna Claire Clouds"'),
    ("youporn.com", 'site:youporn.com/watch "Anna Claire Clouds"'),
    ("redtube.com", 'site:redtube.com "Anna Claire Clouds"'),
    ("spankbang.com", 'site:spankbang.com "Anna Claire Clouds"'),
    ("povr.com", 'site:povr.com "Anna Claire Clouds"'),
    ("vrbangers.com", 'site:vrbangers.com "Anna Claire Clouds"'),
    ("brazzers.com", 'site:brazzers.com "Anna Claire Clouds"'),
    ("realitykings.com", 'site:realitykings.com "Anna Claire Clouds"'),
    ("teamskeet.com", 'site:teamskeet.com "Anna Claire Clouds"'),
    ("adulttime.com", 'site:adulttime.com "Anna Claire Clouds"'),
    ("deeper.com", 'site:deeper.com "Anna Claire Clouds"'),
    ("vixen.com", 'site:vixen.com "Anna Claire Clouds"'),
    ("blacked.com", 'site:blacked.com "Anna Claire Clouds"'),
    ("tushy.com", 'site:tushy.com "Anna Claire Clouds"'),
    ("evilangel.com", 'site:evilangel.com "Anna Claire Clouds"'),
    ("mofos.com", 'site:mofos.com "Anna Claire Clouds"'),
    ("wicked.com", 'site:wicked.com "Anna Claire Clouds"'),
    ("girlsway.com", 'site:girlsway.com "Anna Claire Clouds"'),
    ("girlfriendsfilms.com", 'site:girlfriendsfilms.com "Anna Claire Clouds"'),
    ("twistys.com", 'site:twistys.com "Anna Claire Clouds"'),
    ("cherrypimps.com", 'site:cherrypimps.com "Anna Claire Clouds"'),
    ("intimatelypov.net", 'site:intimatelypov.net "Anna Claire Clouds"'),
    ("avn.com", 'site:avn.com "Anna Claire Clouds" scene'),
]


def normalized(s):
    return re.sub(r"[^a-z0-9]", "", html.unescape(s).lower())


def relevant(meta_raw: str, expected_domain: str):
    try:
        m = json.loads(meta_raw or "{}")
    except Exception:
        return False, {}, ""
    purl = str(m.get("purl") or "")
    title = str(m.get("t") or "")
    murl = str(m.get("murl") or "")
    host = urlparse(purl).netloc.lower().removeprefix("www.")
    if not (host == expected_domain or host.endswith("." + expected_domain)):
        return False, m, purl
    blob = normalized(f"{title} {purl} {murl}")
    ok = "annaclaireclouds" in blob or "annaclairclouds" in blob or all(x in blob for x in ["anna", "claire", "cloud"])
    return ok, m, purl


def make_sheet(rows):
    tw, th, lh, cols, gap = 180, 106, 22, 8, 6
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
            draw.text((x + 3, y + th + 4), f"#{i+1:03d} {row['domain'][:9]}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    rows, hashes, exact = [], [], set()

    with sync_playwright() as p:
        chrome = next((x for x in ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"] if Path(x).exists()), None)
        browser = p.chromium.launch(headless=True, executable_path=chrome, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.3, locale="en-US", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36")
        page = context.new_page()
        page.set_default_timeout(7000)

        for domain, query in SEARCHES:
            if len(rows) >= TARGET:
                break
            url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2&adlt=off"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=22000)
                page.wait_for_timeout(1200)
            except Exception as exc:
                print(f"NAV_FAIL {domain}: {exc}", flush=True)
                continue
            last = stagnant = 0
            for _ in range(25):
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(280)
                c = page.locator("a.iusc").count()
                if c <= last:
                    stagnant += 1
                else:
                    stagnant = 0
                last = c
                if stagnant >= 4 or c >= 190:
                    break
            cards = page.locator("a.iusc")
            count = min(cards.count(), 220)
            accepted_before = len(rows)
            for i in range(count):
                if len(rows) >= TARGET:
                    break
                card = cards.nth(i)
                try:
                    meta_raw = card.get_attribute("m") or ""
                    ok, meta, purl = relevant(meta_raw, domain)
                    if not ok:
                        continue
                    img = card.locator("img.mimg, img").first
                    if not img.is_visible():
                        continue
                    box = img.bounding_box()
                    if not box or box["width"] < 120 or box["height"] < 80:
                        continue
                    raw = img.screenshot(type="jpeg", quality=93, timeout=5000)
                except Exception:
                    continue
                sha = hashlib.sha256(raw).hexdigest()
                if sha in exact:
                    continue
                try:
                    with Image.open(BytesIO(raw)) as im:
                        im.load(); im = im.convert("RGB")
                        ratio = im.width / im.height
                        if ratio < 1.1 or ratio > 2.4:
                            continue
                        ph = imagehash.phash(im)
                        if any((ph-old) <= 1 for old in hashes):
                            continue
                        if im.width < 600:
                            scale = 600 / im.width
                            im = im.resize((600, int(im.height*scale)), Image.Resampling.LANCZOS)
                        idx = len(rows) + 1
                        fn = f"anna_claire_clouds_scene_{idx:03d}.jpg"
                        im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue
                exact.add(sha); hashes.append(ph)
                rows.append({
                    "index": idx,
                    "filename": fn,
                    "domain": domain,
                    "query": query,
                    "title": str(meta.get("t") or ""),
                    "source_page": purl,
                    "image_url": str(meta.get("murl") or ""),
                    "ratio": f"{ratio:.3f}",
                })
            print(f"DOMAIN {domain}: +{len(rows)-accepted_before} total={len(rows)} cards={count}", flush=True)
        browser.close()

    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["index", "filename", "domain", "query", "title", "source_page", "image_url", "ratio"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    make_sheet(rows)
    (OUT / "README.txt").write_text(f"Site-locked public scene thumbnails. Collected: {len(rows)}. Every accepted result matches the requested domain and Anna Claire Clouds in Bing metadata.\n", encoding="utf-8")
    shutil.make_archive("anna_claire_clouds_site_locked_scene_300", "zip", OUT)
    Path("SITELOCK_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
