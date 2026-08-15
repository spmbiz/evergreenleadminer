#!/usr/bin/env python3
"""Zero-spend pre-screen for strict GWS HIGH candidates.

This stage is NOT a no-website certifier. It enumerates candidates that already
have a strongly corroborated current Overture identity, then challenges them with
Bing + deterministic direct-domain probes using the production matcher. Owned
sites are rejected; survivors are persisted as a small queue for the independent
second-index two-pass certifier.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import gws_no_website_certifier_v53 as prod


def dump_jsonl(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(x, ensure_ascii=False, default=str, separators=(",", ":")) + "\n" for x in rows), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--http-concurrency", type=int, default=32)
    ap.add_argument("--search-concurrency", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=24)
    a = ap.parse_args()

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    rows, qmeta = prod.v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")

    places, scan, release = prod.load_places_fixed(a.threads)
    idx = prod.v2.indexes(places)
    strict = []
    early = Counter()

    for c in rows:
        p, pe = prod.v2.resolve(c, places, idx) if prod.v2.in_scope(c) else (None, {"resolved": False})
        cc = dict(c)
        cc["alias"] = prod.v2.t(pe.get("overture_name"))
        pre = prod.v5.preclassify(cc, p, pe, True)
        if pre:
            early[str(pre.get("reason") or "UNKNOWN")] += 1
            continue
        if pe.get("resolved") and prod.v5.strong_place_identity(pe):
            # Deliberately force the cheap path here: this stage must never call Exa
            # or any paid independent index.
            cc["_strict_high_candidate"] = False
            cc["_screen_strict_identity"] = True
            cc["_resolved_place_evidence"] = pe
            strict.append(cc)

    survivors, owned = [], []
    health = Counter()
    batch_size = max(1, a.batch_size)
    for offset in range(0, len(strict), batch_size):
        batch = strict[offset:offset + batch_size]
        W = asyncio.run(prod.v5.run_web(batch, a.http_concurrency, a.search_concurrency, 1))
        for c in batch:
            r = int(c["r"])
            w = W.get(r, {})
            for q in w.get("search_health") or []:
                for h in q.get("providers") or []:
                    p = str(h.get("provider") or "unknown")
                    health[f"{p}_attempts"] += 1
                    health[f"{p}_parsed"] += bool(h.get("parsed"))
                    health[f"{p}_blocked"] += bool(h.get("blocked"))
                    health[f"{p}_errors"] += bool(h.get("error"))
            rec = {
                "r": r,
                "candidate": {k: v for k, v in c.items() if not str(k).startswith("_")},
                "place": c.get("_resolved_place_evidence") or {},
                "screen_web": w,
            }
            if w.get("owned"):
                rec.update(status="REJECT", reason="OWNED_SITE_ZERO_SPEND_SCREEN", owned_site=w.get("owned"))
                owned.append(rec)
            else:
                rec.update(
                    status="MEDIUM",
                    reason="STRICT_IDENTITY_SURVIVED_ZERO_SPEND_SCREEN_REQUIRES_INDEPENDENT_CERTIFICATION",
                    screen_coverage=prod.v5.coverage(w),
                )
                survivors.append(rec)
        progress = {
            "strict_total": len(strict),
            "processed": min(offset + len(batch), len(strict)),
            "owned_rejects": len(owned),
            "survivors": len(survivors),
            "elapsed_seconds": round(time.time() - started, 2),
        }
        print("GWS_V55_SHORTLIST_PROGRESS=" + json.dumps(progress, separators=(",", ":")), flush=True)
        print("::notice title=GWS v5.5 shortlist screen::" + json.dumps(progress, separators=(",", ":")), flush=True)
        dump_jsonl(outdir / "partial_owned_rejects.jsonl", owned)
        dump_jsonl(outdir / "partial_survivors.jsonl", survivors)
        (outdir / "progress.json").write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")

    summary = {
        "schema": "gws-v55-zero-spend-shortlist-screen-v1",
        "expected_source": a.expected,
        "strict_identity_candidates": len(strict),
        "owned_site_rejects": len(owned),
        "independent_certification_survivors": len(survivors),
        "early_reasons": dict(early),
        "provider_health": dict(health),
        "overture_rows": len(places),
        "overture_release": release,
        "scan_seconds": scan,
        "queue_files": len(qmeta.get("files") or []),
        "elapsed_seconds": round(time.time() - started, 2),
        "final_high": 0,
        "note": "Screen only. Survivors are not HIGH until independent two-pass certification succeeds.",
    }
    dump_jsonl(outdir / "owned_rejects.jsonl", owned)
    dump_jsonl(outdir / "strict_survivors.jsonl", survivors)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("GWS_V55_SHORTLIST_SUMMARY=" + json.dumps(summary, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
