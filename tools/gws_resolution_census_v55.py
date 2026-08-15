#!/usr/bin/env python3
"""Cheap census of the immutable 5,047 GWS snapshot before web verification.

Measures how many records genuinely need the expensive strict HIGH path. No web
search is performed. This is calibration/measurement only.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import gws_no_website_certifier_v53 as cert


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--expected", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=12)
    a = ap.parse_args()

    rows, qmeta = cert.v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    places, scan, release = cert.load_places_fixed(a.threads)
    idx = cert.v2.indexes(places)

    counts = Counter()
    samples = {"strong_pending": [], "resolved_weak_pending": [], "unresolved_pending": []}
    strong_rows = []
    resolved_rows = []

    for c in rows:
        if not cert.v2.in_scope(c):
            counts["out_of_scope"] += 1
            continue
        p, pe = cert.v2.resolve(c, places, idx)
        cc = dict(c)
        cc["alias"] = cert.v2.t(pe.get("overture_name"))
        early = cert.v5.preclassify(cc, p, pe, True)
        if early:
            counts["early_total"] += 1
            counts["early_" + str(early.get("reason") or "UNKNOWN")] += 1
            continue

        counts["pending_total"] += 1
        if pe.get("resolved"):
            counts["pending_resolved"] += 1
            resolved_rows.append({"r": int(c["r"]), "n": c.get("n"), "p": c.get("p"), "a": c.get("a"), "ph": c.get("ph"), "place": pe})
            if cert.v5.strong_place_identity(pe):
                counts["pending_strong_identity"] += 1
                row = {"r": int(c["r"]), "n": c.get("n"), "p": c.get("p"), "a": c.get("a"), "ph": c.get("ph"), "alias": cc.get("alias"), "place": pe}
                strong_rows.append(row)
                if len(samples["strong_pending"]) < 30:
                    samples["strong_pending"].append(row)
            else:
                counts["pending_resolved_weak"] += 1
                if len(samples["resolved_weak_pending"]) < 15:
                    samples["resolved_weak_pending"].append({"r": int(c["r"]), "n": c.get("n"), "p": c.get("p"), "place": pe})
        else:
            counts["pending_unresolved"] += 1
            if len(samples["unresolved_pending"]) < 10:
                samples["unresolved_pending"].append({"r": int(c["r"]), "n": c.get("n"), "p": c.get("p"), "a": c.get("a"), "ph": c.get("ph"), "place": pe})

    payload = {
        "schema": "gws-resolution-census-v55",
        "expected": a.expected,
        "overture_rows": len(places),
        "overture_release": release,
        "scan_seconds": scan,
        "queue_files": len(qmeta.get("files") or []),
        "counts": dict(counts),
        "brave_queries_if_one_per_strong": counts.get("pending_strong_identity", 0),
        "brave_queries_if_two_per_strong": 2 * counts.get("pending_strong_identity", 0),
        "samples": samples,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    strong_path = out.with_name("strong_pending.jsonl")
    strong_path.write_text("".join(json.dumps(x, default=str, separators=(",", ":")) + "\n" for x in strong_rows), encoding="utf-8")
    resolved_path = out.with_name("resolved_pending.jsonl")
    resolved_path.write_text("".join(json.dumps(x, default=str, separators=(",", ":")) + "\n" for x in resolved_rows), encoding="utf-8")
    print("GWS_V55_RESOLUTION_CENSUS=" + json.dumps(payload, separators=(",", ":"), default=str), flush=True)


if __name__ == "__main__":
    main()
