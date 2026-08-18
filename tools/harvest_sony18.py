import argparse, hashlib, html, io, json, math, os, re, shutil, subprocess, time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cv2
import imagehash
import numpy as np
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 Chrome/126 Safari/537.36"})
TIMEOUT = 25
STYLE_TARGET = 24
CHAR_REFS = 4
GROUP_SIZE = 3

FILMS = [
    dict(title="Spider-Man: Into the Spider-Verse", year=2018,
         slugs=["spider-man-spider-verse"],
         characters=["Miles Morales","Gwen Stacy","Peter B. Parker","Spider-Man Noir","Peni Parker","Spider-Ham","Kingpin","Prowler"]),
    dict(title="Spider-Man: Across the Spider-Verse", year=2023,
         slugs=["spider-man-spider-verse-sequel"],
         characters=["Miles Morales","Gwen Stacy","Miguel O'Hara","Hobie Brown","Pavitr Prabhakar","Peter B. Parker","Jessica Drew","The Spot"]),
    dict(title="KPop Demon Hunters", year=2025,
         slugs=["kpop-demon-hunters"],
         characters=["Rumi","Mira","Zoey","Jinu","Celine","Bobby","Gwi-Ma","Saja Boys"]),
    dict(title="The Mitchells vs. the Machines", year=2021,
         slugs=["themitchellsvsthemachines","the-mitchells-vs-the-machines"],
         characters=["Katie Mitchell","Rick Mitchell","Linda Mitchell","Aaron Mitchell","Monchi","PAL","Eric","Deborahbot 5000"]),
    dict(title="Surf's Up", year=2007,
         slugs=["surfs","surfs-up"],
         characters=["Cody Maverick","Big Z","Lani Aliikai","Chicken Joe","Tank Evans","Reggie Belafonte","Mikey Abromowitz"]),
    dict(title="Cloudy with a Chance of Meatballs", year=2009,
         slugs=["cloudy-chance-meatballs"],
         characters=["Flint Lockwood","Sam Sparks","Tim Lockwood","Steve","Baby Brent","Earl Devereaux","Mayor Shelbourne","Manny"]),
    dict(title="Cloudy with a Chance of Meatballs 2", year=2013,
         slugs=["cloudy-chance-meatballs-2"],
         characters=["Flint Lockwood","Sam Sparks","Steve","Chester V","Barb","Earl Devereaux","Manny","Foodimals"]),
    dict(title="Hotel Transylvania", year=2012,
         slugs=["hotel-transylvania"],
         characters=["Dracula","Mavis","Jonathan","Frankenstein","Murray","Wayne","Griffin","Eunice"]),
    dict(title="Hotel Transylvania 2", year=2015,
         slugs=["hotel-transylvania-2"],
         characters=["Dracula","Mavis","Jonathan","Dennis","Vlad","Frankenstein","Murray","Wayne"]),
    dict(title="Hotel Transylvania 3: Summer Vacation", year=2018,
         slugs=["hotel-transylvania-3-summer-vacation","hotel-transylvania-3"],
         characters=["Dracula","Mavis","Jonathan","Ericka","Van Helsing","Dennis","Tinkles","Kraken"]),
    dict(title="Hotel Transylvania: Transformania", year=2022,
         slugs=["hotel-transylvania-transformania","hotel-transylvania-4"],
         characters=["Dracula","Mavis","Johnny","Frankenstein","Murray","Wayne","Griffin","Ericka","Van Helsing"]),
    dict(title="Vivo", year=2021,
         slugs=["vivo"],
         characters=["Vivo","Gabi","Andres","Marta Sandoval","Rosa","Dancarino","Valentina","Lutador"]),
    dict(title="Wish Dragon", year=2021,
         slugs=["wish-dragon"],
         characters=["Din","Long","Lina","Mr. Wang","Pockets","Tall Goon","Short Goon","Din's Mother"]),
    dict(title="GOAT", year=2026,
         slugs=["goat"],
         characters=["Will","Roarball Team","Coach","Rival Players"]),
    dict(title="Smurfs: The Lost Village", year=2017,
         slugs=["smurfs-lost-village","smurfs-the-lost-village"],
         characters=["Smurfette","Brainy","Clumsy","Hefty","Papa Smurf","Gargamel","SmurfStorm","SmurfWillow"]),
    dict(title="The Angry Birds Movie 2", year=2019,
         slugs=["angry-birds-movie-2","the-angry-birds-movie-2"],
         characters=["Red","Chuck","Bomb","Silver","Leonard","Zeta","Mighty Eagle","Courtney"]),
    dict(title="Arthur Christmas", year=2011,
         slugs=["arthur-christmas"],
         characters=["Arthur","Steve","Grandsanta","Santa","Mrs. Santa","Bryony","Gwen","Pedro"]),
    dict(title="The Pirates! Band of Misfits", year=2012,
         slugs=["pirates-band-misfits","the-pirates-band-of-misfits"],
         characters=["Pirate Captain","Pirate with a Scarf","Pirate with Gout","Albino Pirate","Charles Darwin","Queen Victoria","Cutlass Liz","Black Bellamy"]),
]

def slugify(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

def clean_text(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def request(url, binary=False):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            return r.content if binary else r.text
        except Exception as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise last

def run(cmd, timeout=420):
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

def candidate_pages(film):
    out = []
    for slug in film["slugs"]:
        for base in [
            "https://www.sonypicturesanimation.com/projects/films/",
            "https://www.sonypicturesanimation.com/index.php/projects/films/",
            "https://stg.sonypicturesanimation.com/projects/films/",
            "https://stg2.sonypicturesanimation.com/projects/films/",
        ]:
            out.append(base + slug)
    return out

def resolve_page(film, log):
    title_tokens = [t for t in clean_text(film["title"]).split() if len(t) > 2 and t not in {"the","with","and","of"}]
    best = None
    for url in candidate_pages(film):
        try:
            text = request(url)
            soup = BeautifulSoup(text, "html.parser")
            page_text = clean_text((soup.title.get_text(" ", strip=True) if soup.title else "") + " " + soup.get_text(" ", strip=True)[:4000])
            score = sum(tok in page_text for tok in title_tokens)
            if score >= max(1, min(3, len(title_tokens))):
                log.append(f"official_page={url} score={score}")
                return url, text
            if best is None or score > best[0]:
                best = (score, url, text)
        except Exception as exc:
            log.append(f"page_fail={url} {type(exc).__name__}")
    links = []
    for page_no in range(0, 6):
        for url in [
            f"https://www.sonypicturesanimation.com/projects/films?page={page_no}",
            f"https://www.sonypicturesanimation.com/index.php/projects/films?page={page_no}",
        ]:
            try:
                text = request(url)
                soup = BeautifulSoup(text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = urljoin(url, a["href"])
                    if "/projects/films/" in href:
                        label = clean_text(a.get_text(" ", strip=True) + " " + a.get("aria-label","") + " " + a.get("title",""))
                        score = sum(tok in (label + " " + clean_text(href)) for tok in title_tokens)
                        if score:
                            links.append((score, href))
            except Exception:
                pass
    for _, href in sorted(set(links), reverse=True):
        try:
            text = request(href)
            page_text = clean_text(BeautifulSoup(text, "html.parser").get_text(" ", strip=True)[:5000])
            score = sum(tok in page_text for tok in title_tokens)
            if score >= max(1, min(3, len(title_tokens))):
                log.append(f"official_page_discovered={href} score={score}")
                return href, text
        except Exception:
            pass
    if best and best[0] > 0:
        log.append(f"official_page_weak_match={best[1]} score={best[0]}")
        return best[1], best[2]
    raise RuntimeError("No official Sony Pictures Animation page resolved")

def extract_image_urls(page_url, text):
    soup = BeautifulSoup(text, "html.parser")
    found = {}
    attrs = ["src","data-src","data-image","data-lazy-src","data-original","poster","href"]
    for tag in soup.find_all(["img","source","a","video","meta"]):
        alt = " ".join(filter(None, [
            tag.get("alt",""), tag.get("title",""), tag.get("aria-label",""),
            tag.get("content","") if tag.name == "meta" else ""
        ]))
        parent = tag.find_parent(["figure","li","section","article","div"])
        context = parent.get_text(" ", strip=True)[:700] if parent else ""
        values = [tag.get(a) for a in attrs if tag.get(a)]
        for a in ["srcset","data-srcset"]:
            if tag.get(a):
                values += [piece.strip().split()[0] for piece in tag.get(a).split(",")]
        style = tag.get("style","")
        values += re.findall(r"url\(['\"]?([^'\")]+)", style)
        for raw in values:
            u = html.unescape(str(raw)).replace("\\/","/")
            u = urljoin(page_url, u)
            lo = u.lower()
            if any(ext in lo for ext in [".jpg",".jpeg",".png",".webp","images.squarespace-cdn.com","imageworks.com"]):
                found[u] = dict(url=u, alt=alt, context=context, source_type="official_site")
    for raw in re.findall(r'https?:\\?/\\?/[^"\'<> ]+?(?:\.jpe?g|\.png|\.webp)(?:\?[^"\'<> ]*)?', text, re.I):
        u = html.unescape(raw).replace("\\/","/")
        found.setdefault(u, dict(url=u, alt="", context="embedded official page asset", source_type="official_site"))
    return list(found.values())

def extract_video_urls(page_url, text):
    soup = BeautifulSoup(text, "html.parser")
    out = []
    for tag in soup.find_all(["iframe","video","source","a"]):
        for attr in ["src","href","data-src"]:
            raw = tag.get(attr)
            if raw:
                u = html.unescape(raw).replace("\\/","/")
                u = urljoin(page_url, u)
                if any(k in u.lower() for k in ["vimeo.com","youtube.com","youtu.be",".mp4",".m3u8"]):
                    out.append(u)
    for vid in re.findall(r'(?:player\.)?vimeo\.com/(?:video/)?(\d{6,12})', text, re.I):
        out.append(f"https://vimeo.com/{vid}")
    for raw in re.findall(r'https?:\\?/\\?/[^"\'<> ]+?(?:\.mp4|\.m3u8)(?:\?[^"\'<> ]*)?', text, re.I):
        out.append(html.unescape(raw).replace("\\/","/"))
    out.append(page_url)
    deduped = []
    for u in out:
        if u not in deduped:
            deduped.append(u)
    return deduped[:10]

def normalize_image(data, path):
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        if min(im.size) < 320 or max(im.size) < 700:
            return None
        if max(im.size) > 1600:
            ratio = 1600 / max(im.size)
            im = im.resize((round(im.width * ratio), round(im.height * ratio)), Image.Resampling.LANCZOS)
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path, "JPEG", quality=89, optimize=True, progressive=True)
        return image_metadata(path)
    except Exception:
        return None

def image_metadata(path):
    try:
        im = Image.open(path).convert("RGB")
        arr = np.array(im)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        sharp = float(np.log1p(cv2.Laplacian(gray, cv2.CV_64F).var()))
        contrast = float(gray.std() / 20.0)
        sat = float(hsv[:,:,1].mean() / 60.0)
        brightness = float(gray.mean())
        quality = sharp + contrast + min(2.0, sat)
        black_top = float((gray[:max(1,gray.shape[0]//12)] < 12).mean())
        black_bottom = float((gray[-max(1,gray.shape[0]//12):] < 12).mean())
        return {
            "path": str(path), "width": im.width, "height": im.height,
            "phash": str(imagehash.phash(im)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "quality": quality, "brightness": brightness,
            "landscape": im.width / max(1, im.height) >= 1.15,
            "black_bar_score": max(black_top, black_bottom)
        }
    except Exception:
        return None

def download_site_pool(page_url, text, film_dir, log):
    pool = []
    seen = set()
    bad_words = [
        "logo","favicon","sprite","icon","rating","facebook","twitter","instagram","tiktok",
        "amazon","bluray","blu-ray","dvd","disc","poster","keyart","key-art","title-treatment",
        "award","share-button","netflix-button"
    ]
    candidates = extract_image_urls(page_url, text)
    log.append(f"official_image_candidates={len(candidates)}")
    idx = 0
    for item in candidates:
        u = item["url"]
        if u in seen:
            continue
        seen.add(u)
        hay = clean_text(u + " " + item.get("alt","") + " " + item.get("context",""))
        if any(word in hay for word in bad_words):
            continue
        if "images.squarespace-cdn.com" in u and "format=" not in u:
            u += ("&" if "?" in u else "?") + "format=1600w"
        try:
            data = request(u, binary=True)
            if len(data) < 18000:
                continue
            dest = film_dir / "_SITE_POOL" / f"site_{idx:03d}.jpg"
            meta = normalize_image(data, dest)
            if not meta:
                continue
            if meta["brightness"] < 8 or meta["brightness"] > 247:
                dest.unlink(missing_ok=True)
                continue
            meta.update(url=u, alt=item.get("alt",""), context=item.get("context",""), source_type="official_site")
            pool.append(meta)
            idx += 1
            if idx >= 100:
                break
        except Exception:
            continue
    log.append(f"official_site_downloaded={len(pool)}")
    return pool

def download_video(url, out_dir, stem, log):
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{stem}.%(ext)s")
    cmd = [
        "yt-dlp","--no-playlist","--no-warnings",
        "-f","bv*[height<=720]+ba/b[height<=720]",
        "--merge-output-format","mp4","--max-filesize","300M",
        "-o",template,url
    ]
    proc = run(cmd, timeout=480)
    for p in out_dir.glob(f"{stem}.*"):
        if p.suffix.lower() in {".mp4",".webm",".mkv",".mov"} and p.stat().st_size > 200000:
            log.append(f"official_video_downloaded={url}")
            return p
    log.append(f"video_download_failed={url} {proc.stderr[-180:]}")
    return None

def search_official_video(title, log):
    query = f"ytsearch10:Sony Pictures Animation {title} official trailer"
    proc = run(["yt-dlp","--flat-playlist","--dump-json","--no-warnings",query], timeout=180)
    for line in proc.stdout.splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        uploader = " ".join(str(row.get(k,"")) for k in ["uploader","channel","channel_id"]).lower()
        video_title = str(row.get("title","")).lower()
        official = any(x in uploader for x in ["sony pictures animation","sony pictures entertainment","sonyanimation","netflix","aardman"])
        useful = any(x in video_title for x in ["trailer","teaser","clip","official"])
        if official and useful:
            url = row.get("webpage_url") or ("https://www.youtube.com/watch?v=" + row.get("id",""))
            log.append(f"official_video_search_match={url} uploader={uploader[:80]}")
            return url
    return None

def extract_video_frames(video, source_url, film_dir, prefix, log):
    try:
        duration_text = run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(video)], timeout=30).stdout.strip()
        duration = float(duration_text)
    except Exception:
        duration = 120.0
    frames = []
    times = np.linspace(max(0.8, duration * 0.06), max(1.0, duration * 0.94), 84)
    for i, ts in enumerate(times):
        dest = film_dir / "_VIDEO_POOL" / f"{prefix}_{i:03d}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        run([
            "ffmpeg","-loglevel","error","-ss",f"{ts:.3f}","-i",str(video),
            "-frames:v","1","-vf","scale=min(1600\\,iw):-2","-q:v","2",str(dest)
        ], timeout=40)
        if not dest.exists():
            continue
        meta = image_metadata(dest)
        if not meta:
            dest.unlink(missing_ok=True)
            continue
        if meta["brightness"] < 12 or meta["brightness"] > 244 or meta["black_bar_score"] > 0.92 or meta["quality"] < 4.0:
            dest.unlink(missing_ok=True)
            continue
        meta.update(url=source_url, alt=f"official video frame {ts:.3f}s", context="official Sony/partner video", source_type="official_video", timestamp_seconds=round(float(ts),3))
        frames.append(meta)
    log.append(f"video_frames={len(frames)} source={source_url}")
    return frames

def video_pool(film, page_url, text, film_dir, log, needed=True):
    if not needed:
        return []
    urls = extract_video_urls(page_url, text)
    fallback = search_official_video(film["title"], log)
    if fallback:
        urls.append(fallback)
    frames = []
    work = film_dir / "_VIDEO_DOWNLOADS"
    downloaded = 0
    for idx, url in enumerate(urls):
        if downloaded >= 2:
            break
        try:
            vid = download_video(url, work, f"video_{idx}", log)
            if not vid:
                continue
            frames.extend(extract_video_frames(vid, url, film_dir, f"v{idx}", log))
            downloaded += 1
        except Exception as exc:
            log.append(f"video_error={url} {type(exc).__name__}")
    shutil.rmtree(work, ignore_errors=True)
    return frames

def dedupe(items, threshold=7, cap=220):
    out, hashes = [], []
    for item in sorted(items, key=lambda x: x["quality"], reverse=True):
        try:
            h = imagehash.hex_to_hash(item["phash"])
        except Exception:
            continue
        if any(h - prev < threshold for prev in hashes):
            continue
        out.append(item)
        hashes.append(h)
        if len(out) >= cap:
            break
    return out

def feature(path):
    im = cv2.imread(path)
    if im is None:
        return np.zeros(96, dtype=np.float32)
    im = cv2.resize(im, (240, 135))
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv],[0,1],None,[16,6],[0,180,0,256]).flatten()
    hist /= hist.sum() + 1e-7
    return hist / (np.linalg.norm(hist) + 1e-7)

def diverse(items, count, seed=None):
    if len(items) <= count:
        return list(items)
    feats = np.stack([feature(x["path"]) for x in items])
    qual = np.array([x["quality"] for x in items], dtype=float)
    qual = (qual - qual.min()) / (np.ptp(qual) + 1e-7)
    selected = [int(np.argmax(qual))] if seed is None else [seed]
    min_dist = np.ones(len(items)) * 9
    while len(selected) < count:
        min_dist = np.minimum(min_dist, 1 - np.clip(feats @ feats[selected[-1]], -1, 1))
        score = min_dist + 0.14 * qual
        score[selected] = -9
        selected.append(int(np.argmax(score)))
    return [items[i] for i in selected]

def select_style(pool):
    landscape = [x for x in pool if x.get("landscape")]
    site = [x for x in landscape if x["source_type"] == "official_site"]
    video = [x for x in landscape if x["source_type"] == "official_video"]
    selected = []
    if site:
        selected += diverse(site, min(10, len(site)))
    remaining = [x for x in video if x not in selected]
    if remaining and len(selected) < STYLE_TARGET:
        selected += diverse(remaining, min(STYLE_TARGET - len(selected), len(remaining)))
    all_remaining = [x for x in landscape if x not in selected]
    if len(selected) < STYLE_TARGET and all_remaining:
        selected += diverse(all_remaining, min(STYLE_TARGET - len(selected), len(all_remaining)))
    if len(selected) < STYLE_TARGET:
        extra = [x for x in pool if x not in selected]
        selected += diverse(extra, min(STYLE_TARGET - len(selected), len(extra)))
    return selected[:STYLE_TARGET]

STOP = {"the","and","with","movie","band","team","queen","king","mr","mrs"}
def tokens(name):
    return [x for x in re.findall(r"[A-Za-z0-9'-]+", name.lower()) if len(x) > 2 and x not in STOP]

def pick_character(pool, name):
    tok = tokens(name)
    matched = []
    for item in pool:
        hay = clean_text(item.get("alt","") + " " + item.get("context","") + " " + Path(urlparse(item.get("url","")).path).name)
        hits = sum(t in hay for t in tok)
        if hits:
            matched.append((hits, item["quality"], item))
    matched.sort(key=lambda x:(x[0],x[1]), reverse=True)
    refs = [x[2] for x in matched[:CHAR_REFS]]
    method = "official_metadata_match"
    if len(refs) < CHAR_REFS:
        candidates = [x for x in pool if x not in refs]
        refs += diverse(candidates, min(CHAR_REFS - len(refs), len(candidates)))
        method = "official_source_candidates_require_identity_review"
    return refs[:CHAR_REFS], method

def contact_sheet(items, dest, cols=4, thumb=(360, 205)):
    if not items:
        return
    w, h = thumb
    margin, label_h = 12, 34
    rows = math.ceil(len(items) / cols)
    canvas = Image.new("RGB", (margin + cols*(w+margin), margin + rows*(h+label_h+margin)), (24,24,24))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    for i, (path, label) in enumerate(items):
        im = Image.open(path).convert("RGB")
        im.thumbnail((w,h))
        tile = Image.new("RGB",(w,h),(5,5,5))
        tile.paste(im,((w-im.width)//2,(h-im.height)//2))
        x = margin + (i % cols)*(w+margin)
        y = margin + (i // cols)*(h+label_h+margin)
        canvas.paste(tile,(x,y))
        draw.text((x,y+h+5),label[:48],font=font,fill="white")
    canvas.save(dest,"JPEG",quality=88,optimize=True)

def compact_source(item):
    return {k:item.get(k) for k in ["url","source_type","timestamp_seconds","alt","context","width","height","sha256"] if item.get(k) not in [None,""]}

def process(film, root):
    slug = slugify(film["title"])
    film_dir = root / f'{film["year"]}_{slug}'
    film_dir.mkdir(parents=True, exist_ok=True)
    log = []
    page_url, page_text = resolve_page(film, log)
    site = download_site_pool(page_url, page_text, film_dir, log)
    video = video_pool(film, page_url, page_text, film_dir, log, needed=True)
    pool = dedupe(site + video)
    style = select_style(pool)
    style_manifest = []
    style_items = []
    for idx, item in enumerate(style, 1):
        ext = "site" if item["source_type"] == "official_site" else "video"
        dest = film_dir / "STYLE_PACK_24" / f"{idx:02d}_{ext}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item["path"], dest)
        style_manifest.append({"index":idx,"file":str(dest.relative_to(film_dir)),"source":compact_source(item)})
        style_items.append((dest, f"{idx:02d} {ext}"))
    chars_manifest = {}
    char_sheet_items = []
    for name in film["characters"]:
        refs, method = pick_character(pool, name)
        rows = []
        char_dir = film_dir / "CHARACTER_PACK" / re.sub(r"[^A-Za-z0-9_-]+","_",name).strip("_")
        char_dir.mkdir(parents=True, exist_ok=True)
        for idx, item in enumerate(refs, 1):
            dest = char_dir / f"ref_{idx:02d}.jpg"
            shutil.copy2(item["path"], dest)
            rows.append({"file":str(dest.relative_to(film_dir)),"source":compact_source(item)})
            char_sheet_items.append((dest, f"{name} {idx}"))
        chars_manifest[name] = {
            "method":method,
            "human_identity_review":method != "official_metadata_match",
            "refs":rows
        }
    contact_sheet(style_items, film_dir / "STYLE_CONTACT_SHEET.jpg", cols=4)
    contact_sheet(char_sheet_items, film_dir / "CHARACTER_CONTACT_SHEET.jpg", cols=4)
    manifest = {
        "title":film["title"],"year":film["year"],"studio":"Sony Pictures Animation",
        "official_page":page_url,
        "counts":{
            "official_site_candidates":len(site),
            "official_video_candidates":len(video),
            "unique_candidates":len(pool),
            "style_refs":len(style),
            "characters":len(chars_manifest),
            "character_refs":sum(len(v["refs"]) for v in chars_manifest.values())
        },
        "style_target":STYLE_TARGET,
        "style_complete":len(style) >= STYLE_TARGET,
        "style":style_manifest,
        "characters":chars_manifest,
        "qa":{
            "official_sources_only":True,
            "perceptual_deduplication":True,
            "minimum_style_refs_requested":16,
            "automated_visual_diversity_selection":True,
            "character_identity_review_required_when_flagged":True
        },
        "log":log
    }
    (film_dir / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (film_dir / "LOG.txt").write_text("\n".join(log), encoding="utf-8")
    shutil.rmtree(film_dir / "_SITE_POOL", ignore_errors=True)
    shutil.rmtree(film_dir / "_VIDEO_POOL", ignore_errors=True)
    status = "complete" if manifest["style_complete"] else ("usable" if len(style) >= 16 else "partial")
    return {"title":film["title"],"status":status,"counts":manifest["counts"],"official_page":page_url}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--output", default="output")
    args = parser.parse_args()
    start = args.group * GROUP_SIZE
    chosen = FILMS[start:start+GROUP_SIZE]
    root = Path(args.output) / f"sony18_volume_{args.group+1}"
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for film in chosen:
        try:
            print("PROCESS", film["title"], flush=True)
            results.append(process(film, root))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            results.append({"title":film["title"],"status":"failed","error":str(exc)})
    summary = {"group":args.group+1,"style_target_per_film":STYLE_TARGET,"results":results}
    (root / "GROUP_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    bad = [r for r in results if r.get("status") not in {"complete","usable"}]
    if bad:
        raise SystemExit(2)

if __name__ == "__main__":
    main()
