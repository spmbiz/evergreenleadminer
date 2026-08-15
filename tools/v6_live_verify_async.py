#!/usr/bin/env python3
"""Async current-web verifier for V6 fast-lane CSV.

Semantics intentionally mirror v6_live_verify.py. The difference is transport:
a single aiohttp ClientSession with bounded global/per-host concurrency rather
than one blocking requests call per thread. Missing/blocked sites remain
UNCERTAIN; no contact or social data is inferred.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html as htmlmod
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

HOSP = (
    "vacation", "rental", "rentals", "hotel", "resort", "villa", "villas",
    "cabin", "cabins", "chalet", "lodging", "accommodation",
    "property management", "holiday", "guest", "booking", "stay",
)
PARKED = (
    "domain is for sale", "this domain is for sale", "buy this domain",
    "domain may be for sale", "expired domain", "website is for sale",
    "parked free", "sedo domain parking", "hugedomains", "afternic",
    "dan.com", "coming soon",
)
CLOSED = (
    "permanently closed", "ceased operations", "we have closed",
    "no longer operating", "business has closed", "closed our doors",
)
UA = "Mozilla/5.0 (compatible; AIProdLeadVerifier/1.2-async; public-business-research)"
BAD_IG_PREFIXES = (
    "p/", "reel/", "reels/", "stories/", "explore/", "accounts/",
    "direct/", "about/", "legal/", "developer/",
)


def norm(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()


def tokens(name):
    stop = {
        "hotel", "resort", "vacation", "rentals", "rental", "property",
        "management", "home", "homes", "villa", "villas", "the", "and",
        "of", "at", "in", "llc", "inc", "company",
    }
    return [x for x in re.findall(r"[a-z0-9]+", name.lower()) if len(x) >= 4 and x not in stop]


def extract_instagram(raw_html, base_url):
    for href in re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']", raw_html or "", flags=re.I):
        try:
            u = urljoin(base_url, htmlmod.unescape(href).strip())
            p = urlparse(u)
            h = (p.hostname or "").lower().strip(".")
            if h.startswith("www."):
                h = h[4:]
            if h != "instagram.com":
                continue
            path = (p.path or "").strip("/")
            if not path or any(path.lower().startswith(x) for x in BAD_IG_PREFIXES):
                continue
            handle = path.split("/", 1)[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
                continue
            return f"https://www.instagram.com/{handle}/"
        except Exception:
            continue
    return ""


def classify(row, status, final_url, raw_html):
    name = norm(row.get("name"))
    email = norm(row.get("public_email"))
    out = dict(row)
    out.update({
        "live_status": "UNCERTAIN",
        "http_status": str(status or ""),
        "final_url": final_url or "",
        "hospitality_hits": "0",
        "identity_hits": "0",
        "email_on_homepage": "NO",
        "instagram": "",
        "instagram_source_url": "",
        "live_reason": "",
    })
    if status and status >= 400:
        out["live_reason"] = f"HTTP_{status}"
        return out

    ig = extract_instagram(raw_html, final_url)
    if ig:
        out["instagram"] = ig
        out["instagram_source_url"] = final_url

    text = re.sub(r"<[^>]+>", " ", (raw_html or "")[:900000]).lower()
    text = re.sub(r"\s+", " ", text)
    if any(x in text for x in PARKED):
        out["live_status"] = "REJECT"
        out["live_reason"] = "PARKED_OR_FOR_SALE"
        return out
    if any(x in text for x in CLOSED):
        out["live_status"] = "REJECT"
        out["live_reason"] = "CLOSED_SIGNAL"
        return out

    hh = sum(1 for x in HOSP if x in text)
    it = sum(1 for x in tokens(name) if x in text)
    out["hospitality_hits"] = str(hh)
    out["identity_hits"] = str(it)
    out["email_on_homepage"] = "YES" if email and email.lower() in text else "NO"
    strong_name = any(x in name.lower() for x in (
        "vacation rental", "vacation rentals", "holiday rental", "cabin rental",
        "cabin rentals", "chalet rental", "villa rental", "property management",
    ))
    if hh >= 2 and (it >= 1 or strong_name):
        out["live_status"] = "HIGH"
        out["live_reason"] = "CURRENT_HOSPITALITY_IDENTITY"
    elif hh >= 1 and (it >= 1 or strong_name):
        out["live_status"] = "MEDIUM"
        out["live_reason"] = "CURRENT_WEAK_HOSPITALITY_IDENTITY"
    else:
        out["live_status"] = "UNCERTAIN"
        out["live_reason"] = "INSUFFICIENT_CURRENT_IDENTITY_PROOF"
    return out


async def fetch_one(session, sem, row, timeout):
    url = norm(row.get("website"))
    base = dict(row)
    base.update({
        "live_status": "UNCERTAIN", "http_status": "", "final_url": "",
        "hospitality_hits": "0", "identity_hits": "0",
        "email_on_homepage": "NO", "instagram": "",
        "instagram_source_url": "", "live_reason": "",
    })
    try:
        async with sem:
            async with session.get(url, allow_redirects=True, max_redirects=6, timeout=timeout) as resp:
                body = await resp.content.read(900000)
                charset = resp.charset or "utf-8"
                try:
                    raw_html = body.decode(charset, errors="ignore")
                except LookupError:
                    raw_html = body.decode("utf-8", errors="ignore")
                return classify(row, resp.status, str(resp.url), raw_html)
    except asyncio.TimeoutError:
        base["live_reason"] = "NETWORK_TIMEOUT"
    except aiohttp.TooManyRedirects:
        base["live_reason"] = "NETWORK_TOOMANYREDIRECTS"
    except aiohttp.ClientConnectorCertificateError:
        base["live_reason"] = "NETWORK_SSLERROR"
    except aiohttp.ClientSSLError:
        base["live_reason"] = "NETWORK_SSLERROR"
    except aiohttp.ClientConnectorError:
        base["live_reason"] = "NETWORK_CONNECTIONERROR"
    except aiohttp.ClientError as e:
        base["live_reason"] = "NETWORK_" + type(e).__name__.upper()
    except Exception as e:
        base["live_reason"] = "NETWORK_" + type(e).__name__.upper()
    return base


async def verify_all(rows, concurrency, per_host, timeout_seconds):
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=min(3.0, timeout_seconds),
        sock_connect=min(3.0, timeout_seconds),
        sock_read=timeout_seconds,
    )
    connector = aiohttp.TCPConnector(
        limit=concurrency,
        limit_per_host=max(1, per_host),
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    async with aiohttp.ClientSession(
        headers={"User-Agent": UA},
        connector=connector,
        timeout=timeout,
        trust_env=False,
    ) as session:
        tasks = [asyncio.create_task(fetch_one(session, sem, r, timeout)) for r in rows]
        out = []
        for fut in asyncio.as_completed(tasks):
            out.append(await fut)
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--workers", type=int, default=128, help="global async concurrency")
    ap.add_argument("--per-host", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=7.0)
    a = ap.parse_args()

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(a.input, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    verified = asyncio.run(verify_all(rows, max(1, a.workers), max(1, a.per_host), a.timeout))
    verified.sort(key=lambda r: (
        r.get("live_status") != "HIGH",
        r.get("live_status") != "MEDIUM",
        -(int(r.get("operator_score") or 0)),
        -(int(r.get("premium_score") or 0)),
        r.get("name", "").lower(),
    ))
    fields = list(verified[0].keys()) if verified else []
    with (outdir / "v6_live_verified.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(verified)
    keep = [r for r in verified if r["live_status"] in ("HIGH", "MEDIUM")]
    with (outdir / "v6_live_ready.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(keep)

    summary = {
        "engine": "asyncio-aiohttp",
        "input_fast_ready": len(rows),
        "live_high": sum(r["live_status"] == "HIGH" for r in verified),
        "live_medium": sum(r["live_status"] == "MEDIUM" for r in verified),
        "live_reject": sum(r["live_status"] == "REJECT" for r in verified),
        "live_uncertain": sum(r["live_status"] == "UNCERTAIN" for r in verified),
        "live_ready": len(keep),
        "instagram_found": sum(bool(r.get("instagram")) for r in keep),
        "concurrency": a.workers,
        "per_host": a.per_host,
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    (outdir / "v6_live_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
