#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
OUT = Path("google_dom_output")
PHOTOS = OUT / "photos"
PAGES = OUT / "google_pages"

QUERIES = [
    '"Anna Claire Clouds" POV',
    '"Anna Claire Clouds" POV scene',
    '"Anna Claire Clouds" POV preview',
    '"Anna Claire Clouds" POV trailer',
    '"Anna Claire Clouds" POVR',
    '"Anna Claire Clouds" "Happy Little Clouds"',
    '"Anna Claire Clouds" "Intimately POV"',
    '"Anna Claire Clouds" "A Girl and Her Canvas"',
    '"Anna Claire Clouds" "Manuel\'s Fucking POV 14"',
    '"Anna Claire Clouds" "My Mom\'s Roommate"',
    '"Anna Claire Clouds" "POV Hookups"',
    '"Anna Claire Clouds" "VR Bangers"',
]

BLOCKED_TEXT = (
    "gangbang", "bukkake", "cumshot", "blowjob", "anal sex", "anal-sex",
    "hardcore", "full scene", "full-scene", "leak", "torrent", "rule34",
)


def parse_ref_url(href: str) -> str:
    if not href:
        return ""
    try:
        q = parse_qs(urlparse(href).query)
        return (q.get("imgrefurl") or q.get("url") or q.get("q") or [""])[0]
    except Exception:
        return ""


def click_consent(page) -> None:
    for text in ("Accept all", "I agree", "Accept everything", "Tout accepter"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(text)}$", re.I))
            if btn.count():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def make_contact_sheet(rows: list[dict[str, str]]) -> None:
    thumb = 160
    label_h = 34
    cols = 8
    gap = 7
    nrows = (len(rows) + cols - 1) // cols
    canvas = Image.new("RGB", (gap + cols * (thumb + gap), gap + nrows * (thumb + label_h + gap)), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (thumb + gap)
        y = gap + (i // cols) * (thumb + label_h + gap)
        try:
            with Image.open(PHOTOS / row["filename"]) as im:
                fitted = ImageOps.fit(im.convert("RGB"), (thumb, thumb), method=Image.Resampling.LANCZOS)
                canvas.paste(fitted, (x, y))
        except OSError:
            continue
        draw.text((x + 2, y + thumb + 2), f"#{i+1:03d} {row['query_short'][:24]}", fill="black", font=font)
        draw.text((x + 2, y + thumb + 17), row["source_domain"][:24], fill="black", font=font)
    canvas.save(OUT / "contact_sheet.jpg", quality=89, optimize=True)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    PHOTOS.mkdir(parents=True)
    PAGES.mkdir(parents=True)

    rows: list[dict[str, str]] = []
    phashes: list[imagehash.ImageHash] = []
    exact_hashes: set[str] = set()

    with sync_playwright() as p:
        chrome = "/usr/bin/google-chrome"
        if not Path(chrome).exists():
            chrome = "/usr/bin/google-chrome-stable"
        browser = p.chromium.launch(
            headless=True,
            executable_path=chrome if Path(chrome).exists() else None,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1440,2200",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 2100},
            locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            device_scale_factor=1,
        )
        context.add_cookies([
            {"name": "CONSENT", "value": "YES+cb.20240602-08-p0.en+FX+410", "domain": ".google.com", "path": "/"},
            {"name": "SOCS", "value": "CAESHAgBEhIaAB", "domain": ".google.com", "path": "/"},
        ])
        page = context.new_page()
        page.set_default_timeout(5000)

        for qi, query in enumerate(QUERIES, 1):
            if len(rows) >= TARGET:
                break
            url = f"https://www.google.com/search?tbm=isch&udm=2&safe=off&hl=en&q={quote_plus(query)}"
            print(f"QUERY {qi}/{len(QUERIES)}: {query}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            click_consent(page)
            page.wait_for_timeout(1500)

            title = page.title()
            body_text = ""
            try:
                body_text = page.locator("body").inner_text(timeout=2500)[:1000]
            except Exception:
                pass
            if "unusual traffic" in body_text.lower() or "not a robot" in body_text.lower():
                print("Google challenge detected", flush=True)
                continue

            # Save the actual Google result viewport for audit/reference.
            try:
                page.screenshot(path=str(PAGES / f"google_{qi:02d}.jpg"), type="jpeg", quality=82, full_page=False)
            except Exception:
                pass

            for _ in range(12):
                page.mouse.wheel(0, 1900)
                page.wait_for_timeout(350)
                for label in ("Show more results", "More results"):
                    try:
                        b = page.get_by_role("button", name=re.compile(label, re.I))
                        if b.count() and b.first.is_visible():
                            b.first.click(timeout=700)
                    except Exception:
                        pass

            imgs = page.locator("img")
            count = imgs.count()
            accepted_this_query = 0
            print(f"DOM images: {count}", flush=True)
            for j in range(count):
                if len(rows) >= TARGET or accepted_this_query >= 32:
                    break
                img = imgs.nth(j)
                try:
                    info = img.evaluate("""el => ({
                        w: el.naturalWidth || el.width || 0,
                        h: el.naturalHeight || el.height || 0,
                        alt: el.alt || '',
                        src: el.currentSrc || el.src || '',
                        cls: el.className || '',
                        href: el.closest('a') ? el.closest('a').href : ''
                    })""")
                except Exception:
                    continue
                w = int(info.get("w") or 0)
                h = int(info.get("h") or 0)
                if w < 120 or h < 90 or max(w / max(h, 1), h / max(w, 1)) > 4.2:
                    continue
                alt = str(info.get("alt") or "")
                href = str(info.get("href") or "")
                src = str(info.get("src") or "")
                surrounding = f"{alt} {href} {src}".lower()
                if any(term in surrounding for term in BLOCKED_TEXT):
                    continue
                if any(x in surrounding for x in ("googlelogo", "gstatic.com/images/branding", "encrypted-tbn0.gstatic.com/images?q=tbn:and9gctool")):
                    continue
                tmp = OUT / "_candidate.png"
                try:
                    img.scroll_into_view_if_needed(timeout=1500)
                    page.wait_for_timeout(60)
                    img.screenshot(path=str(tmp), timeout=2500)
                    with Image.open(tmp) as im:
                        im.load()
                        im = ImageOps.exif_transpose(im).convert("RGB")
                        if im.width < 110 or im.height < 80:
                            continue
                        ph = imagehash.phash(im)
                        import hashlib
                        raw_hash = hashlib.sha256(tmp.read_bytes()).hexdigest()
                        if raw_hash in exact_hashes or any((ph - old) <= 2 for old in phashes):
                            continue
                        idx = len(rows) + 1
                        fn = f"anna_claire_clouds_google_pov_{idx:03d}.jpg"
                        im.save(PHOTOS / fn, "JPEG", quality=92, optimize=True, progressive=True)
                        exact_hashes.add(raw_hash)
                        phashes.append(ph)
                except Exception:
                    continue
                source_page = parse_ref_url(href)
                domain = urlparse(source_page).netloc.lower().removeprefix("www.") if source_page else "google-result"
                qshort = query.replace('"Anna Claire Clouds" ', "").replace('"', "")
                rows.append({
                    "index": str(len(rows) + 1),
                    "filename": fn,
                    "query": query,
                    "query_short": qshort,
                    "alt_text": alt,
                    "google_result_href": href,
                    "source_page": source_page,
                    "source_domain": domain,
                    "thumbnail_src": src,
                    "natural_width": str(w),
                    "natural_height": str(h),
                })
                accepted_this_query += 1
                print(f"accepted {len(rows)}: {qshort} | {domain} | {alt[:60]}", flush=True)

        browser.close()

    fields = [
        "index", "filename", "query", "query_short", "alt_text",
        "google_result_href", "source_page", "source_domain", "thumbnail_src",
        "natural_width", "natural_height",
    ]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    make_contact_sheet(rows)
    (OUT / "README.txt").write_text(
        "Real Chrome screenshots of thumbnails visibly returned by Google Images for exact Anna Claire Clouds + POV/video-preview queries.\n"
        f"Collected: {len(rows)} unique thumbnail screenshots.\n"
        "google_pages/ contains viewport screenshots of the search result pages; manifest.csv records query and source metadata.\n",
        encoding="utf-8",
    )
    Path("GOOGLE_DOM_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    shutil.make_archive("anna_claire_clouds_google_pov_real_results", "zip", OUT)
    print(f"FINAL_COUNT={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
