#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import imagehash
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

TARGET = int(os.getenv("TARGET_COUNT", "200"))
ROOT = Path(__file__).resolve().parent
RAW = ROOT / "social_raw"
OUT = ROOT / "output"
PHOTOS = OUT / "photos"
LOGS = ROOT / "logs"


def run(name: str, command: list[str]) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    print("\n===", name, "===")
    print(" ".join(command))
    proc = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (LOGS / f"{name}.log").write_text(proc.stdout, encoding="utf-8", errors="replace")
    print(proc.stdout[-5000:])
    print("exit:", proc.returncode)
    return proc.returncode


def count_images(path: Path) -> int:
    exts = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    return sum(1 for p in path.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def metadata_for(path: Path) -> dict:
    candidates = [
        path.with_suffix(path.suffix + ".json"),
        path.with_suffix(".json"),
    ]
    for meta in candidates:
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def source_url(meta: dict, path: Path) -> str:
    for key in ("post_url", "url", "webpage_url", "source", "post_shortcode"):
        value = meta.get(key)
        if value:
            if key == "post_shortcode" and not str(value).startswith("http"):
                return f"https://www.instagram.com/p/{value}/"
            return str(value)
    parts = {x.lower() for x in path.parts}
    if "instagram_gallerydl" in parts or "instaloader" in parts:
        return "https://www.instagram.com/annaclairecloudstv/"
    if "facebook_gallerydl" in parts:
        return "https://www.facebook.com/annaclaireclouds/photos/"
    return ""


def make_contact_sheet(rows: list[dict[str, str]]) -> None:
    thumb, gap, label, cols = 150, 8, 22, 10
    nrows = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (gap + cols * (thumb + gap), gap + nrows * (thumb + label + gap)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x = gap + (i % cols) * (thumb + gap)
        y = gap + (i // cols) * (thumb + label + gap)
        with Image.open(PHOTOS / row["filename"]) as im:
            tile = ImageOps.fit(im.convert("RGB"), (thumb, thumb), method=Image.Resampling.LANCZOS)
        sheet.paste(tile, (x, y))
        draw.text((x + 3, y + thumb + 3), f"#{i + 1:03d}", fill="black", font=font)
    sheet.save(OUT / "contact_sheet.jpg", "JPEG", quality=90, optimize=True)


def main() -> int:
    for p in (RAW, OUT, LOGS):
        if p.exists():
            shutil.rmtree(p)
    RAW.mkdir(parents=True)
    PHOTOS.mkdir(parents=True)

    instagram = RAW / "instagram_gallerydl"
    facebook = RAW / "facebook_gallerydl"
    instaloader_dir = RAW / "instaloader"

    # Official Instagram profile. Public Instagram posts are platform-moderated and
    # provide the most relevant source material. We request posts only and skip videos.
    run("instagram_gallerydl", [
        sys.executable, "-m", "gallery_dl",
        "--dest", str(instagram),
        "--write-metadata",
        "--filter", "extension in ('jpg', 'jpeg', 'png', 'webp', 'avif')",
        "--range", "1-450",
        "-o", "extractor.instagram.include=posts",
        "-o", "extractor.instagram.max-posts=300",
        "-o", "extractor.instagram.sleep-request=1.5",
        "-o", "extractor.instagram.user-strategy=web,search",
        "https://www.instagram.com/annaclairecloudstv/",
    ])

    # Instaloader fallback often uses a different public endpoint.
    if count_images(instagram) < TARGET:
        run("instagram_instaloader", [
            sys.executable, "-m", "instaloader",
            "--no-videos",
            "--no-video-thumbnails",
            "--no-captions",
            "--no-compress-json",
            "--dirname-pattern", str(instaloader_dir),
            "--filename-pattern", "{date_utc:%Y%m%d}_{shortcode}_{filename}",
            "--count", "300",
            "annaclairecloudstv",
        ])

    # Official public Facebook photo page as a second source if Instagram alone
    # does not yield 200 unique still images.
    if count_images(RAW) < TARGET:
        run("facebook_gallerydl", [
            sys.executable, "-m", "gallery_dl",
            "--dest", str(facebook),
            "--write-metadata",
            "--filter", "extension in ('jpg', 'jpeg', 'png', 'webp', 'avif')",
            "--range", "1-500",
            "-o", "extractor.facebook.include=photos,albums",
            "-o", "extractor.facebook.videos=false",
            "https://www.facebook.com/annaclaireclouds/photos/",
        ])

    candidates = [p for p in RAW.rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"}]
    # Prefer official Instagram output, then fallback sources; newest-first names
    # are retained by the extractors, while path ordering keeps source priority.
    candidates.sort(key=lambda p: (
        0 if "instagram_gallerydl" in {x.lower() for x in p.parts} else
        1 if "instaloader" in {x.lower() for x in p.parts} else 2,
        str(p).lower(),
    ))

    sha_seen: set[str] = set()
    phashes: list[imagehash.ImageHash] = []
    rows: list[dict[str, str]] = []

    for path in candidates:
        if len(rows) >= TARGET:
            break
        try:
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            if sha in sha_seen:
                continue
            with Image.open(path) as im:
                im.load()
                im = ImageOps.exif_transpose(im)
                if min(im.size) < 300:
                    continue
                if max(im.width / im.height, im.height / im.width) > 3.5:
                    continue
                im = im.convert("RGB")
                ph = imagehash.phash(im)
                if any((ph - old) <= 1 for old in phashes):
                    continue
                idx = len(rows) + 1
                filename = f"anna_claire_clouds_{idx:03d}.jpg"
                im.save(PHOTOS / filename, "JPEG", quality=94, optimize=True, progressive=True)
                meta = metadata_for(path)
                rows.append({
                    "index": str(idx),
                    "filename": filename,
                    "source": "instagram" if "instagram" in str(path).lower() else "facebook",
                    "source_url": source_url(meta, path),
                    "original_path": str(path.relative_to(ROOT)),
                    "width": str(im.width),
                    "height": str(im.height),
                    "sha256_original": sha,
                    "date": str(meta.get("date") or meta.get("date_utc") or ""),
                    "post_id": str(meta.get("post_id") or meta.get("shortcode") or meta.get("post_shortcode") or ""),
                })
                sha_seen.add(sha)
                phashes.append(ph)
                print(f"ACCEPT {idx}/{TARGET}: {path.name} {im.width}x{im.height}")
        except (OSError, UnidentifiedImageError, ValueError):
            continue

    fields = ["index", "filename", "source", "source_url", "original_path", "width", "height", "sha256_original", "date", "post_id"]
    with (OUT / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    if rows:
        make_contact_sheet(rows)
    (OUT / "README.txt").write_text(
        "Anna Claire Clouds — official public social-media photo collection\n"
        f"Collected: {len(rows)} / {TARGET}\n\n"
        "Primary source: official Instagram @annaclairecloudstv. Secondary source, only if required: official Facebook photo page.\n"
        "Videos are excluded. Files below 300 px, corrupt images, exact duplicates, and near-exact duplicates are excluded.\n"
        "The source manifest is included. Rights remain with Anna Claire Clouds and the respective photographers/platforms; verify permission before publication or commercial use.\n",
        encoding="utf-8",
    )
    shutil.make_archive("anna_claire_clouds_200_official_public_photos", "zip", OUT)
    (ROOT / "COLLECTION_COUNT.txt").write_text(str(len(rows)), encoding="utf-8")
    print("FINAL_COUNT", len(rows))
    return 0 if len(rows) >= TARGET else 2


if __name__ == "__main__":
    raise SystemExit(main())
