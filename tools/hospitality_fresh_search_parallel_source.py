#!/usr/bin/env python3
"""Bounded intra-runner parallel wrapper for Hospitality fresh SearchFabric discovery.

The underlying source stays authoritative for query construction, provenance,
portal filtering and canonical-domain prefiltering. This wrapper only partitions
one worker's query slice into independent subprocesses, runs them concurrently,
and losslessly merges their read-only observations/candidates.

No canonical mutation happens here. Final canonicalization remains the existing
single-writer aggregate.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_part(part_dir: Path, canonical_domains: str, cursor: int, count: int) -> dict:
    part_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "tools/hospitality_fresh_search_source.py",
        "--canonical-domains", canonical_domains,
        "--outdir", str(part_dir),
        "--cursor", str(cursor),
        "--max-queries", str(count),
    ]
    p = subprocess.run(cmd, cwd=ROOT, text=True)
    if p.returncode:
        raise RuntimeError(f"fresh source part failed cursor={cursor} count={count} rc={p.returncode}")
    summary_path = part_dir / "fresh_search_summary.json"
    return json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if fields:
            w.writeheader()
            w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-domains", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--cursor", type=int, required=True)
    ap.add_argument("--max-queries", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    t0 = time.time()
    out = Path(a.outdir)
    parts_root = out / "parallel_parts"
    out.mkdir(parents=True, exist_ok=True)
    parts_root.mkdir(parents=True, exist_ok=True)

    total_queries = max(0, int(a.max_queries))
    worker_count = max(1, min(int(a.workers or 1), total_queries or 1))
    base, rem = divmod(total_queries, worker_count)
    specs = []
    offset = 0
    for i in range(worker_count):
        count = base + (1 if i < rem else 0)
        if count <= 0:
            continue
        specs.append((i, int(a.cursor) + offset, count, parts_root / f"part-{i:02d}"))
        offset += count

    summaries = []
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as ex:
        futs = {
            ex.submit(run_part, part_dir, a.canonical_domains, cursor, count): (i, cursor, count, part_dir)
            for i, cursor, count, part_dir in specs
        }
        for fut in as_completed(futs):
            i, cursor, count, part_dir = futs[fut]
            summary = fut.result()
            summaries.append((i, cursor, count, part_dir, summary))

    summaries.sort(key=lambda x: x[0])

    # Merge candidates by domain before the expensive contact-recovery stage.
    candidates_by_domain: dict[str, dict] = {}
    observations = []
    provider_events = []
    for _i, _cursor, _count, part_dir, _summary in summaries:
        csv_path = part_dir / "v6_recovery_candidates.csv"
        if csv_path.exists():
            with csv_path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    domain = str(row.get("domain") or "").strip().lower()
                    if domain and domain not in candidates_by_domain:
                        candidates_by_domain[domain] = row
        obs_path = part_dir / "source_observations.jsonl"
        if obs_path.exists():
            with obs_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        observations.append(line)
        ev_path = part_dir / "provider_events.json"
        if ev_path.exists():
            try:
                data = json.loads(ev_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    provider_events.extend(data)
            except Exception:
                pass

    candidate_rows = list(candidates_by_domain.values())
    write_csv(out / "v6_recovery_candidates.csv", candidate_rows)
    (out / "source_observations.jsonl").write_text(
        ("\n".join(observations) + "\n") if observations else "",
        encoding="utf-8",
    )
    (out / "provider_events.json").write_text(
        json.dumps(provider_events, indent=2) + "\n", encoding="utf-8"
    )

    first = summaries[0][4] if summaries else {}
    sum_fields = (
        "queries_planned",
        "queries_with_provider_ok",
        "provider_events",
        "raw_search_results",
        "excluded_portal_or_social",
        "canonical_known_rejected_early",
        "duplicate_domain_results",
    )
    merged = {
        "schema": "HOSPITALITY_FRESH_SEARCH_V3_PARALLEL",
        "catalog_queries_total": int(first.get("catalog_queries_total") or 0),
        "bootstrap_span": int(first.get("bootstrap_span") or 0),
        "rotation_epoch_minutes": float(first.get("rotation_epoch_minutes") or 0),
        "rotation_window": int(first.get("rotation_window") or 0),
        "requested_cursor": int(a.cursor),
        "effective_cursor": int(first.get("effective_cursor") or a.cursor),
        "canonical_unseen_candidate_domains": len(candidate_rows),
        "canonical_snapshot_domains": max(
            [int(x[4].get("canonical_snapshot_domains") or 0) for x in summaries] or [0]
        ),
        "elapsed_seconds": round(time.time() - t0, 2),
        "next_cursor": int(a.cursor) + total_queries,
        "canonical_mutation": False,
        "query_parallelism": len(specs),
        "parallel_parts": [
            {
                "cursor": cursor,
                "queries": count,
                "effective_cursor": int(summary.get("effective_cursor") or cursor),
                "raw_search_results": int(summary.get("raw_search_results") or 0),
                "canonical_unseen_candidate_domains": int(summary.get("canonical_unseen_candidate_domains") or 0),
                "elapsed_seconds": float(summary.get("elapsed_seconds") or 0),
            }
            for _i, cursor, count, _part_dir, summary in summaries
        ],
    }
    for field in sum_fields:
        merged[field] = sum(int(x[4].get(field) or 0) for x in summaries)

    (out / "fresh_search_summary.json").write_text(
        json.dumps(merged, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(merged, indent=2))


if __name__ == "__main__":
    main()
