#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

STOPWORDS = {
    "the", "de", "la", "le", "les", "du", "des", "et", "and", "chez", "brussels", "bruxelles",
    "srl", "sprl", "bv", "nv", "sa", "shop", "store", "service", "services", "belgium", "belgique",
}
PLATFORM_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "youtube.com",
    "treatwell.", "planity.com", "fresha.com", "salonkee.", "nearcut.", "booking.com",
    "tripadvisor.", "yelp.", "google.", "maps.apple.", "waze.com", "pagesdor.be", "goudengids.be",
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def txt(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def norm(v) -> str:
    s = unicodedata.normalize("NFKD", txt(v)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def is_platform(h: str) -> bool:
    return any(x in (h or "").lower() for x in PLATFORM_DOMAINS)


def sig_tokens(name: str) -> list[str]:
    out = []
    for token in norm(name).split():
        if token not in STOPWORDS and len(token) >= 3 and token not in out:
            out.append(token)
    return out[:4]


def candidate_hosts(row: dict, max_domains: int) -> list[str]:
    toks = sig_tokens(row.get("hub_name") or row.get("overture_name"))
    if not toks:
        return []
    variants = []
    for v in ("".join(toks), "-".join(toks), "".join(toks[:2]), "-".join(toks[:2])):
        v = re.sub(r"[^a-z0-9-]", "", v)
        if len(v) >= 4 and v not in variants:
            variants.append(v)
    hosts = []
    for tld in ("be", "com", "brussels"):
        for v in variants:
            h = f"{v}.{tld}"
            if h not in hosts:
                hosts.append(h)
            if len(hosts) >= max_domains:
                return hosts
    return hosts


def page_identity_score(row: dict, final_url: str, body: str) -> tuple[bool, dict]:
    final_host = host(final_url)
    if not final_host or is_platform(final_host):
        return False, {}
    title_m = TITLE_RE.search(body)
    title = TAG_RE.sub(" ", title_m.group(1)) if title_m else ""
    page = norm(TAG_RE.sub(" ", body[:65536]))
    name = norm(row.get("hub_name") or row.get("overture_name"))
    if not name:
        return False, {}
    title_score = SequenceMatcher(None, name, norm(title)).ratio() if title else 0.0
    nt = set(sig_tokens(name))
    pt = set(page.split())
    coverage = len(nt & pt) / max(1, len(nt))
    slug = norm(final_host.split(".")[0])
    slug_score = SequenceMatcher(None, norm("".join(sig_tokens(name))), slug.replace(" ", "")).ratio()
    phone = re.sub(r"\D", "", txt(row.get("overture_phone")))
    body_digits = re.sub(r"\D", "", body[:65536])
    phone_match = bool(len(phone) >= 7 and phone[-7:] in body_digits)
    email = txt(row.get("overture_email")).lower()
    email_match = bool(email and email in body.lower())
    postcode = txt(row.get("hub_postalcode"))
    postcode_match = bool(postcode and re.search(rf"(?<!\d){re.escape(postcode)}(?!\d)", body))
    locality_match = any(x in page for x in ("bruxelles", "brussel", "brussels"))
    strong_name = title_score >= 0.78 or coverage >= 0.80
    location = postcode_match or locality_match
    matched = bool(phone_match or email_match or (strong_name and location and slug_score >= 0.42))
    evidence = {
        "final_url": final_url,
        "title": txt(title)[:180],
        "title_similarity": round(title_score, 3),
        "token_coverage": round(coverage, 3),
        "slug_similarity": round(slug_score, 3),
        "phone_match": phone_match,
        "email_match": email_match,
        "postcode_match": postcode_match,
        "locality_match": locality_match,
    }
    return matched, evidence


async def fetch_host(session: aiohttp.ClientSession, sem: asyncio.Semaphore, candidate: str, timeout: float, stats: dict) -> dict:
    for scheme in ("https", "http"):
        url = f"{scheme}://{candidate}/"
        try:
            async with sem:
                async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
                    stats["requests"] += 1
                    if resp.status == 429:
                        stats["rate_429"] += 1
                    if resp.status >= 500 or resp.status in {401, 403, 404, 410, 429}:
                        continue
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "html" not in ctype and "text" not in ctype and ctype:
                        continue
                    body = (await resp.content.read(65536)).decode(errors="ignore")
                    stats["http_success"] += 1
                    return {"candidate": candidate, "url": str(resp.url), "status": resp.status, "body": body}
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError):
            stats["errors"] += 1
    return {"candidate": candidate}


async def run_probe(rows: list[dict], concurrency: int, per_host: int, max_domains: int, timeout: float) -> tuple[list[dict], dict]:
    domain_rows: dict[str, set[int]] = {}
    checked_rows = 0
    for i, row in enumerate(rows):
        if row.get("outcome") not in {"REVIEW", "UNCERTAIN"} or row.get("owned_website"):
            continue
        domains = candidate_hosts(row, max_domains)
        if not domains:
            continue
        checked_rows += 1
        row["async_probe_domains_checked"] = len(domains)
        for d in domains:
            domain_rows.setdefault(d, set()).add(i)

    stats = {
        "records_checked": checked_rows,
        "domains_unique": len(domain_rows),
        "requests": 0,
        "http_success": 0,
        "rate_429": 0,
        "errors": 0,
        "owned_sites_found": 0,
        "concurrency": concurrency,
        "per_host": per_host,
        "max_domains_per_record": max_domains,
    }
    if not domain_rows:
        return rows, stats

    connector = aiohttp.TCPConnector(limit=max(1, concurrency), limit_per_host=max(1, per_host), ttl_dns_cache=300)
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GWS-Brussels-Public-Site-Check/1.0)",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    }
    client_timeout = aiohttp.ClientTimeout(total=max(1.0, timeout), connect=min(2.0, max(1.0, timeout)))
    async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=client_timeout) as session:
        tasks = [fetch_host(session, sem, d, timeout, stats) for d in domain_rows]
        fetched = await asyncio.gather(*tasks)

    best: dict[int, tuple[float, dict]] = {}
    for result in fetched:
        if not result.get("body"):
            continue
        d = result["candidate"]
        for i in domain_rows.get(d, ()):
            matched, ev = page_identity_score(rows[i], result.get("url") or "", result["body"])
            if not matched:
                continue
            quality = float(ev.get("title_similarity") or 0) + float(ev.get("token_coverage") or 0)
            prior = best.get(i)
            if prior is None or quality > prior[0]:
                best[i] = (quality, ev)

    for i, (_, ev) in best.items():
        row = rows[i]
        row["owned_website"] = ev["final_url"]
        row["outcome"] = "REJECT"
        row["reason"] = "OWNED_SITE_FOUND_ASYNC_DOMAIN_PROBE"
        row["needs_gpt_review"] = False
        row["async_probe_evidence"] = ev
        stats["owned_sites_found"] += 1
    return rows, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", required=True)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--per-host", type=int, default=2)
    ap.add_argument("--max-domains", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=4.5)
    args = ap.parse_args()

    shard = Path(args.shard_dir)
    path = shard / "records.jsonl"
    if not path.exists():
        print(json.dumps({"status": "noop", "reason": "no_records"}))
        return 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    t0 = time.time()
    rows, stats = asyncio.run(run_probe(rows, args.concurrency, args.per_host, args.max_domains, args.timeout))
    elapsed = max(0.001, time.time() - t0)
    stats["elapsed_seconds"] = round(elapsed, 3)
    stats["requests_per_second"] = round(stats["requests"] / elapsed, 3)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")

    metrics_path = shard / "metrics.json"
    metrics = {}
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            metrics = {}
    metrics["async_probe"] = stats
    metrics["review_candidates_after_async"] = sum(1 for r in rows if r.get("outcome") == "REVIEW")
    metrics["uncertain_after_async"] = sum(1 for r in rows if r.get("outcome") == "UNCERTAIN")
    metrics["owned_site_or_chain_rejects_after_async"] = sum(1 for r in rows if r.get("outcome") == "REJECT")
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": "ok", **stats}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
