#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import html
import json
import os
import random
import re
import time
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# Strict legacy challenger. This file intentionally NEVER emits HIGH.
# It finds reasons to reject/withhold legacy no-site claims and only sends
# survivors to strict review. Search/provider failures are retryable errors.

REL = os.getenv("OVERTURE_RELEASE", "2026-06-17.0")
OVT = f"s3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*"
BBOX = (4.20, 50.75, 4.55, 50.95)
PCS = {
    "1000", "1020", "1030", "1040", "1050", "1060", "1070", "1080",
    "1081", "1082", "1083", "1090", "1120", "1130", "1140", "1150",
    "1160", "1170", "1180", "1190", "1200", "1210",
}
PLAT = (
    "facebook.", "instagram.", "linkedin.", "tiktok.", "youtube.",
    "google.", "g.page", "maps.apple.", "waze.", "pagesdor.", "goudengids.",
    "bizique.", "cylex.", "opendi.", "openingsuren.", "heures.", "selfcity.",
    "treatwell.", "planity.", "fresha.", "salonkee.", "nearcut.", "booking.",
    "tripadvisor.", "yelp.", "companyweb.", "infobel.", "bottin.",
    "atout-commerces.", "lokal-handel.", "pappers.", "openthebox.", "fsma.",
    "garagebelgique.", "nosavis.", "wanderlog.", "audentia-gestion.",
)
SEARCH_HOSTS = ("google.", "bing.com", "duckduckgo.com", "html.duckduckgo.com")
STOP = {
    "the", "de", "la", "le", "les", "du", "des", "et", "and", "a", "au",
    "aux", "sa", "sprl", "srl", "bv", "nv", "bruxelles", "brussels", "belgium",
    "belgique", "be", "services", "service", "company", "societe",
}
UA = os.getenv(
    "GWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GWSVerifier/2.0",
)
MAXBODY = 450_000


def t(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def n(v):
    s = unicodedata.normalize("NFKD", t(v)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def dg(v):
    d = re.sub(r"\D+", "", t(v))
    if d.startswith("0032"):
        d = d[2:]
    if d.startswith("0") and len(d) >= 9:
        d = "32" + d[1:]
    return d


def toks(v):
    return {x for x in n(v).split() if len(x) > 1 and x not in STOP}


def sim(a, b):
    a, b = n(a), n(b)
    if not a or not b:
        return 0.0
    s = SequenceMatcher(None, a, b).ratio()
    A, B = toks(a), toks(b)
    j = len(A & B) / max(1, len(A | B))
    return max(s, 0.62 * s + 0.38 * j)


def ov(a, b):
    A, B = toks(a), toks(b)
    return len(A & B) / max(1, len(A))


def host(u):
    try:
        h = (urllib.parse.urlparse(u if "://" in u else "https://" + u).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def platform(u):
    h = host(u)
    return (not h) or any(x in h for x in PLAT) or any(x in h for x in SEARCH_HOSTS)


def owned(v):
    if v is None:
        return ""
    if not isinstance(v, (list, tuple)):
        v = [v]
    for u in v:
        u = t(u)
        if u and not platform(u):
            return u
    return ""


def _decode_queue_file(p: Path):
    compact = "".join(p.read_text(encoding="utf-8").split())
    try:
        raw = gzip.decompress(base64.b64decode(compact, validate=True))
    except Exception as e:
        raise RuntimeError(f"QUEUE_DECODE_FAILED:{p}:{type(e).__name__}:{e}") from e
    out = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception as e:
            raise RuntimeError(f"QUEUE_JSON_FAILED:{p}:{line_no}:{type(e).__name__}:{e}") from e
        if "r" not in row:
            raise RuntimeError(f"QUEUE_ROW_MISSING_R:{p}:{line_no}")
        out.append(row)
    return out


def queue(path_or_dir):
    p = Path(path_or_dir)
    files = sorted(p.glob("queue_*.jsonl.gz.b64")) if p.is_dir() else [p]
    if not files:
        raise RuntimeError(f"QUEUE_NOT_FOUND:{path_or_dir}")
    rows = []
    for f in files:
        rows.extend(_decode_queue_file(f))
    by = {}
    dup = 0
    for row in rows:
        k = int(row["r"])
        if k in by:
            dup += 1
            # Conflicting duplicate rows are fatal; exact duplicates are harmless.
            if json.dumps(by[k], sort_keys=True) != json.dumps(row, sort_keys=True):
                raise RuntimeError(f"QUEUE_CONFLICTING_DUPLICATE_R:{k}")
        by[k] = row
    return [by[k] for k in sorted(by)], {"files": [str(x) for x in files], "duplicates": dup}


def dump(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def in_scope(c):
    return re.sub(r"\D", "", t(c.get("p")))[:4] in PCS


def load_places(threads):
    import duckdb

    w, s, e, nn = BBOX
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    con.execute(f"SET threads={threads}")
    q = f"""
    SELECT id,names.primary name,websites,phones,addresses,confidence,operating_status
    FROM read_parquet('{OVT}',hive_partitioning=1)
    WHERE bbox.xmax>={w} AND bbox.xmin<={e} AND bbox.ymax>={s} AND bbox.ymin<={nn}
      AND names.primary IS NOT NULL
    """
    z = time.time()
    cur = con.execute(q)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, x)) for x in cur.fetchall()]
    return rows, round(time.time() - z, 3)


def indexes(P):
    ph, ex, tk = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, p in enumerate(P):
        ex[n(p["name"])].append(i)
        for x in p.get("phones") or []:
            d = dg(x)
            if d:
                ph[d].append(i)
        for x in list(toks(p["name"]))[:4]:
            tk[x].append(i)
    return ph, ex, tk


def resolve(c, P, I):
    ph, ex, tk = I
    cp, cn = dg(c.get("ph")), n(c.get("n"))
    ids = set(ph.get(cp, [])) | set(ex.get(cn, []))
    for x in list(toks(c.get("n")))[:4]:
        ids.update(tk.get(x, [])[:1200])
    best = None
    pc = t(c.get("p"))[:4]
    for i in ids:
        p = P[i]
        pphones = {dg(x) for x in p.get("phones") or []}
        px = bool(cp and cp in pphones)
        ns = sim(c.get("n"), p.get("name"))
        ab = json.dumps(p.get("addresses"), ensure_ascii=False, default=str)
        ao = ov(c.get("a"), ab)
        pm = bool(pc and pc in ab)
        sc = (1.6 if px else 0) + 0.8 * ns + 0.18 * ao + (0.12 if pm else 0)
        if best is None or sc > best[0]:
            best = (sc, p, px, ns, ao, pm)
    if not best:
        return None, {"resolved": False}
    _, p, px, ns, ao, pm = best
    ok = px or (ns >= 0.91 and (pm or ao >= 0.2)) or (ns >= 0.82 and pm and ao >= 0.25)
    ev = {
        "resolved": ok,
        "phone_exact": px,
        "name_similarity": round(ns, 3),
        "address_overlap": round(ao, 3),
        "postcode_match": pm,
        "overture_id": t(p.get("id")),
        "overture_name": t(p.get("name")),
        "operating_status": t(p.get("operating_status")),
    }
    return (p if ok else None), ev


def roots(name):
    x = [w for w in n(name).split() if len(w) > 2 and w not in STOP][:4]
    out = []
    for r in ("".join(x), "-".join(x)):
        if 4 <= len(r) <= 40 and r not in out:
            out.append(r)
    return out[:2]


def guesses(c):
    suffixes = (".be", ".com", ".eu", ".net")
    return [r + s for r in roots(c.get("n")) for s in suffixes][:8]


def hrefs(body, base, limit=24):
    out = []
    for h in re.findall(r'''href\s*=\s*["']([^"'#]+)''', body, re.I):
        u = html.unescape(urllib.parse.urljoin(base, h.strip()))
        if "google.com/url?" in u:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            u = (q.get("q") or q.get("url") or [u])[0]
        if u.startswith("http") and not platform(u) and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def textish(body):
    return n(re.sub(r"<[^>]+>", " ", html.unescape(body)))


def identity(c, body, url=""):
    tx = textish(body)
    p = dg(c.get("ph"))
    digits = dg(tx)
    pm = bool(p and p in digits)
    ns = ov(c.get("n"), tx)
    ao = ov(c.get("a"), tx)
    pc = t(c.get("p"))[:4]
    pcm = bool(pc and pc in tx)
    dh = host(url)
    domain_overlap = ov(c.get("n"), dh.replace(".", " "))
    matched = pm or (ns >= 0.66 and (ao >= 0.18 or pcm)) or (ns >= 0.45 and ao >= 0.35) or (domain_overlap >= 0.6 and (ao >= 0.12 or pcm))
    return {
        "matched": matched,
        "phone": pm,
        "name_overlap": round(ns, 3),
        "address_overlap": round(ao, 3),
        "postcode": pcm,
        "domain_name_overlap": round(domain_overlap, 3),
    }


def search_queries(c):
    name = t(c.get("n"))
    addr = t(c.get("a"))
    pc = t(c.get("p"))[:4]
    ph = t(c.get("ph"))
    qs = []
    if name:
        qs.append(f'"{name}" "{pc}" website')
    if name and addr:
        # Shorten address so exact punctuation differences do not destroy recall.
        qs.append(f'"{name}" "{addr[:70]}"')
    if ph:
        qs.append(f'"{ph}" website')
    return qs[:3]


async def webcheck(rows, conc, search_conc):
    import aiohttp

    sem = asyncio.Semaphore(conc)
    ssem = asyncio.Semaphore(search_conc)
    timeout = aiohttp.ClientTimeout(total=14, connect=5, sock_read=9)
    ans = {}
    headers = {"User-Agent": UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        async def get(url, is_search=False):
            attempts = 3 if is_search else 2
            last = None
            for attempt in range(attempts):
                try:
                    cm = ssem if is_search else sem
                    async with cm:
                        if is_search:
                            await asyncio.sleep(random.uniform(0.18, 0.65) * (attempt + 1))
                        async with sess.get(url, allow_redirects=True, ssl=True) as r:
                            b = (await r.content.read(MAXBODY)).decode(errors="ignore")
                            blocked = r.status in (403, 429) or any(x in b.lower() for x in ("unusual traffic", "captcha", "verify you are human"))
                            if blocked:
                                last = {"ok": False, "status": r.status, "blocked": True, "url": str(r.url)}
                            else:
                                return {"ok": True, "status": r.status, "url": str(r.url), "body": b}
                except Exception as e:
                    last = {"ok": False, "error": type(e).__name__}
                await asyncio.sleep(0.25 * (2 ** attempt))
            return last or {"ok": False, "error": "UNKNOWN"}

        async def search_provider(query):
            providers = [
                ("google", "https://www.google.com/search?hl=fr&num=10&filter=0&q=" + urllib.parse.quote_plus(query)),
                ("bing", "https://www.bing.com/search?count=10&q=" + urllib.parse.quote_plus(query)),
                ("ddg", "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)),
            ]
            health = []
            for provider, url in providers:
                q = await get(url, is_search=True)
                h = {"provider": provider, "ok": bool(q.get("ok")), "status": q.get("status"), "blocked": bool(q.get("blocked")), "error": q.get("error")}
                health.append(h)
                if q.get("ok") and q.get("status", 999) < 500:
                    links = hrefs(q.get("body", ""), q.get("url", url), limit=18)
                    return links, health
            return [], health

        async def one(c):
            ev = {
                "source_ok": False,
                "source_identity": {},
                "direct_checked": 0,
                "search_queries": 0,
                "search_successes": 0,
                "search_health": [],
                "search_candidates": [],
                "owned": "",
                "owned_identity": {},
            }
            src = t(c.get("su"))
            initial = []
            if src:
                q = await get(src)
                ev["source_ok"] = bool(q.get("ok"))
                if q.get("ok"):
                    ev["source_identity"] = identity(c, q.get("body", ""), q.get("url", src))
                    initial.extend(hrefs(q.get("body", ""), q.get("url", src), limit=10))

            # Persistent SERP discovery. Google first, then Bing/DDG only when needed.
            for sq in search_queries(c):
                ev["search_queries"] += 1
                links, health = await search_provider(sq)
                ev["search_health"].append({"query": sq, "providers": health})
                if any(x.get("ok") for x in health):
                    ev["search_successes"] += 1
                for u in links:
                    if u not in ev["search_candidates"]:
                        ev["search_candidates"].append(u)
                # If a query already yielded several independent domains, use them
                # before issuing more searches; remaining queries are for weak/no-hit cases.
                if len({host(x) for x in ev["search_candidates"]}) >= 6:
                    break

            cand = initial + ev["search_candidates"] + ["https://" + x for x in guesses(c)]
            seen = set()
            for u in cand:
                h = host(u)
                if not h or h in seen or platform(u):
                    continue
                seen.add(h)
                q = await get(u)
                ev["direct_checked"] += 1
                if q.get("ok") and q.get("status", 999) < 500:
                    ide = identity(c, q.get("body", ""), q.get("url", u))
                    if ide["matched"] and not platform(q.get("url", u)):
                        ev["owned"] = q.get("url", u)
                        ev["owned_identity"] = ide
                        break
                if ev["direct_checked"] >= 14:
                    break
            return int(c["r"]), ev

        results = await asyncio.gather(*(one(c) for c in rows))
        for r, e in results:
            ans[r] = e
    return ans


def classify(c, p, pe, w, ovok):
    r = int(c["r"])
    base = {"r": r, "candidate": c, "place": pe, "web": w}
    if not in_scope(c):
        return {**base, "status": "REJECT", "reason": "OUT_OF_SCOPE"}
    if p:
        s = owned(p.get("websites"))
        if s:
            return {**base, "status": "REJECT", "reason": "OWNED_SITE_OVERTURE", "owned_site": s}
        if t(p.get("operating_status")).lower() in {"closed", "permanently_closed"}:
            return {**base, "status": "REJECT", "reason": "CLOSED_OVERTURE"}
    if w.get("owned"):
        return {**base, "status": "REJECT", "reason": "OWNED_SITE_SEARCH_CONFIRMED", "owned_site": w["owned"]}
    if not ovok:
        return {**base, "status": "ERROR_RETRYABLE", "reason": "OVERTURE_UNAVAILABLE"}

    resolved = bool(pe.get("resolved"))
    search_ok = int(w.get("search_successes") or 0)
    searched = int(w.get("search_queries") or 0)
    checked = int(w.get("direct_checked") or 0)
    source = bool(w.get("source_ok") and (w.get("source_identity") or {}).get("matched"))

    if not resolved:
        return {**base, "status": "UNCERTAIN", "reason": "CURRENT_IDENTITY_NOT_RESOLVED"}
    if searched == 0 or search_ok == 0:
        return {**base, "status": "ERROR_RETRYABLE", "reason": "SEARCH_PROVIDERS_UNAVAILABLE"}

    ready = source and search_ok >= 1 and checked >= 3
    # MEDIUM means only "survived this bulk challenge", never strict no-site proof.
    return {
        **base,
        "status": "MEDIUM",
        "reason": "RESOLVED_SURVIVED_PERSISTENT_SEARCH_CHALLENGE",
        "strict_review_ready": ready,
    }


def preflight(a):
    rows, meta = queue(a.queue)
    out = {
        "expected": a.expected,
        "loaded_unique": len(rows),
        "queue_files": len(meta["files"]),
        "duplicate_rows": meta["duplicates"],
        "ok": len(rows) == a.expected,
    }
    print("PREFLIGHT=" + json.dumps(out, separators=(",", ":")))
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")


def worker(a):
    rows, qmeta = queue(a.queue)
    if a.expected and len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    part = [x for i, x in enumerate(rows) if i % a.worker_count == a.worker_index]
    z = time.time()
    ovok = True
    scan_error = ""
    try:
        P, scan = load_places(a.threads)
        I = indexes(P)
    except Exception as e:
        P = []
        I = (defaultdict(list), defaultdict(list), defaultdict(list))
        scan = -1
        ovok = False
        scan_error = f"{type(e).__name__}:{e}"
    W = asyncio.run(webcheck(part, a.http_concurrency, a.search_concurrency))
    out = []
    for c in part:
        p, pe = resolve(c, P, I) if ovok and in_scope(c) else (None, {"resolved": False})
        out.append(classify(c, p, pe, W.get(int(c["r"]), {}), ovok))
    d = Path(a.outdir)
    d.mkdir(parents=True, exist_ok=True)
    dump(d / "results.jsonl", out)
    S = Counter(x["status"] for x in out)
    reasons = Counter(x.get("reason") for x in out)
    summ = {
        "worker": a.worker_index,
        "attempted": len(part),
        "statuses": dict(S),
        "reasons": dict(reasons),
        "strict_review_ready": sum(bool(x.get("strict_review_ready")) for x in out),
        "owned_sites_found": sum(x.get("reason", "").startswith("OWNED_SITE") for x in out),
        "search_provider_errors": sum(x.get("reason") == "SEARCH_PROVIDERS_UNAVAILABLE" for x in out),
        "scan_seconds": scan,
        "scan_error": scan_error,
        "queue_files": len(qmeta["files"]),
        "elapsed_seconds": round(time.time() - z, 2),
    }
    (d / "summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print("DEEP_V2_WORKER=" + json.dumps(summ, separators=(",", ":")))


def aggregate(a):
    root = Path(a.input_root)
    out, sums = [], []
    for p in root.rglob("results.jsonl"):
        out.extend(json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
    for p in root.rglob("summary.json"):
        try:
            sums.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    by = {}
    duplicate_results = 0
    for x in out:
        r = int(x["r"])
        if r in by:
            duplicate_results += 1
        by[r] = x
    out = [by[k] for k in sorted(by)]
    # Never persist an apparently-successful empty/partial aggregate.
    if len(out) != a.expected:
        raise SystemExit(f"INCOMPLETE_AGGREGATE expected={a.expected} got={len(out)} summaries={len(sums)}")
    ready = [x for x in out if x.get("strict_review_ready")]
    exceptions = [x for x in out if x.get("status") in {"REJECT", "UNCERTAIN", "ERROR_RETRYABLE", "ERROR_HARD"}]
    site_hits = [x for x in out if x.get("reason", "").startswith("OWNED_SITE")]
    S = Counter(x["status"] for x in out)
    reasons = Counter(x.get("reason") for x in out)
    raw = ("\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in out) + "\n").encode()
    enc = base64.b64encode(gzip.compress(raw, 9)).decode()
    d = Path(a.outdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.jsonl.gz.b64").write_text(enc + "\n", encoding="utf-8")
    dump(d / "strict_review_ready.jsonl", ready)
    dump(d / "exceptions.jsonl", exceptions)
    dump(d / "owned_site_hits.jsonl", site_hits)
    summ = {
        "schema_version": 2,
        "expected": a.expected,
        "attempted_unique": len(out),
        "statuses": dict(S),
        "reasons": dict(reasons),
        "strict_review_ready": len(ready),
        "owned_sites_found": len(site_hits),
        "exceptions": len(exceptions),
        "duplicate_results": duplicate_results,
        "worker_summaries": len(sums),
        "run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (d / "summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print("DEEP_V2_AGG=" + json.dumps(summ, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--queue", required=True)
    p.add_argument("--expected", type=int, default=5003)
    w = sub.add_parser("worker")
    w.add_argument("--queue", required=True)
    w.add_argument("--worker-index", type=int, required=True)
    w.add_argument("--worker-count", type=int, default=20)
    w.add_argument("--threads", type=int, default=12)
    w.add_argument("--http-concurrency", type=int, default=20)
    w.add_argument("--search-concurrency", type=int, default=2)
    w.add_argument("--expected", type=int, default=5003)
    w.add_argument("--outdir", required=True)
    g = sub.add_parser("aggregate")
    g.add_argument("--input-root", required=True)
    g.add_argument("--outdir", required=True)
    g.add_argument("--expected", type=int, default=5003)
    a = ap.parse_args()
    if a.cmd == "preflight":
        preflight(a)
    elif a.cmd == "worker":
        worker(a)
    else:
        aggregate(a)


if __name__ == "__main__":
    main()
