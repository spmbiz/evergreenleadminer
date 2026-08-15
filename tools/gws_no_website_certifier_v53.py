#!/usr/bin/env python3
"""GWS autonomous no-website certifier v5.3 hardening layer.

GPT is not part of certification. This wrapper hardens v5 with:
- dynamic Overture release discovery via the official STAC catalog;
- a schema-smoke gate and corrected DuckDB projection;
- fail-hard worker behavior when the global resolver is unavailable;
- SERP usability based on parseability, not whether an owned-domain candidate exists;
- authoritative DNS-negative handling for guessed domains;
- explicit provider-attempt sanity checks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import socket
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import gws_legacy_deep_v2 as v2
import gws_legacy_deep_v4 as v4
import gws_no_website_certifier_v5 as v5

CERT_VERSION = "gws-no-website-v5.3"
STAC_URL = os.getenv("OVERTURE_STAC_URL", "https://stac.overturemaps.org/catalog.json")
RELEASE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}\.\d+$")
MIN_OVERTURE_ROWS = int(os.getenv("GWS_MIN_OVERTURE_BRUSSELS_ROWS", "1000"))

v5.CERT_VERSION = CERT_VERSION


def resolve_overture_release() -> str:
    pinned = (os.getenv("OVERTURE_RELEASE") or "").strip()
    if pinned:
        if not RELEASE_RE.match(pinned):
            raise RuntimeError(f"INVALID_OVERTURE_RELEASE:{pinned}")
        return pinned
    req = urllib.request.Request(STAC_URL, headers={"User-Agent": "GWSVerifier/5.3"})
    with urllib.request.urlopen(req, timeout=20) as r:
        catalog = json.load(r)
    release = str(catalog.get("latest") or "").strip()
    if not RELEASE_RE.match(release):
        raise RuntimeError(f"OVERTURE_STAC_LATEST_INVALID:{release!r}")
    return release


def overture_query(release: str, limit: int | None = None) -> str:
    w, s, e, nn = v2.BBOX
    path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    lim = f" LIMIT {int(limit)}" if limit else ""
    return f"""
    SELECT
      id,
      names.primary AS \"name\",
      websites,
      phones,
      addresses,
      confidence,
      operating_status
    FROM read_parquet('{path}', hive_partitioning=1)
    WHERE bbox.xmax >= {w} AND bbox.xmin <= {e}
      AND bbox.ymax >= {s} AND bbox.ymin <= {nn}
      AND names.primary IS NOT NULL{lim}
    """


def load_places_fixed(threads: int, limit: int | None = None):
    import duckdb
    release = resolve_overture_release()
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
        con.execute(f"SET threads={max(1, int(threads))}")
        z = time.time()
        cur = con.execute(overture_query(release, limit=limit))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, x)) for x in cur.fetchall()]
        elapsed = round(time.time() - z, 3)
    finally:
        con.close()
    if not rows:
        raise RuntimeError(f"OVERTURE_EMPTY_BRUSSELS_SCAN:{release}")
    return rows, elapsed, release


def overture_smoke(threads: int):
    rows, elapsed, release = load_places_fixed(threads, limit=1)
    row = rows[0]
    required = {"id", "name", "websites", "phones", "addresses", "confidence", "operating_status"}
    missing = sorted(required - set(row))
    if missing:
        raise SystemExit("OVERTURE_SCHEMA_MISSING:" + ",".join(missing))
    print("OVERTURE_SMOKE_OK=" + json.dumps({
        "release": release,
        "elapsed_seconds": elapsed,
        "sample_id": str(row.get("id") or ""),
        "columns": sorted(row),
    }, separators=(",", ":")))


def _serp_parsed(provider: str, body: str) -> bool:
    low = (body or "").lower()
    if len(low) < 800:
        return False
    common_negative = (
        "did not match any documents", "no results", "aucun résultat", "geen resultaten",
        "we did not find results", "there are no results for",
    )
    if any(x in low for x in common_negative):
        return True
    if provider == "google":
        return ('id="search"' in low or "id='search'" in low or "data-snhf" in low or "class=\"g\"" in low)
    if provider == "bing":
        return ('id="b_results"' in low or "class=\"b_algo" in low or "b_results" in low)
    if provider == "ddg":
        return ("result__a" in low or "result__body" in low or "results_links" in low)
    return False


def _dns_negative(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "dns" in name
        or isinstance(exc, socket.gaierror)
        or "name or service not known" in text
        or "nodename nor servname" in text
        or "no address associated with hostname" in text
        or ("name resolution" in text and "temporary failure" not in text)
    )


async def webcheck_hardened(rows, conc, search_conc):
    import aiohttp
    sem = asyncio.Semaphore(conc)
    ssem = asyncio.Semaphore(search_conc)
    timeout = aiohttp.ClientTimeout(total=16, connect=5, sock_read=10)
    ans = {}
    headers = {"User-Agent": v2.UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async def get(url, is_search=False):
            attempts = 3 if is_search else 2
            last = {}
            for attempt in range(attempts):
                try:
                    async with (ssem if is_search else sem):
                        if is_search:
                            await asyncio.sleep(random.uniform(.15, .45) * (attempt + 1))
                        async with sess.get(url, allow_redirects=True, ssl=True) as r:
                            body = (await r.content.read(v2.MAXBODY)).decode(errors="ignore")
                            low = body.lower()
                            blocked = r.status in (403, 429) or any(x in low for x in (
                                "unusual traffic", "captcha", "verify you are human", "detected unusual",
                                "before you continue to google", "consent.google.com",
                            ))
                            if blocked:
                                last = {"ok": False, "status": r.status, "blocked": True, "url": str(r.url)}
                            else:
                                return {"ok": True, "status": r.status, "url": str(r.url), "body": body}
                except Exception as exc:
                    if not is_search and _dns_negative(exc):
                        return {"ok": False, "status": 404, "dns_negative": True, "error": ""}
                    last = {"ok": False, "status": 0, "error": type(exc).__name__, "error_detail": str(exc)[:160]}
                await asyncio.sleep(.25 * (2 ** attempt))
            return last or {"ok": False, "status": 0, "error": "UNKNOWN"}

        def providers(query):
            q = urllib.parse.quote_plus(query)
            return [
                ("google", f"https://www.google.com/search?hl=fr&num=10&filter=0&q={q}"),
                ("bing", f"https://www.bing.com/search?count=10&q={q}"),
                ("ddg", f"https://html.duckduckgo.com/html/?q={q}"),
            ]

        async def one(c):
            ev = {
                "search_queries": 0, "search_usable_queries": 0, "search_health": [],
                "search_candidates": [], "healthy_providers": [], "direct_checked": 0,
                "direct_health": [], "owned": "", "owned_identity": {}, "owned_via": "",
                "candidate_seeds": [],
            }
            seeds = v4.guesses(c)
            ev["candidate_seeds"] = seeds[:]
            for sq in v4.search_queries(c):
                ev["search_queries"] += 1
                qlinks, qseen, qhealth = [], set(), []
                parsed_count = 0
                for provider, url in providers(sq):
                    resp = await get(url, is_search=True)
                    http_ok = bool(resp.get("ok") and int(resp.get("status") or 999) < 400 and not resp.get("blocked"))
                    parsed = bool(http_ok and _serp_parsed(provider, resp.get("body", "")))
                    if parsed:
                        parsed_count += 1
                        if provider not in ev["healthy_providers"]:
                            ev["healthy_providers"].append(provider)
                    links = v4.hrefs(resp.get("body", ""), resp.get("url", url), 24) if parsed else []
                    qhealth.append({
                        "provider": provider, "http_ok": http_ok, "parsed": parsed,
                        "status": resp.get("status"), "blocked": bool(resp.get("blocked")),
                        "error": resp.get("error"), "external_domains": len({v2.host(x) for x in links}),
                    })
                    for u in links:
                        h = v2.host(u)
                        if h and h not in qseen:
                            qseen.add(h); qlinks.append(u)
                if parsed_count >= 2:
                    ev["search_usable_queries"] += 1
                ev["search_health"].append({"query": sq, "providers": qhealth, "parsed_providers": parsed_count, "external_domains": len(qseen)})
                existing = {v2.host(x) for x in ev["search_candidates"]}
                for u in qlinks:
                    h = v2.host(u)
                    if h and h not in existing:
                        ev["search_candidates"].append(u); existing.add(h)
                if len(existing) >= 16 and ev["search_usable_queries"] >= 2:
                    break
            cand = seeds + ev["search_candidates"]
            seen = set()
            for u in cand:
                h = v2.host(u)
                if not h or h in seen or v2.platform(u):
                    continue
                seen.add(h)
                resp = await get(u)
                ev["direct_checked"] += 1
                dh = {
                    "seed": u, "final": resp.get("url", u), "status": resp.get("status"),
                    "ok": bool(resp.get("ok")), "error": resp.get("error"),
                    "dns_negative": bool(resp.get("dns_negative")),
                }
                if resp.get("ok") and not v4._dead(int(resp.get("status") or 999), resp.get("body", "")):
                    ide = v4.identity(c, resp.get("body", ""), resp.get("url", u))
                    dh["identity"] = ide
                    if ide["matched"] and not v2.platform(resp.get("url", u)):
                        ev["owned"] = resp.get("url", u)
                        ev["owned_identity"] = ide
                        ev["owned_via"] = "prior_or_email_domain" if u in seeds else "persistent_search"
                        ev["direct_health"].append(dh)
                        break
                ev["direct_health"].append(dh)
                if ev["direct_checked"] >= 20:
                    break
            return int(c["r"]), ev
        results = await asyncio.gather(*(one(c) for c in rows))
        ans.update(results)
    return ans


v4.webcheck = webcheck_hardened
v2.load_places = lambda threads: load_places_fixed(threads)[:2]


def worker(a):
    rows, qmeta = v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    part = [x for i, x in enumerate(rows) if i % a.worker_count == a.worker_index]
    z = time.time()
    try:
        P, scan, release = load_places_fixed(a.threads)
        if len(P) < MIN_OVERTURE_ROWS:
            raise RuntimeError(f"OVERTURE_SCAN_TOO_SMALL:{len(P)}<{MIN_OVERTURE_ROWS}:{release}")
        I = v2.indexes(P)
    except Exception as exc:
        d = Path(a.outdir); d.mkdir(parents=True, exist_ok=True)
        summary = {
            "worker": a.worker_index, "attempted": 0, "partition_size": len(part),
            "statuses": {}, "reasons": {"OVERTURE_GLOBAL_SCAN_FAILED": len(part)},
            "scan_seconds": -1, "scan_error": f"{type(exc).__name__}:{exc}",
            "elapsed_seconds": round(time.time() - z, 2), "cert_version": CERT_VERSION,
        }
        (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("GWS_V53_WORKER_FATAL=" + json.dumps(summary, separators=(",", ":")))
        raise SystemExit(2)
    resolved, pending, out = {}, [], {}
    for c in part:
        p, pe = v2.resolve(c, P, I) if v2.in_scope(c) else (None, {"resolved": False})
        cc = dict(c); cc["alias"] = v2.t(pe.get("overture_name")); resolved[int(c["r"])] = (cc, p, pe)
        early = v5.preclassify(cc, p, pe, True)
        if early: out[int(c["r"])] = early
        else: pending.append(cc)
    W1 = asyncio.run(v5.run_web(pending, a.http_concurrency, a.search_concurrency, 1)) if pending else {}
    if pending:
        provider_attempts = sum(len(q.get("providers") or []) for w in W1.values() for q in (w.get("search_health") or []))
        if provider_attempts == 0:
            raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS1")
    second = []
    for c in pending:
        r = int(c["r"]); pe = resolved[r][2]; w = W1.get(r, {})
        if w.get("owned"):
            out[r] = {"r": r, "candidate": c, "place": pe, "web_pass1": w, "status": "REJECT", "reason": "OWNED_SITE_SEARCH_CONFIRMED", "owned_site": w["owned"]}
        elif not v5.coverage(w)["ok"]:
            out[r] = {"r": r, "candidate": c, "place": pe, "web_pass1": w, "status": "ERROR_RETRYABLE", "reason": "SEARCH_COVERAGE_INSUFFICIENT_PASS1"}
        else:
            second.append(c)
    W2 = asyncio.run(v5.run_web(second, a.http_concurrency, a.search_concurrency, 2)) if second else {}
    if second:
        provider_attempts2 = sum(len(q.get("providers") or []) for w in W2.values() for q in (w.get("search_health") or []))
        if provider_attempts2 == 0:
            raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS2")
    for c in second:
        r = int(c["r"]); pe = resolved[r][2]; w1 = W1[r]; w2 = W2.get(r, {})
        if w2.get("owned"):
            out[r] = {"r": r, "candidate": c, "place": pe, "web_pass1": w1, "web_pass2": w2, "status": "REJECT", "reason": "OWNED_SITE_SECOND_PASS_CONFIRMED", "owned_site": w2["owned"]}
            continue
        cert = v5.certificate(c, pe, w1, w2)
        if not v5.coverage(w2)["ok"]: st, reason = "ERROR_RETRYABLE", "SEARCH_COVERAGE_INSUFFICIENT_PASS2"
        elif cert["unresolved_plausible_domains"]: st, reason = "UNCERTAIN", "PLAUSIBLE_DOMAIN_UNRESOLVED"
        elif not cert["gates"]["current_identity_strong"]: st, reason = "MEDIUM", "IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH"
        elif cert["verified"]: st, reason = "HIGH", "VERIFIED_NO_WEBSITE"
        else: st, reason = "MEDIUM", "SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE"
        out[r] = {"r": r, "candidate": c, "place": pe, "web_pass1": w1, "web_pass2": w2, "certificate": cert, "status": st, "reason": reason}
    final = [out[int(c["r"])] for c in part]
    d = Path(a.outdir); d.mkdir(parents=True, exist_ok=True); v2.dump(d / "results.jsonl", final)
    S = Counter(x["status"] for x in final); reasons = Counter(x.get("reason") for x in final)
    summ = {
        "worker": a.worker_index, "attempted": len(part), "statuses": dict(S), "reasons": dict(reasons),
        "high_verified_no_website": S.get("HIGH", 0),
        "owned_sites_found": sum(str(x.get("reason", "")).startswith("OWNED_SITE") for x in final),
        "scan_seconds": scan, "scan_error": "", "overture_release": release, "overture_rows": len(P),
        "queue_files": len(qmeta["files"]), "elapsed_seconds": round(time.time() - z, 2), "cert_version": CERT_VERSION,
    }
    (d / "summary.json").write_text(json.dumps(summ, indent=2) + "\n", encoding="utf-8")
    print("GWS_V53_WORKER=" + json.dumps(summ, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sm = sp.add_parser("overture-smoke"); sm.add_argument("--threads", type=int, default=2)
    p = sp.add_parser("preflight"); p.add_argument("--queue", required=True); p.add_argument("--expected", type=int, default=5047)
    w = sp.add_parser("worker"); w.add_argument("--queue", required=True); w.add_argument("--worker-index", type=int, required=True); w.add_argument("--worker-count", type=int, required=True); w.add_argument("--threads", type=int, default=12); w.add_argument("--http-concurrency", type=int, default=32); w.add_argument("--search-concurrency", type=int, default=2); w.add_argument("--expected", type=int, default=5047); w.add_argument("--outdir", required=True)
    g = sp.add_parser("aggregate"); g.add_argument("--input-root", required=True); g.add_argument("--outdir", required=True); g.add_argument("--expected", type=int, default=5047)
    a = ap.parse_args()
    if a.cmd == "overture-smoke": overture_smoke(a.threads)
    elif a.cmd == "preflight": v2.preflight(a)
    elif a.cmd == "worker": worker(a)
    else: v5.aggregate(a)


if __name__ == "__main__":
    main()
