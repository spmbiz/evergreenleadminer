#!/usr/bin/env python3
"""Worker execution policy for autonomous GWS verification v5.4.

Unresolved current identities are still challenged on the web so owned sites can
be discovered, but they cannot become HIGH. Therefore they stop after one healthy
adversarial pass instead of consuming a second certification pass that cannot
change HIGH eligibility. Strongly resolved candidates retain the mandatory two
passes before VERIFIED_NO_WEBSITE.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path


def worker(a, core):
    rows, qmeta = core.v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    part = [x for i, x in enumerate(rows) if i % a.worker_count == a.worker_index]
    z = time.time()
    try:
        P, scan, release = core.load_places_fixed(a.threads)
        if len(P) < core.MIN_OVERTURE_ROWS:
            raise RuntimeError(f"OVERTURE_SCAN_TOO_SMALL:{len(P)}<{core.MIN_OVERTURE_ROWS}:{release}")
        I = core.v2.indexes(P)
    except Exception as exc:
        d = Path(a.outdir); d.mkdir(parents=True, exist_ok=True)
        summary = {
            "worker": a.worker_index, "attempted": 0, "partition_size": len(part),
            "statuses": {}, "reasons": {"OVERTURE_GLOBAL_SCAN_FAILED": len(part)},
            "scan_seconds": -1, "scan_error": f"{type(exc).__name__}:{exc}",
            "elapsed_seconds": round(time.time() - z, 2), "cert_version": core.CERT_VERSION,
        }
        (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print("GWS_V54_WORKER_FATAL=" + json.dumps(summary, separators=(",", ":")))
        raise SystemExit(2)

    resolved, pending, out = {}, [], {}
    for c in part:
        p, pe = core.v2.resolve(c, P, I) if core.v2.in_scope(c) else (None, {"resolved": False})
        cc = dict(c); cc["alias"] = core.v2.t(pe.get("overture_name"))
        resolved[int(c["r"])] = (cc, p, pe)
        early = core.v5.preclassify(cc, p, pe, True)
        if early:
            out[int(c["r"])] = early
        else:
            pending.append(cc)

    W1 = asyncio.run(core.v5.run_web(pending, a.http_concurrency, a.search_concurrency, 1)) if pending else {}
    if pending:
        provider_attempts = sum(len(q.get("providers") or []) for w in W1.values() for q in (w.get("search_health") or []))
        if provider_attempts == 0:
            raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS1")

    second = []
    unresolved_challenged = 0
    for c in pending:
        r = int(c["r"]); pe = resolved[r][2]; w = W1.get(r, {})
        if w.get("owned"):
            out[r] = {
                "r": r, "candidate": c, "place": pe, "web_pass1": w,
                "status": "REJECT", "reason": "OWNED_SITE_SEARCH_CONFIRMED", "owned_site": w["owned"],
            }
        elif not core.v5.coverage(w)["ok"]:
            out[r] = {
                "r": r, "candidate": c, "place": pe, "web_pass1": w,
                "status": "ERROR_RETRYABLE", "reason": "SEARCH_COVERAGE_INSUFFICIENT_PASS1",
            }
        elif not pe.get("resolved"):
            unresolved_challenged += 1
            out[r] = {
                "r": r, "candidate": c, "place": pe, "web_pass1": w,
                "status": "UNCERTAIN", "reason": "CURRENT_IDENTITY_NOT_RESOLVED_AFTER_WEB_CHALLENGE",
            }
        else:
            second.append(c)

    W2 = asyncio.run(core.v5.run_web(second, a.http_concurrency, a.search_concurrency, 2)) if second else {}
    if second:
        provider_attempts2 = sum(len(q.get("providers") or []) for w in W2.values() for q in (w.get("search_health") or []))
        if provider_attempts2 == 0:
            raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS2")

    for c in second:
        r = int(c["r"]); pe = resolved[r][2]; w1 = W1[r]; w2 = W2.get(r, {})
        if w2.get("owned"):
            out[r] = {
                "r": r, "candidate": c, "place": pe, "web_pass1": w1, "web_pass2": w2,
                "status": "REJECT", "reason": "OWNED_SITE_SECOND_PASS_CONFIRMED", "owned_site": w2["owned"],
            }
            continue
        cert = core.v5.certificate(c, pe, w1, w2)
        if not core.v5.coverage(w2)["ok"]:
            st, reason = "ERROR_RETRYABLE", "SEARCH_COVERAGE_INSUFFICIENT_PASS2"
        elif cert["unresolved_plausible_domains"]:
            st, reason = "UNCERTAIN", "PLAUSIBLE_DOMAIN_UNRESOLVED"
        elif not cert["gates"]["current_identity_strong"]:
            st, reason = "MEDIUM", "IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH"
        elif cert["verified"]:
            st, reason = "HIGH", "VERIFIED_NO_WEBSITE"
        else:
            st, reason = "MEDIUM", "SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE"
        out[r] = {
            "r": r, "candidate": c, "place": pe, "web_pass1": w1, "web_pass2": w2,
            "certificate": cert, "status": st, "reason": reason,
        }

    final = [out[int(c["r"])] for c in part]
    d = Path(a.outdir); d.mkdir(parents=True, exist_ok=True)
    core.v2.dump(d / "results.jsonl", final)
    S = Counter(x["status"] for x in final); reasons = Counter(x.get("reason") for x in final)
    summ = {
        "worker": a.worker_index, "attempted": len(part), "statuses": dict(S), "reasons": dict(reasons),
        "high_verified_no_website": S.get("HIGH", 0),
        "owned_sites_found": sum(str(x.get("reason", "")).startswith("OWNED_SITE") for x in final),
        "unresolved_web_challenged": unresolved_challenged,
        "second_pass_candidates": len(second),
        "scan_seconds": scan, "scan_error": "", "overture_release": release, "overture_rows": len(P),
        "queue_files": len(qmeta["files"]), "elapsed_seconds": round(time.time() - z, 2),
        "cert_version": core.CERT_VERSION,
    }
    (d / "summary.json").write_text(json.dumps(summ, indent=2) + "\n", encoding="utf-8")
    print("GWS_V54_WORKER=" + json.dumps(summ, separators=(",", ":")))
