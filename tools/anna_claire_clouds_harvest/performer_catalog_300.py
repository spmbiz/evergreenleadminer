#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import os
import random
import shutil
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("catalog300_output")
PHOTOS = OUT / "photos"

SOURCES = [
    ("pornhub", "https://www.pornhub.com/pornstar/anna-claire-clouds/videos"),
    ("xhamster", "https://xhamster.com/pornstars/anna-claire-clouds"),
    ("youporn", "https://www.youporn.com/pornstar/anna-claire-clouds/"),
    ("redtube", "https://www.redtube.com/pornstar/anna-claire-clouds"),
    ("tube8", "https://www.tube8.com/pornstar/anna-claire-clouds/"),
]


def page_variants(base: str):
    yield base
    sep = "&" if "?" in base else "?"
    for n in range(2, 9):
        yield f"{base}{sep}page={n}"


def sheet(rows):
    tw, th, lh, cols, gap = 180, 104, 22, 8, 6
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
            draw.text((x + 3, y + th + 4), f"#{i+1:03d} {row['source'][:8]}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def dismiss_gates(page):
    labels = [
        "I am 18", "I'm 18", "Enter", "Continue", "Accept", "Accept all",
        "Agree", "I agree", "Yes, I am 18", "I am over 18", "Proceed",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count():
                btn.first.click(timeout=600)
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=label, exact=False)
            if link.count():
                link.first.click(timeout=600)
        except Exception:
            pass


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    rows = []
    exact = set()
    phashes = []

    with sync_playwright() as p:
        chrome = next((x for x in ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"] if Path(x).exists()), None)
        browser = p.chromium.launch(headless=True, executable_path=chrome, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1.25,
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(6500)

        for source, base in SOURCES:
            if len(rows) >= TARGET:
                break
            for pageno, url in enumerate(page_variants(base), 1):
                if len(rows) >= TARGET:
                    break
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=22000)
                    page.wait_for_timeout(1200)
                except Exception as exc:
                    print(f"NAV_FAIL {source} p{pageno}: {exc}", flush=True)
                    continue
                dismiss_gates(page)

                stagnant = 0
                last = 0
                for _ in range(28):
                    page.mouse.wheel(0, 2600)
                    page.wait_for_timeout(300)
                    c = page.locator("img").count()
                    if c <= last:
                        stagnant += 1
                    else:
                        stagnant = 0
                    last = c
                    if stagnant >= 5:
                        break

                imgs = page.locator("img")
                count = min(imgs.count(), 500)
                print(f"PAGE {source} {pageno} images={count} url={page.url}", flush=True)
                order = list(range(count))
                random.shuffle(order)
                accepted_before = len(rows)

                for i in order:
                    if len(rows) >= TARGET:
                        break
                    el = imgs.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        box = el.bounding_box()
                        if not box:
                            continue
                        w, h = box["width"], box["height"]
                        ratio = w / max(h, 1)
                        if w < 175 or h < 90 or ratio < 1.2 or ratio > 2.25:
                            continue
                        alt = (el.get_attribute("alt") or "").strip()
                        src = (el.get_attribute("src") or el.get_attribute("data-src") or el.get_attribute("data-original") or "").strip()
                        low = f"{alt} {src}".lower()
                        if any(x in low for x in ["logo", "avatar", "icon", "banner", "sprite", "flag", "emoji", "ads"]):
                            continue
                        raw = el.screenshot(type="jpeg", quality=92, timeout=5000)
                    except Exception:
                        continue
                    sha = hashlib.sha256(raw).hexdigest()
                    if sha in exact:
                        continue
                    try:
                        with Image.open(BytesIO(raw)) as im:
                            im.load()
                            im = im.convert("RGB")
                            if im.width < 160 or im.height < 85:
                                continue
                            ph = imagehash.phash(im)
                            if any((ph - old) <= 2 for old in phashes):
                                continue
                            if im.width < 560:
                                scale = 560 / im.width
                                im = im.resize((560, int(im.height * scale)), Image.Resampling.LANCZOS)
                            idx = len(rows) + 1
                            fn = f"anna_claire_clouds_scene_{idx:03d}.jpg"
                            im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
                    except (UnidentifiedImageError, OSError, ValueError):
                        continue
                    exact.add(sha)
                    phashes.append(ph)
                    rows.append({
                        "index": idx,
                        "filename": fn,
                        "source": source,
                        "page": page.url,
                        "alt": alt,
                        "image_src": src,
                        "ratio": f"{ratio:.3f}",
                    })
                gained = len(rows) - accepted_before
                print(f"ACCEPTED {source} p{pageno}: +{gained}, total={len(rows)}", flush=True)
                if gained == 0 and pageno >= 3:
                    break
        browser.close()

    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["index", "filename", "source", "page", "alt", "image_src", "ratio"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    sheet(rows)
    (OUT / "README.txt").write_text(
        f"Public video-card thumbnails captured from performer-specific catalog pages.\nCollected: {len(rows)}\n"
        "No full videos or paywalled files were downloaded. Source pages are in manifest.csv.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_catalog_scene_300", "zip", OUT)
    Path("CATALOG300_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
