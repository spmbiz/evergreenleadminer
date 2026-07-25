from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import imagehash
import requests
from PIL import Image, ImageDraw, ImageFont, ImageStat

MANIFEST = Path("tools/gta_v_real_100_manifest.csv")
ROOT = Path("GTA_V_REAL_100")
IMAGES = ROOT / "images"
MAX_WIDTH = 1920
WORKERS = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.igta5.com/official-screenshots",
}


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return value[:72] or "gta_v_gameplay"


def candidates(url: str) -> list[str]:
    without_scheme = re.sub(r"^https?://", "", url)
    return [
        url,
        f"https://wsrv.nl/?url={quote(without_scheme, safe='/')}&w={MAX_WIDTH}&output=jpg&q=92",
        f"https://images.weserv.nl/?url={quote(without_scheme, safe='/')}&w={MAX_WIDTH}&output=jpg&q=92",
    ]


def download_one(row: dict[str, str]) -> dict:
    index = int(row["index"])
    title = row["title"].strip()
    source = row["direct_image_url"].strip()
    session = requests.Session()
    error_messages: list[str] = []
    for attempt_url in candidates(source):
        try:
            response = session.get(attempt_url, headers=HEADERS, timeout=90, allow_redirects=True)
            content_type = (response.headers.get("content-type") or "").lower()
            if response.status_code != 200 or "image" not in content_type or len(response.content) < 20_000:
                error_messages.append(f"{response.status_code}:{content_type}:{len(response.content)}:{attempt_url}")
                continue
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            width, height = image.size
            ratio = width / max(height, 1)
            if width < 900 or height < 500 or not 1.30 <= ratio <= 2.40:
                error_messages.append(f"invalid_dimensions:{width}x{height}:{attempt_url}")
                continue
            original_width, original_height = width, height
            if width > MAX_WIDTH:
                new_height = round(height * MAX_WIDTH / width)
                image = image.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
            filename = f"{index:03d}_{slugify(title)}.jpg"
            output = IMAGES / filename
            image.save(output, "JPEG", quality=94, optimize=True, progressive=True)
            final_bytes = output.read_bytes()
            thumb = image.copy()
            thumb.thumbnail((320, 180), Image.Resampling.LANCZOS)
            stats = ImageStat.Stat(thumb.convert("L"))
            return {
                "ok": True,
                "index": index,
                "title": title,
                "filename": filename,
                "source_gallery": row["source_gallery"],
                "source_image": source,
                "downloaded_via": attempt_url,
                "source_width": original_width,
                "source_height": original_height,
                "output_width": image.width,
                "output_height": image.height,
                "file_size": len(final_bytes),
                "sha256": hashlib.sha256(final_bytes).hexdigest(),
                "phash": str(imagehash.phash(image, hash_size=16)),
                "brightness": round(float(stats.mean[0]), 2),
                "contrast": round(float(stats.stddev[0]), 2),
            }
        except Exception as exc:
            error_messages.append(f"{type(exc).__name__}:{exc}:{attempt_url}")
    return {
        "ok": False,
        "index": index,
        "title": title,
        "source_image": source,
        "errors": " | ".join(error_messages),
    }


def font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def make_contact_sheet(rows: list[dict]) -> None:
    cols = 5
    cell_width = 360
    image_height = 202
    label_height = 55
    header = 80
    row_count = math.ceil(len(rows) / cols)
    canvas = Image.new("RGB", (cols * cell_width, header + row_count * (image_height + label_height)), "#0b0b0b")
    draw = ImageDraw.Draw(canvas)
    draw.text((18, 16), "GTA V — 100 real, individually sourced gameplay screenshots", fill="white", font=font(25))
    draw.text((18, 49), "Official screenshots · no video/GIF extraction · exact-file and perceptual QA", fill="#bdbdbd", font=font(14))
    for position, row in enumerate(rows):
        rr, cc = divmod(position, cols)
        x = cc * cell_width
        y = header + rr * (image_height + label_height)
        image = Image.open(IMAGES / row["filename"]).convert("RGB")
        image.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
        frame = Image.new("RGB", (cell_width, image_height), "black")
        frame.paste(image, ((cell_width - image.width) // 2, (image_height - image.height) // 2))
        canvas.paste(frame, (x, y))
        draw.rectangle((x, y + image_height, x + cell_width, y + image_height + label_height), fill="#191919")
        title = row["title"] if len(row["title"]) <= 42 else row["title"][:39] + "..."
        draw.text((x + 7, y + image_height + 7), f"{row['index']:03d} · {title}", fill="white", font=font(12))
        draw.text((x + 7, y + image_height + 29), f"{row['output_width']}×{row['output_height']}", fill="#aaaaaa", font=font(11))
    canvas.save(ROOT / "CONTACT_SHEET_100.jpg", "JPEG", quality=91, optimize=True)


def similarity_report(rows: list[dict]) -> list[dict]:
    hashes = [(row, imagehash.hex_to_hash(row["phash"])) for row in rows]
    pairs: list[dict] = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            distance = hashes[i][1] - hashes[j][1]
            if distance <= 28:
                a, b = hashes[i][0], hashes[j][0]
                pairs.append({
                    "index_a": a["index"],
                    "title_a": a["title"],
                    "index_b": b["index"],
                    "title_b": b["title"],
                    "phash_distance": distance,
                })
    pairs.sort(key=lambda item: item["phash_distance"])
    return pairs


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    IMAGES.mkdir(parents=True)
    with MANIFEST.open(encoding="utf-8-sig") as handle:
        manifest = list(csv.DictReader(handle))
    if len(manifest) != 100:
        raise SystemExit(f"Manifest must contain exactly 100 rows, found {len(manifest)}")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(download_one, row): row for row in manifest}
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            status = "OK" if result["ok"] else "FAILED"
            print(f"[{completed:03d}/100] {status} #{result['index']:03d} {result['title']}", flush=True)

    successes = sorted([row for row in results if row["ok"]], key=lambda row: row["index"])
    failures = sorted([row for row in results if not row["ok"]], key=lambda row: row["index"])
    sha_count = len({row["sha256"] for row in successes})
    pairs = similarity_report(successes)
    exact_duplicates = len(successes) - sha_count
    near_duplicates = [pair for pair in pairs if pair["phash_distance"] <= 12]

    write_csv(ROOT / "sources.csv", successes)
    write_csv(ROOT / "failures.csv", failures)
    write_csv(ROOT / "similarity_report.csv", pairs)
    (ROOT / "sources.json").write_text(json.dumps(successes, ensure_ascii=False, indent=2), encoding="utf-8")
    if successes:
        make_contact_sheet(successes)

    result = {
        "requested": 100,
        "downloaded": len(successes),
        "failed": len(failures),
        "unique_sha256": sha_count,
        "exact_duplicate_files": exact_duplicates,
        "near_duplicate_pairs_phash_le_12": len(near_duplicates),
        "minimum_phash_distance": pairs[0]["phash_distance"] if pairs else None,
        "status": "complete" if len(successes) == 100 and exact_duplicates == 0 else "partial",
    }
    (ROOT / "QA_RESULT.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (ROOT / "README.txt").write_text(
        "GTA V — 100 REAL GAMEPLAY SCREENSHOTS\n\n"
        "Every file is an individually named official GTA V screenshot from the iGTA5 archive.\n"
        "No image was extracted from a video or GIF. No FiveM menu captures are included.\n"
        "Images were downloaded independently, decoded with Pillow, normalized to at most 1920 px wide,\n"
        "hashed with SHA-256, and compared using 256-bit perceptual hashes.\n\n"
        "See sources.csv for every original URL and similarity_report.csv for the visual QA.\n",
        encoding="utf-8",
    )

    with zipfile.ZipFile("GTA_V_REAL_100_SCREENSHOTS.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(ROOT.parent))

    print(json.dumps(result, indent=2), flush=True)
    if result["status"] != "complete":
        raise SystemExit("Curated pack did not pass exact-count/integrity QA")


if __name__ == "__main__":
    main()
