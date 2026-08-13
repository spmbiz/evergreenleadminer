#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

RELEASE = "2026-06-17.0"
OVERTURE = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*"
OUT_DIR = Path("results/gws_brussels_resolver_v2")

COMMUNES = [
    ("Uccle / Ukkel", "uccle", "1180", 0),
    ("Ixelles / Elsene", "ixelles", "1050", 1),
    ("Watermael-Boitsfort / Watermaal-Bosvoorde", "watermael-boitsfort", "1170", 2),
    ("Auderghem / Oudergem", "auderghem", "1160", 3),
    ("Forest / Vorst", "forest", "1190", 4),
    ("Saint-Gilles / Sint-Gillis", "saint-gilles", "1060", 5),
    ("Etterbeek", "etterbeek", "1040", 6),
]

SOUTH_SOURCES = []
for commune, slug, postal, priority in COMMUNES:
    SOUTH_SOURCES.append((commune, f"https://coiffeurbelgique.com/c/{slug}-21/", postal, priority, "hairdresser"))
    SOUTH_SOURCES.append((commune, f"https://barbierbelgique.com/c/{slug}-21/", postal, priority, "barber"))

CHAIN_WORDS = (
    "jean louis david", "dessange", "toni guy", "camille albane", "olivier dachkin",
    "hairdis", "kruidvat", "ici paris", "basic fit", "orange", "proximus", "base shop",
)

PLATFORM_OR_DIRECTORY_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "youtube.com",
    "treatwell.be", "mytreatwell.be", "planity.com", "fresha.com", "salonkee.",
    "pagesdor.be", "goudengids.be", "bizique.be", "cylex-belgie.be", "opendi.be",
    "selfcity.be", "heures.be", "openingsuren.", "waze.com", "google.com", "google.be",
    "business.site", "maps.apple.", "tripadvisor.", "yelp.", "bottin.be",
    "coiffeurbelgique.com", "barbierbelgique.com", "topcoiffeur.be", "ucclecity.be",
    "ixelles.city", "etterbeek.city", "companyweb.be", "foursquare.", "cybo.com",
    "openinghours.", "hours.be", "yellowpages.", "tupalo.", "infobel.",
    "duckduckgo.com", "appbarber.com.br", "booksy.", "linktr.ee", "wa.me",
)

USER_AGENT = "Mozilla/5.0 (compatible; GWS-South-Brussels-Harvester/1.0)"
DDG = "https://html.duckduckgo.com/html/?"

def txt(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()

def norm(s):
    s = unicodedata.normalize("NFKD", txt(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def tokens(s):
    stop = {"the", "de", "la", "le", "les", "du", "des", "and", "et", "sa", "sprl", "srl", "bv", "nv", "coiffure", "salon", "hair", "barber", "barbershop"}
    return {x for x in norm(s).split() if len(x) > 1 and x not in stop}

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / max(1, len(ta | tb))
    contains = 1.0 if (a in b or b in a) and min(len(a), len(b)) >= 4 else 0.0
    return max(seq, 0.65 * seq + 0.35 * jac, 0.72 * contains + 0.28 * jac)

def host(u):
    try:
        h = (urlparse(u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def is_platform_or_directory(h):
    h = h.lower()
    return any(x in h for x in PLATFORM_OR_DIRECTORY_DOMAINS)

def first(v):
    if isinstance(v, (list, tuple)):
        return txt(v[0]) if v else ""
    return txt(v)

def brand_name(v):
    if not isinstance(v, dict):
        return ""
    n = v.get("names")
    if isinstance(n, dict):
        return txt(n.get("primary"))
    return txt(v.get("name"))

def owned_site(websites):
    if not websites:
        return ""
    vals = websites if isinstance(websites, (list, tuple)) else [websites]
    for u in vals:
        u = txt(u)
        h = host(u)
        if h and not is_platform_or_directory(h):
            return u
    return ""

def fetch_url(url, timeout=35):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.7"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

class DirectoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries = []
        self.current = None
        self.in_h2 = False
        self.href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h2":
            if self.current:
                self._finish()
            self.current = {"name": "", "text": [], "links": []}
            self.in_h2 = True
        if tag == "a" and self.current is not None:
            self.href = txt(attrs.get("href"))
            if self.href:
                self.current["links"].append(self.href)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.in_h2 = False
        if tag == "a":
            self.href = ""

    def handle_data(self, data):
        s = txt(data)
        if not s or self.current is None:
            return
        if self.in_h2:
            self.current["name"] = txt(self.current["name"] + " " + s)
        self.current["text"].append(s)

    def close(self):
        super().close()
        if self.current:
            self._finish()

    def _finish(self):
        if self.current and self.current["name"]:
            self.current["body"] = " ".join(self.current["text"])
            self.entries.append(self.current)
        self.current = None

def extract_postal(body):
    m = re.search(r"\b(10[0-9]{2}|11[0-9]{2}|12[0-9]{2})\b", body)
    return m.group(1) if m else ""

def extract_address(body):
    m = re.search(r"Adresse\s*:\s*(.*?)(?=Téléphone\s*:|Site Internet\s*:|$)", body, re.I)
    return txt(m.group(1)) if m else ""

def directory_signal(entry):
    body = entry["body"]
    low = body.lower()
    links = []
    for u in entry["links"]:
        if u.startswith(("tel:", "mailto:", "#", "javascript:")):
            continue
        links.append(u)
    explicit_no = "pas de site web" in low or "aucun site web" in low
    dead_business_site = ".business.site" in low or any("business.site" in u for u in links)
    platform_links = [u for u in links if is_platform_or_directory(host(u))]
    owned_links = [u for u in links if host(u) and not is_platform_or_directory(host(u))]
    social_only = bool(platform_links) and not owned_links and any(
        x in (low + " " + " ".join(platform_links).lower())
        for x in ("facebook", "instagram", "treatwell", "planity", "fresha", "salonkee")
    )
    if owned_links and not explicit_no:
        return "", owned_links
    if explicit_no:
        return "EXPLICIT_NO_WEBSITE", owned_links
    if dead_business_site:
        return "DEAD_GOOGLE_BUSINESS_SITE", owned_links
    if social_only:
        return "PLATFORM_ONLY", owned_links
    return "", owned_links

def fetch_negative_reservoir():
    rows = []
    source_stats = {}
    for commune, url, expected_postal, priority, source_family in SOUTH_SOURCES:
        stat_key = f"{commune}|{source_family}"
        try:
            html = fetch_url(url)
            p = DirectoryParser()
            p.feed(html)
            p.close()
            accepted = 0
            in_commune = 0
            for e in p.entries:
                name = txt(e["name"])
                if not name or any(c in norm(name) for c in CHAIN_WORDS):
                    continue
                body = e["body"]
                postal = extract_postal(body)
                if postal != expected_postal:
                    continue
                in_commune += 1
                signal, owned_links = directory_signal(e)
                if not signal:
                    continue
                rows.append({
                    "commune": commune,
                    "commune_priority": priority,
                    "source_family": source_family,
                    "directory_name": name,
                    "directory_address": extract_address(body),
                    "postalcode": postal,
                    "negative_signal": signal,
                    "source_url": url,
                    "directory_owned_links": json.dumps(owned_links, ensure_ascii=False),
                })
                accepted += 1
            source_stats[stat_key] = {
                "url": url,
                "entries": len(p.entries),
                "in_commune_entries": in_commune,
                "negative": accepted,
                "error": "",
            }
        except Exception as exc:
            source_stats[stat_key] = {
                "url": url,
                "entries": 0,
                "in_commune_entries": 0,
                "negative": 0,
                "error": f"{type(exc).__name__}:{exc}",
            }
    out = []
    seen = set()
    for r in sorted(rows, key=lambda x: (x["commune_priority"], norm(x["directory_name"]))):
        key = (norm(r["directory_name"]), r["commune"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, source_stats

def load_overture():
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    q = f"""
    SELECT id,names.primary AS name,basic_category,categories.primary AS category_primary,
           websites,socials,brand,addresses,confidence,operating_status,
           (bbox.xmin+bbox.xmax)/2.0 AS longitude,(bbox.ymin+bbox.ymax)/2.0 AS latitude
    FROM read_parquet('{OVERTURE}', hive_partitioning=1)
    WHERE bbox.xmax >= 4.22 AND bbox.xmin <= 4.48
      AND bbox.ymax >= 50.73 AND bbox.ymin <= 50.90
      AND (operating_status IS NULL OR operating_status='open')
      AND names.primary IS NOT NULL
    """
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, x)) for x in cur.fetchall()]

def place_address_text(p):
    return norm(json.dumps(p.get("addresses"), ensure_ascii=False, default=str))

def build_place_index(places):
    idx = defaultdict(list)
    for i, p in enumerate(places):
        for t in tokens(p.get("name")):
            idx[t].append(i)
    return idx

def resolve_candidate(c, idx, places):
    cand_ids = set()
    for t in tokens(c["directory_name"]):
        cand_ids.update(idx.get(t, []))
    if not cand_ids:
        postal = c["postalcode"]
        cand_ids = {i for i, p in enumerate(places) if postal and postal in place_address_text(p)}
    best = None
    caddr = tokens(c.get("directory_address"))
    for i in cand_ids:
        p = places[i]
        ns = sim(c["directory_name"], p.get("name"))
        pa = place_address_text(p)
        overlap = len(caddr & tokens(pa)) / max(1, len(caddr)) if caddr else 0.0
        postal_match = bool(c["postalcode"] and c["postalcode"] in pa)
        score = ns * 100 + overlap * 24 + (10 if postal_match else 0) + float(p.get("confidence") or 0) * 8
        if best is None or score > best[0]:
            best = (score, ns, overlap, postal_match, p)
    if best is None:
        return None, 0.0, 0.0, False
    _, ns, overlap, postal_match, p = best
    resolved = (
        (ns >= 0.86 and postal_match)
        or (ns >= 0.76 and postal_match and overlap >= 0.18)
        or (ns >= 0.92 and overlap >= 0.10)
    )
    return p, ns, overlap, resolved

def ddg_result_links(html):
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        u = m.group(1)
        if "uddg=" in u:
            try:
                q = parse_qs(urlparse(u).query)
                u = unquote(first(q.get("uddg")))
            except Exception:
                pass
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            out.append(u)
    return list(dict.fromkeys(out))

def suspicious_domain(name, u):
    h = host(u)
    if not h or is_platform_or_directory(h):
        return False
    stem = norm(h.split(".")[0]).replace(" ", "")
    ntoks = [x for x in tokens(name) if len(x) >= 3]
    if not ntoks:
        return False
    compact = norm(name).replace(" ", "")
    return sim(stem, compact) >= 0.60 or any(t in stem for t in ntoks if len(t) >= 4)

def web_challenge(c):
    queries = [
        f'"{c["directory_name"]}" "{c["postalcode"]}" Bruxelles',
        f'"{c["directory_name"]}" website Bruxelles',
    ]
    suspects = []
    tried = 0
    errors = []
    for q in queries:
        try:
            url = DDG + urllib.parse.urlencode({"q": q})
            html = fetch_url(url, timeout=20)
            tried += 1
            for u in ddg_result_links(html)[:20]:
                if suspicious_domain(c["directory_name"], u):
                    suspects.append(u)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
    suspects = list(dict.fromkeys(suspects))
    if suspects:
        return "REJECT", "POSSIBLE_OWNED_DOMAIN_IN_EXACT_SEARCH", suspects, tried, errors
    if tried == 0:
        return "ERROR_RETRYABLE", "WEB_CHALLENGE_UNAVAILABLE", [], tried, errors
    return "PASS", "NO_OWNED_DOMAIN_IN_TWO_EXACT_SEARCHES", [], tried, errors

def verify_one(c, idx, places):
    p, ns, addr_overlap, resolved = resolve_candidate(c, idx, places)
    out = dict(c)
    out.update({
        "overture_id": txt(p.get("id")) if resolved and p else "",
        "overture_name": txt(p.get("name")) if resolved and p else "",
        "name_similarity": round(ns, 3),
        "address_overlap": round(addr_overlap, 3),
        "overture_confidence": txt(p.get("confidence")) if resolved and p else "",
        "overture_websites": json.dumps(p.get("websites"), ensure_ascii=False, default=str) if resolved and p and p.get("websites") else "",
        "overture_socials": json.dumps(p.get("socials"), ensure_ascii=False, default=str) if resolved and p and p.get("socials") else "",
        "owned_website": "",
        "brand": "",
        "web_challenge": "",
        "web_reason": "",
        "web_suspects": "",
        "outcome": "UNCERTAIN",
        "reason": "NO_EXACT_CURRENT_OVERTURE_MATCH",
    })
    if not resolved or not p:
        return out

    site = owned_site(p.get("websites"))
    brand = brand_name(p.get("brand"))
    out["owned_website"] = site
    out["brand"] = brand
    if site:
        out["outcome"] = "REJECT"
        out["reason"] = "OWNED_SITE_FOUND_IN_CURRENT_OVERTURE"
        return out
    if brand and any(cw in norm(brand) for cw in CHAIN_WORDS):
        out["outcome"] = "REJECT"
        out["reason"] = "CHAIN_BRAND"
        return out

    wc, wr, suspects, tried, errors = web_challenge(c)
    out["web_challenge"] = wc
    out["web_reason"] = wr
    out["web_suspects"] = json.dumps(suspects, ensure_ascii=False)
    out["web_queries_completed"] = tried
    out["web_errors"] = json.dumps(errors, ensure_ascii=False)
    if wc == "PASS":
        out["outcome"] = "HIGH"
        out["reason"] = "EXPLICIT_NEGATIVE_SITE_SIGNAL+CURRENT_ENTITY+TWO_PASS_DOMAIN_CHALLENGE"
    elif wc == "REJECT":
        out["outcome"] = "REJECT"
        out["reason"] = wr
    else:
        out["outcome"] = "ERROR_RETRYABLE"
        out["reason"] = wr
    return out

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    reservoir, source_stats = fetch_negative_reservoir()
    places = load_overture()
    idx = build_place_index(places)

    verified = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(verify_one, c, idx, places): c for c in reservoir}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                verified.append(fut.result())
            except Exception as exc:
                r = dict(c)
                r.update({
                    "outcome": "ERROR_RETRYABLE",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "owned_website": "",
                    "web_challenge": "ERROR_RETRYABLE",
                })
                verified.append(r)

    verified.sort(key=lambda r: (
        r["commune_priority"],
        0 if r.get("outcome") == "HIGH" else 1,
        -float(r.get("overture_confidence") or 0),
        norm(r["directory_name"]),
    ))

    highs = [r for r in verified if r.get("outcome") == "HIGH"]
    rejects = [r for r in verified if r.get("outcome") == "REJECT"]
    uncertain = [r for r in verified if r.get("outcome") == "UNCERTAIN"]
    errors = [r for r in verified if r.get("outcome") == "ERROR_RETRYABLE"]

    fields = sorted({k for r in verified for k in r.keys()})
    for fn, data in [
        ("south_negative_reservoir.csv", reservoir),
        ("south_verified.csv", verified),
        ("south_high.csv", highs),
        ("south_reject.csv", rejects),
    ]:
        flds = fields if fn != "south_negative_reservoir.csv" else sorted({k for r in data for k in r.keys()})
        with (OUT_DIR / fn).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=flds, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    by_commune = defaultdict(Counter)
    for r in verified:
        by_commune[r["commune"]][r["outcome"]] += 1

    summary = {
        "mode": "SOUTH_FIRST_EXPLICIT_NEGATIVE_SITE",
        "sources": source_stats,
        "raw_directory_entries": sum(v["entries"] for v in source_stats.values()),
        "in_commune_directory_entries": sum(v["in_commune_entries"] for v in source_stats.values()),
        "negative_site_reservoir": len(reservoir),
        "overture_places_in_bbox": len(places),
        "serious_terminal": len(verified),
        "high": len(highs),
        "reject": len(rejects),
        "uncertain": len(uncertain),
        "error_retryable": len(errors),
        "high_yield_pct": round(100 * len(highs) / max(1, len(verified)), 2),
        "by_commune": {k: dict(v) for k, v in by_commune.items()},
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("GWS_SOUTH_NEGATIVE_SITE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
