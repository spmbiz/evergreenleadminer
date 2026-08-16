#!/usr/bin/env python3
"""Read-only GWS tier-0 HTTP benchmark using ProjectDiscovery httpx.

This is deliberately NOT a production qualification gate. It extracts only explicit
website/domain candidates already present in durable GWS evidence, probes them with
httpx, compares basic reachability/timing against a small requests baseline, and
writes benchmark artifacts. It never mutates canonical/state files.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

KEYS = {
    "website", "websites", "owned_website", "candidate_url", "candidate_domain",
    "official_domain", "official_url", "final_url", "owned", "final",
}
THIRD_PARTY = {
    "google.com", "google.be", "googleusercontent.com", "facebook.com",
    "instagram.com", "linkedin.com", "youtube.com", "tiktok.com", "x.com",
    "twitter.com", "bing.com", "duckduckgo.com", "yelp.com", "tripadvisor.com",
}
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower().strip(".")
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def third_party(h: str) -> bool:
    return any(h == d or h.endswith("." + d) for d in THIRD_PARTY)


def normalize_candidate(value: Any) -> list[str]:
    vals: list[str] = []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return vals
        # Some persisted fields contain JSON arrays encoded as strings.
        if s[:1] in "[{":
            try:
                return normalize_candidate(json.loads(s))
            except Exception:
                pass
        found = URL_RE.findall(s)
        if found:
            vals.extend(found)
        elif re.fullmatch(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:/.*)?", s):
            vals.append("https://" + s)
    elif isinstance(value, list):
        for x in value:
            vals.extend(normalize_candidate(x))
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in KEYS:
                vals.extend(normalize_candidate(v))
    out: list[str] = []
    for u in vals:
        u = u.rstrip(".,;:)]}")
        h = host(u)
        if h and not third_party(h):
            out.append(u)
    return out


def walk(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in KEYS:
                yield from normalize_candidate(v)
            if isinstance(v, (dict, list)):
                yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def iter_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    if path.suffix == ".jsonl":
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            x = json.loads(line)
                            if isinstance(x, dict):
                                yield x
                        except Exception:
                            continue
        except Exception:
            return
    elif path.suffix == ".json":
        try:
            x = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(x, dict):
            yield x
        elif isinstance(x, list):
            for row in x:
                if isinstance(row, dict):
                    yield row


def collect(roots: list[Path], limit: int) -> list[str]:
    seen: dict[str, str] = {}
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(sorted(root.rglob("*.jsonl"), reverse=True))
            files.extend(sorted(root.rglob("*.json"), reverse=True))
    for p in files:
        for rec in iter_records(p):
            for u in walk(rec):
                h = host(u)
                if h and h not in seen:
                    seen[h] = u
                    if len(seen) >= limit:
                        return list(seen.values())
    return list(seen.values())


def requests_probe(url: str, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, headers={"User-Agent": "AIProd-GWS-Tier0-Benchmark/1.0"}, stream=True)
        return {
            "url": url, "ok": True, "status_code": r.status_code, "final_url": r.url,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:
        return {"url": url, "ok": False, "error": type(exc).__name__, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


def run_httpx(binary: str, targets: Path, output: Path, threads: int, timeout: int) -> tuple[float, int]:
    cmd = [
        binary, "-l", str(targets), "-silent", "-json", "-sc", "-title", "-cl", "-ct",
        "-location", "-rt", "-server", "-cdn", "-cname", "-tls-probe", "-hash", "sha256",
        "-favicon", "-fr", "-maxr", "5", "-timeout", str(timeout), "-retries", "1",
        "-threads", str(threads), "-rate-limit", "80",
    ]
    t0 = time.perf_counter()
    with output.open("w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True, check=False)
    elapsed = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError(f"httpx_exit_{p.returncode}: {p.stderr[-1200:]}")
    rows = sum(1 for line in output.read_text(encoding="utf-8").splitlines() if line.strip())
    return elapsed, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", default=["gpt/gws_review", "gpt/gws_verified_review", "data/gws/verification"])
    ap.add_argument("--outdir", default="results/gws_httpx_canary")
    ap.add_argument("--max-targets", type=int, default=120)
    ap.add_argument("--threads", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=7)
    ap.add_argument("--httpx", default="httpx")
    a = ap.parse_args()

    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    targets = collect([Path(x) for x in a.roots], max(1, a.max_targets))
    target_path = out / "targets.txt"
    target_path.write_text("".join(x + "\n" for x in targets), encoding="utf-8")
    if not targets:
        summary = {"status": "NO_TARGETS", "targets": 0, "production_enabled": False}
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary)); return

    httpx_out = out / "httpx.jsonl"
    hx_elapsed, hx_rows = run_httpx(a.httpx, target_path, httpx_out, a.threads, a.timeout)

    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=min(a.threads, len(targets))) as ex:
        baseline = list(ex.map(lambda u: requests_probe(u, a.timeout), targets))
    req_elapsed = time.perf_counter() - t0
    (out / "requests_baseline.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in baseline), encoding="utf-8")

    hx = []
    for line in httpx_out.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try: hx.append(json.loads(line))
            except Exception: pass
    metadata = {
        k: sum(1 for r in hx if r.get(k) not in (None, "", [], {}))
        for k in ("status_code", "title", "content_length", "content_type", "location", "response_time", "webserver", "cdn_name", "cname", "tls", "hash", "favicon")
    }
    req_ok = sum(1 for r in baseline if r.get("ok"))
    req_live = sum(1 for r in baseline if r.get("ok") and int(r.get("status_code") or 0) < 500)
    hx_live = sum(1 for r in hx if int(r.get("status_code") or 0) < 500)
    summary = {
        "status": "BENCHMARK_ONLY",
        "production_enabled": False,
        "targets": len(targets),
        "httpx_rows": hx_rows,
        "httpx_live_lt500": hx_live,
        "requests_responses": req_ok,
        "requests_live_lt500": req_live,
        "httpx_elapsed_seconds": round(hx_elapsed, 3),
        "requests_elapsed_seconds": round(req_elapsed, 3),
        "httpx_targets_per_second": round(len(targets) / hx_elapsed, 2) if hx_elapsed else None,
        "requests_targets_per_second": round(len(targets) / req_elapsed, 2) if req_elapsed else None,
        "httpx_metadata_nonempty": metadata,
        "decision_rule": "Do not production-enable from speed alone; require equal-or-better useful reachability plus materially useful metadata on the same candidate set.",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()
