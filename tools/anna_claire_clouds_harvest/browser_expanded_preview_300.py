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
from urllib.parse import quote_plus

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright

TARGET = int(os.getenv("TARGET_COUNT", "300"))
OUT = Path("expanded_preview300")
PHOTOS = OUT / "photos"

QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POV thumbnail',
    '"Anna Claire Clouds" POV video still',
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
    '"Anna Claire Clouds" VR',
    '"Anna Claire Clouds" VR scene',
    '"Anna Claire Clouds" VR preview',
    '"Anna Claire Clouds" VR trailer',
    '"Anna Claire Clouds" virtual reality',
    '"Anna Claire Clouds" scene preview',
    '"Anna Claire Clouds" scene thumbnail',
    '"Anna Claire Clouds" video still',
    '"Anna Claire Clouds" trailer',
    '"Anna Claire Clouds" preview',
    '"Anna Claire Clouds" "Dark Side" trailer',
    '"Anna Claire Clouds" Cassex trailer',
    '"Anna Claire Clouds" "Blacked Raw" scene',
    '"Anna Claire Clouds" "Going Up" preview',
    '"Anna Claire Clouds" "Massive Asses 13"',
    '"Anna Claire Clouds" "Listen to Your Body"',
    '"Anna Claire Clouds" "Dirty Talk" scene',
    '"Anna Claire Clouds" "Bound to Please Her"',
    '"Anna Claire Clouds" "Method to Her Badness"',
    '"Anna Claire Clouds" "Karter Kreation"',
    '"Anna Claire Clouds" "Lawless" scene',
    '"Anna Claire Clouds" "Fusion" preview',
    '"Anna Claire Clouds" "Can\'t Makeup My Mind"',
    '"Ana Clouds" POV',
]

SCENE_TERMS = [
    "pov", "povr", "vr", "virtual reality", "scene", "preview", "trailer",
    "video still", "thumbnail", "happy little clouds", "a girl and her canvas",
    "intimately pov", "pov hookups", "mr big pov", "manuel", "vr bangers",
    "a huge fan", "double delight", "dark side", "cassex", "blacked raw",
    "going up", "massive asses", "listen to your body", "dirty talk",
    "bound to please her", "method to her badness", "karter kreation", "lawless",
    "fusion", "makeup my mind",
]

BLOCK_TERMS = [
    "award", "awards", "avn expo", "xbiz", "red carpet", "gala", "interview",
    "podcast", "headshot", "portrait", "biography", "wikipedia", "wikimedia",
    "instagram", "tiktok", "facebook", "onlyfans", "getty", "event photo",
]


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", html.unescape(s).lower())


def metadata_text(meta: dict) -> str:
    return html.unescape(" ".join(str(meta.get(k) or "") for k in ("t", "desc", "purl", "murl"))).lower()


def relevant(meta: dict) -> bool:
    text = metadata_text(meta)
    ct = compact(text)
    has_name = any(x in ct for x in ("annaclaireclouds", "annaclairclouds", "anaclouds", "annaclouds"))
    has_scene = any(term in text for term in SCENE_TERMS)
    blocked = any(term in text for term in BLOCK_TERMS)
    return has_name and has_scene and not blocked


def make_sheet(rows):
    tw, th, lh, cols, gap = 200, 125, 24, 7, 6
    nrows = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (tw + gap), gap + nrows * (th + lh + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + lh + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                canvas.paste(ImageOps.contain(im.convert("RGB"), (tw, th), method=Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 3, y + th + 3), f"#{i+1:03d} q{row['query_index']} r{row['rank']}", fill="black", font=font)
        except OSError:
            pass
    canvas.save(OUT / "contact_sheet.jpg", "JPEG", quality=88, optimize=True)


def largest_visible_image(page):
    # Choose the largest visible image after opening a result, while ignoring the search grid.
    handle = page.evaluate_handle("""
    () => {
      const imgs = [...document.images];
      const scored = imgs.map(img => {
        const r = img.getBoundingClientRect();
        const cs = getComputedStyle(img);
        const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
        const inGrid = !!img.closest('a.iusc');
        const src = img.currentSrc || img.src || '';
        const bad = /logo|icon|avatar|sprite|favicon/i.test(src + ' ' + (img.alt || ''));
        return {img, area:r.width*r.height, width:r.width, height:r.height, visible, inGrid, bad};
      }).filter(x => x.visible && !x.inGrid && !x.bad && x.width >= 420 && x.height >= 230);
      scored.sort((a,b) => b.area-a.area);
      return scored.length ? scored[0].img : null;
    }
    """)
    try:
        return handle.as_element()
    except Exception:
        return None


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    rows, exact, phashes = [], set(), []

    with sync_playwright() as p:
        chrome = next((x for x in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser") if Path(x).exists()), None)
        browser = p.chromium.launch(headless=True, executable_path=chrome, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
        ])
        context = browser.new_context(
            viewport={"width": 1700, "height": 1100},
            device_scale_factor=1.2,
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        context.add_cookies([
            {"name":"SRCHHPGUSR", "value":"ADLT=OFF&NRSLT=50", "domain":".bing.com", "path":"/"},
            {"name":"SRCHUSR", "value":"DOB=20200101", "domain":".bing.com", "path":"/"},
        ])
        page = context.new_page()
        page.set_default_timeout(6000)

        for qi, query in enumerate(QUERIES, 1):
            if len(rows) >= TARGET:
                break
            url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2&adlt=off"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1000)
            except Exception as exc:
                print(f"NAV_FAIL q{qi}: {exc}", flush=True)
                continue

            for label in ("Accept", "Agree", "I agree", "Accept all"):
                try:
                    page.get_by_role("button", name=label).click(timeout=400)
                except Exception:
                    pass

            last, stagnant = 0, 0
            for _ in range(14):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(260)
                count = page.locator("a.iusc").count()
                stagnant = stagnant + 1 if count <= last else 0
                last = count
                if stagnant >= 3 or count >= 140:
                    break
                try:
                    more = page.get_by_text("See more images", exact=False)
                    if more.count():
                        more.last.click(timeout=400)
                except Exception:
                    pass

            cards = page.locator("a.iusc")
            count = min(cards.count(), 140)
            kept_q = 0
            print(f"QUERY q{qi} cards={count} {query}", flush=True)

            for rank in range(count):
                if len(rows) >= TARGET:
                    break
                card = cards.nth(rank)
                try:
                    meta = json.loads(card.get_attribute("m") or "{}")
                except Exception:
                    continue
                if not relevant(meta):
                    continue
                try:
                    card.scroll_into_view_if_needed(timeout=1000)
                    page.wait_for_timeout(40)
                    card.click(timeout=2500, force=True)
                    page.wait_for_timeout(650)
                except Exception:
                    continue

                preview = largest_visible_image(page)
                if preview is None:
                    continue
                try:
                    box = preview.bounding_box()
                    if not box or box["width"] < 420 or box["height"] < 230:
                        continue
                    raw = preview.screenshot(type="jpeg", quality=93, timeout=5000)
                except Exception:
                    continue

                sha = hashlib.sha256(raw).hexdigest()
                if sha in exact:
                    continue
                try:
                    with Image.open(BytesIO(raw)) as im:
                        im.load(); im = ImageOps.exif_transpose(im).convert("RGB")
                        if im.width / max(1, im.height) < 1.15:
                            continue
                        ph = imagehash.phash(im)
                        if any((ph - old) <= 2 for old in phashes):
                            continue
                        idx = len(rows) + 1
                        fn = f"anna_claire_clouds_scene_preview_{idx:03d}.jpg"
                        if im.width < 720:
                            scale = 720 / im.width
                            im = im.resize((720, max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
                        im.save(PHOTOS / fn, "JPEG", quality=93, optimize=True, progressive=True)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue

                exact.add(sha); phashes.append(ph); kept_q += 1
                rows.append({
                    "index": idx, "filename": fn, "query_index": qi, "query": query,
                    "rank": rank + 1, "title": str(meta.get("t") or ""),
                    "source_page": str(meta.get("purl") or ""), "image_url": str(meta.get("murl") or ""),
                })
            print(f"KEPT q{qi}={kept_q} TOTAL={len(rows)}", flush=True)
        browser.close()

    fields = ["index", "filename", "query_index", "query", "rank", "title", "source_page", "image_url"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    make_sheet(rows)
    (OUT / "README.txt").write_text(
        f"Fresh expanded-preview browser harvest. Collected: {len(rows)}.\n"
        "Each image was captured from the enlarged preview after clicking a scene/POV result.\n"
        "Awards, gala, interview, podcast and portrait results were excluded by metadata.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_expanded_scene_previews", "zip", OUT)
    Path("EXPANDED_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print(f"FINAL_COUNT={len(rows)}", flush=True)

if __name__ == "__main__":
    main()
