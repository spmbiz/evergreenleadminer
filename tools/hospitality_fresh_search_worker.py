#!/usr/bin/env python3
"""Production wrapper for fresh SearchFabric Hospitality discovery.

Pipeline inside one hosted runner:
  parallel fresh-search partitions -> canonical-unseen domain merge ->
  bounded first-party contact enrichment -> parallel live verify -> artifact.

Canonical safety invariants:
- this worker never mutates canonical state;
- a sane canonical-domain snapshot is mandatory before search;
- planner snapshot is preferred, durable primary is retried, LKG is fallback;
- final canonicalization remains the existing single-writer aggregate.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import fleet_runtime as fr

ROOT = Path(__file__).resolve().parents[1]
MIN_CANONICAL_DOMAINS = 10_000
DEFAULT_SEARCH_PARALLELISM = 4
PRIMARY_CANONICAL_URL = (
    "https://github.com/walidgdg1-ai/evergreenleadminer/releases/download/"
    "harvest-state/hospitality-canonical.sqlite"
)
BACKUP_CANONICAL_URL = (
    "https://github.com/walidgdg1-ai/evergreenleadminer/releases/download/"
    "harvest-state-backup/hospitality-canonical-lkg.sqlite"
)


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")


def ensure_ddgs() -> None:
    try:
        import ddgs  # noqa: F401
        return
    except Exception:
        pass
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "ddgs"])


def count_domain_snapshot(path: Path, stop_at: int = MIN_CANONICAL_DOMAINS) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    opener = gzip.open if path.suffix == ".gz" else open
    count = 0
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
                    if count >= stop_at:
                        break
    except Exception:
        return 0
    return count


def domains_from_sqlite(db: Path, out: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT domain FROM leads WHERE domain IS NOT NULL AND domain<>''")
        domains = sorted({str(r[0]).strip().lower() for r in rows if r and str(r[0]).strip()})
    finally:
        con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for domain in domains:
            f.write(domain + "\n")
    return len(domains)


def download_sqlite(url: str, target: Path, attempts: int) -> bool:
    import requests

    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with requests.get(
                url,
                stream=True,
                timeout=(8, 45),
                allow_redirects=True,
                headers={"User-Agent": "ai-prod-hospitality-fresh-snapshot/1.1"},
            ) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP_{resp.status_code}")
                with tmp.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            if tmp.stat().st_size < 1024 * 1024:
                raise RuntimeError(f"canonical asset unexpectedly small: {tmp.stat().st_size}")
            tmp.replace(target)
            return True
        except Exception as exc:
            print(json.dumps({
                "canonical_snapshot_download_retry": attempt,
                "url_role": "backup" if "backup" in url else "primary",
                "error": f"{type(exc).__name__}: {exc}",
            }))
            try:
                tmp.unlink()
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(min(8, attempt * 2))
    return False


def ensure_canonical_domains(input_path: str, root: Path) -> tuple[str, str, int]:
    supplied = Path(input_path) if input_path else Path("__missing__")
    supplied_count = count_domain_snapshot(supplied)
    if supplied_count >= MIN_CANONICAL_DOMAINS:
        return str(supplied), "planner_artifact", supplied_count

    recovered = root / "canonical-domains-recovered.txt.gz"
    db = root / "canonical-recovery.sqlite"
    errors = []
    for label, url, attempts in (
        ("durable_primary", PRIMARY_CANONICAL_URL, 4),
        ("lkg_backup", BACKUP_CANONICAL_URL, 2),
    ):
        try:
            if not download_sqlite(url, db, attempts):
                errors.append(f"{label}: download failed")
                continue
            count = domains_from_sqlite(db, recovered)
            try:
                db.unlink()
            except Exception:
                pass
            if count >= MIN_CANONICAL_DOMAINS:
                print(json.dumps({
                    "canonical_snapshot_recovered": True,
                    "source": label,
                    "domains": count,
                    "supplied_domains_seen": supplied_count,
                }))
                return str(recovered), label, count
            errors.append(f"{label}: only {count} domains")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            try:
                db.unlink()
            except Exception:
                pass

    raise RuntimeError(
        "fresh_search canonical snapshot unavailable; refusing unprefiltered run; "
        + "; ".join(errors)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="github")
    ap.add_argument("--cycle-id", required=True)
    ap.add_argument("--cursor", type=int, required=True)
    ap.add_argument("--max-queries", type=int, default=30)
    ap.add_argument("--canonical-domains", default="")
    ap.add_argument("--local-workers", type=int, default=32)
    ap.add_argument("--contact-workers", type=int, default=24)
    ap.add_argument("--search-workers", type=int, default=DEFAULT_SEARCH_PARALLELISM)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    root = Path(a.outdir)
    source = root / "source"
    recovery = root / "recovery"
    source.mkdir(parents=True, exist_ok=True)
    recovery.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    status, error = "success", ""
    canonical_source = "unknown"
    canonical_count = 0
    search_parallelism = max(1, min(int(a.search_workers or 1), int(a.max_queries or 1)))

    try:
        canonical_domains, canonical_source, canonical_count = ensure_canonical_domains(
            a.canonical_domains, root
        )
        ensure_ddgs()

        # Search was historically serial inside each hosted runner. Partitioning
        # the 30-query slice here lets one runner use its available CPU/network
        # while keeping provider concurrency bounded and measurable.
        run([
            sys.executable, "tools/hospitality_fresh_search_parallel_source.py",
            "--canonical-domains", canonical_domains,
            "--outdir", str(source),
            "--cursor", str(a.cursor),
            "--max-queries", str(a.max_queries),
            "--workers", str(search_parallelism),
        ])

        shutil.copy2(source / "v6_recovery_candidates.csv", recovery / "v6_recovery_candidates.csv")
        run([
            sys.executable, "tools/v6_public_contact_enrich.py",
            "--input", str(recovery / "v6_recovery_candidates.csv"),
            "--outdir", str(recovery),
            "--workers", str(a.contact_workers),
            "--timeout", "8",
            "--max-pages", "3",
            "--max-bytes", "700000",
        ])
        run([
            sys.executable, "tools/promote_contact_ready.py",
            "--input", str(recovery / "v6_recovery_enriched.csv"),
            "--output", str(recovery / "v6_fast_ready.csv"),
            "--summary", str(recovery / "v6_contact_ready_summary.json"),
        ])
        run([
            sys.executable, "tools/v6_live_verify.py",
            "--input", str(recovery / "v6_fast_ready.csv"),
            "--outdir", str(recovery),
            "--workers", str(a.local_workers),
            "--timeout", "8",
        ])
    except Exception as exc:
        status = "failed_retryable"
        error = f"{type(exc).__name__}: {exc}"

    src = fr.load_json(source / "fresh_search_summary.json", {})
    rec = fr.load_json(recovery / "v6_contact_recovery_summary.json", {})
    ready = fr.load_json(recovery / "v6_contact_ready_summary.json", {})
    live = fr.load_json(recovery / "v6_live_summary.json", {})
    reported_snapshot_count = int(src.get("canonical_snapshot_domains") or canonical_count or 0)
    summary = {
        "provider": a.provider,
        "cycle_id": a.cycle_id,
        "lane": "fresh_search",
        "task_type": "fresh_search",
        "shard": {
            "name": f"FRESH_SEARCH::{a.cursor}",
            "country": "MULTI",
            "region": f"FRESH_SEARCH::{a.cursor}",
            "bbox": f"fresh-search:{a.cursor}",
            "release": "daily",
        },
        "status": status,
        "error": error,
        "canonical_snapshot_source": canonical_source,
        "canonical_snapshot_domains": reported_snapshot_count,
        "search_parallelism": int(src.get("query_parallelism") or search_parallelism),
        "local_workers": a.local_workers,
        "contact_workers": a.contact_workers,
        "elapsed_seconds": round(time.time() - t0, 2),
        "raw_site_email_rows": int(src.get("raw_search_results") or 0),
        "canonical_prefilter_rejected": int(src.get("canonical_known_rejected_early") or 0),
        "fresh_candidate_domains": int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovery_candidates": int(src.get("canonical_unseen_candidate_domains") or 0),
        "recovered_public_emails": int(rec.get("recovered_public_emails") or 0),
        "contact_ready": int(ready.get("contact_ready") or 0),
        "social_or_contact_without_email": int(ready.get("social_or_contact_without_email") or 0),
        "fast_ready": int(live.get("input_fast_ready") or 0),
        "live_high": int(live.get("live_high") or 0),
        "live_medium": int(live.get("live_medium") or 0),
        "live_ready": int(live.get("live_ready") or 0),
        "instagram_found": int(rec.get("instagram_found") or live.get("instagram_found") or 0),
        "facebook_found": int(rec.get("facebook_found") or 0),
        "http_429_rate": 0.0,
        "timeout_rate": 0.0,
        "error_rate": 0.0 if status == "success" else 1.0,
        "search_cursor": a.cursor,
        "search_queries": a.max_queries,
    }
    fr.write_json(root / "worker_summary.json", summary)
    print(json.dumps(summary, indent=2))
    if status != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
