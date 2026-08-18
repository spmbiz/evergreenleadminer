#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import os
import random
import shutil
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("browser_scene300")
PHOTOS = OUT / "photos"
QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" VR',
    '"Anna Claire Clouds" VR scene',
    '"Anna Claire Clouds" trailer',
    '"Anna Claire Clouds" preview',
    '"Anna Claire Clouds" scene still',
    '"Anna Claire Clouds" video thumbnail',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" VR Bangers',
    '"Anna Claire Clouds" Adult Time',
    '"Anna Claire Clouds" Deeper',
    '"Anna Claire Clouds" Brazzers',
    '"Anna Claire Clouds" Reality Kings',
]


def save_sheet(rows):
    tw, th, lh, cols, gap = 180, 112, 22, 8, 6
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
            draw.text((x + 3, y + th + 4), f"#{i+1:03d} {row['query_index']}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    rows = []
    hashes = []
    exact = set()

    with sync_playwright() as p:
        chrome = None
        for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if Path(candidate).exists():
                chrome = candidate
                break
        browser = p.chromium.launch(
            headless=True,
            executable_path=chrome,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1.3,
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(8000)

        for qi, query in enumerate(QUERIES, 1):
            if len(rows) >= TARGET:
                break
            url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2&adlt=moderate"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1200)
            except Exception as exc:
                print(f"NAV_FAIL {qi}: {exc}", flush=True)
                continue

            # Dismiss occasional consent overlays.
            for label in ("Accept", "Agree", "I agree", "Accept all"):
                try:
                    page.get_by_role("button", name=label).click(timeout=500)
                except Exception:
                    pass

            last_count = 0
            stagnant = 0
            for _ in range(22):
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(350)
                count = page.locator("a.iusc img.mimg, img.mimg").count()
                if count <= last_count:
                    stagnant += 1
                else:
                    stagnant = 0
                last_count = count
                if stagnant >= 4 or count >= 180:
                    break
                try:
                    more = page.get_by_text("See more images", exact=False)
                    if more.count():
                        more.last.click(timeout=700)
                except Exception:
                    pass

            loc = page.locator("a.iusc img.mimg, img.mimg")
            count = min(loc.count(), 190)
            print(f"QUERY {qi}/{len(QUERIES)} count={count} {query}", flush=True)

            order = list(range(count))
            random.shuffle(order)
            for j in order:
                if len(rows) >= TARGET:
                    break
                el = loc.nth(j)
                try:
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] < 120 or box["height"] < 85:
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
                        if im.width < 120 or im.height < 80:
                            continue
                        ph = imagehash.phash(im)
                        if any((ph - old) <= 2 for old in hashes):
                            continue
                        ratio = im.width / im.height
                        orientation = "landscape" if ratio >= 1.18 else ("portrait" if ratio <= 0.88 else "square")
                        idx = len(rows) + 1
                        fn = f"anna_claire_clouds_scene_{idx:03d}.jpg"
                        # Upscale tiny thumbnails to a practical minimum without changing content.
                        if im.width < 480:
                            scale = 480 / im.width
                            im = im.resize((480, max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
                        im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue
                exact.add(sha)
                hashes.append(ph)
                try:
                    parent = el.locator("xpath=ancestor::a[contains(@class,'iusc')][1]")
                    source_meta = parent.get_attribute("m") or ""
                except Exception:
                    source_meta = ""
                rows.append({
                    "index": idx,
                    "filename": fn,
                    "query_index": qi,
                    "query": query,
                    "orientation": orientation,
                    "ratio": f"{ratio:.3f}",
                    "bing_metadata": source_meta[:2000],
                })
            print(f"ACCEPTED_TOTAL={len(rows)}", flush=True)
        browser.close()

    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["index", "filename", "query_index", "query", "orientation", "ratio", "bing_metadata"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    save_sheet(rows)
    counts = {k: sum(1 for r in rows if r["orientation"] == k) for k in ("landscape", "portrait", "square")}
    (OUT / "README.txt").write_text(
        f"Direct browser screenshots of public Bing Images result thumbnails.\nCollected: {len(rows)}\n"
        f"Landscape: {counts['landscape']} | Portrait: {counts['portrait']} | Square: {counts['square']}\n"
        "Each row records the exact query and Bing result metadata.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_300_scene_grid", "zip", OUT)
    Path("BROWSER300_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
