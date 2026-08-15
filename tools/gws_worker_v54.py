#!/usr/bin/env python3
"""Tiered worker policy for autonomous GWS verification.

Only candidates with a strongly corroborated current identity can ever become
HIGH, so only they receive the expensive independent-index two-pass certificate.
Unresolved and resolved-but-weak identities receive one bounded owned-site
challenge and remain UNCERTAIN/MEDIUM if no site is found. HIGH gates themselves
remain strict and fail closed. Every batch is durably checkpointed.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path


def _atomic_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _atomic_dump(core, path: Path, rows) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    core.v2.dump(tmp, rows)
    tmp.replace(path)


def _checkpoint(core, d: Path, part, out, *, worker: int, stage: str, batch_index: int,
                pending_total: int, pending_resolved: int, pending_unresolved: int,
                scan: float, release: str, started: float) -> None:
    finalized = [out[int(c["r"])] for c in part if int(c["r"]) in out]
    _atomic_dump(core, d / "partial_results.jsonl", finalized)
    statuses = Counter(x.get("status") for x in finalized)
    reasons = Counter(x.get("reason") for x in finalized)
    progress = {
        "worker": worker,
        "stage": stage,
        "batch_index": batch_index,
        "partition_size": len(part),
        "finalized_rows": len(finalized),
        "pending_total": pending_total,
        "pending_resolved": pending_resolved,
        "pending_unresolved": pending_unresolved,
        "statuses": dict(statuses),
        "reasons": dict(reasons),
        "scan_seconds": scan,
        "overture_release": release,
        "elapsed_seconds": round(time.time() - started, 2),
        "checkpoint_schema": "gws-v55-worker-checkpoint-v3",
    }
    _atomic_json(d / "progress.json", progress)
    compact = json.dumps(progress, separators=(",", ":"))
    print("GWS_V55_PROGRESS=" + compact, flush=True)
    print(f"::notice title=GWS v5.5 worker {worker} progress::{compact}", flush=True)


def worker(a, core):
    rows, qmeta = core.v2.queue(a.queue)
    if len(rows) != a.expected:
        raise SystemExit(f"QUEUE_COUNT_MISMATCH expected={a.expected} got={len(rows)}")
    part = [x for i, x in enumerate(rows) if i % a.worker_count == a.worker_index]
    z = time.time()
    d = Path(a.outdir); d.mkdir(parents=True, exist_ok=True)
    try:
        P, scan, release = core.load_places_fixed(a.threads)
        if len(P) < core.MIN_OVERTURE_ROWS:
            raise RuntimeError(f"OVERTURE_SCAN_TOO_SMALL:{len(P)}<{core.MIN_OVERTURE_ROWS}:{release}")
        I = core.v2.indexes(P)
    except Exception as exc:
        summary = {
            "worker": a.worker_index, "attempted": 0, "partition_size": len(part),
            "statuses": {}, "reasons": {"OVERTURE_GLOBAL_SCAN_FAILED": len(part)},
            "scan_seconds": -1, "scan_error": f"{type(exc).__name__}:{exc}",
            "elapsed_seconds": round(time.time() - z, 2), "cert_version": core.CERT_VERSION,
        }
        _atomic_json(d / "summary.json", summary)
        print("GWS_V55_WORKER_FATAL=" + json.dumps(summary, separators=(",", ":")), flush=True)
        raise SystemExit(2)

    resolved, pending, out = {}, [], {}
    for c in part:
        p, pe = core.v2.resolve(c, P, I) if core.v2.in_scope(c) else (None, {"resolved": False})
        strong = bool(pe.get("resolved") and core.v5.strong_place_identity(pe))
        cc = dict(c)
        cc["alias"] = core.v2.t(pe.get("overture_name"))
        cc["_unresolved_challenge"] = not bool(pe.get("resolved"))
        cc["_strict_high_candidate"] = strong
        resolved[int(c["r"])] = (cc, p, pe)
        early = core.v5.preclassify(cc, p, pe, True)
        if early:
            out[int(c["r"])] = early
        else:
            pending.append(cc)

    pending_resolved = sum(not c.get("_unresolved_challenge") for c in pending)
    pending_unresolved = len(pending) - pending_resolved
    strict_high_pending = sum(bool(c.get("_strict_high_candidate")) for c in pending)
    _checkpoint(
        core, d, part, out, worker=a.worker_index, stage="resolved", batch_index=0,
        pending_total=len(pending), pending_resolved=pending_resolved,
        pending_unresolved=pending_unresolved, scan=scan, release=release, started=z,
    )

    batch_size = max(1, int(os.getenv("GWS_WEB_BATCH_SIZE", "12")))
    unresolved_challenged = 0
    resolved_weak_challenged = 0
    second_pass_candidates = 0

    for offset in range(0, len(pending), batch_size):
        batch_no = offset // batch_size + 1
        batch = pending[offset:offset + batch_size]
        W1 = asyncio.run(core.v5.run_web(batch, a.http_concurrency, a.search_concurrency, 1))
        provider_attempts = sum(
            len(q.get("providers") or [])
            for w in W1.values() for q in (w.get("search_health") or [])
        )
        if provider_attempts == 0:
            _checkpoint(
                core, d, part, out, worker=a.worker_index, stage="pass1_zero_attempts",
                batch_index=batch_no, pending_total=len(pending), pending_resolved=pending_resolved,
                pending_unresolved=pending_unresolved, scan=scan, release=release, started=z,
            )
            raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS1")

        second = []
        for c in batch:
            r = int(c["r"])
            pe = resolved[r][2]
            w = W1.get(r, {})
            if w.get("owned"):
                out[r] = {
                    "r": r, "candidate": c, "place": pe, "web_pass1": w,
                    "status": "REJECT", "reason": "OWNED_SITE_SEARCH_CONFIRMED",
                    "owned_site": w["owned"],
                }
            elif not pe.get("resolved"):
                unresolved_challenged += 1
                out[r] = {
                    "r": r, "candidate": c, "place": pe, "web_pass1": w,
                    "challenge_coverage": core.v5.coverage(w),
                    "status": "UNCERTAIN",
                    "reason": "CURRENT_IDENTITY_NOT_RESOLVED_AFTER_BOUNDED_WEB_CHALLENGE",
                }
            elif not c.get("_strict_high_candidate"):
                # Current entity is resolved but not strong enough for HIGH. Do not
                # spend a second independent-index pass on a permanently HIGH-ineligible row.
                resolved_weak_challenged += 1
                out[r] = {
                    "r": r, "candidate": c, "place": pe, "web_pass1": w,
                    "challenge_coverage": core.v5.coverage(w),
                    "status": "MEDIUM",
                    "reason": "IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH_AFTER_BOUNDED_WEB_CHALLENGE",
                }
            elif not core.v5.coverage(w)["ok"]:
                out[r] = {
                    "r": r, "candidate": c, "place": pe, "web_pass1": w,
                    "status": "ERROR_RETRYABLE", "reason": "SEARCH_COVERAGE_INSUFFICIENT_PASS1",
                }
            else:
                second.append(c)

        second_pass_candidates += len(second)
        W2 = asyncio.run(core.v5.run_web(second, a.http_concurrency, a.search_concurrency, 2)) if second else {}
        if second:
            provider_attempts2 = sum(
                len(q.get("providers") or [])
                for w in W2.values() for q in (w.get("search_health") or [])
            )
            if provider_attempts2 == 0:
                _checkpoint(
                    core, d, part, out, worker=a.worker_index, stage="pass2_zero_attempts",
                    batch_index=batch_no, pending_total=len(pending), pending_resolved=pending_resolved,
                    pending_unresolved=pending_unresolved, scan=scan, release=release, started=z,
                )
                raise SystemExit("SEARCH_ENGINE_ZERO_ATTEMPTS_PASS2")

        for c in second:
            r = int(c["r"])
            pe = resolved[r][2]
            w1 = W1[r]
            w2 = W2.get(r, {})
            if w2.get("owned"):
                out[r] = {
                    "r": r, "candidate": c, "place": pe, "web_pass1": w1, "web_pass2": w2,
                    "status": "REJECT", "reason": "OWNED_SITE_SECOND_PASS_CONFIRMED",
                    "owned_site": w2["owned"],
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

        _checkpoint(
            core, d, part, out, worker=a.worker_index, stage="web_batch_complete",
            batch_index=batch_no, pending_total=len(pending), pending_resolved=pending_resolved,
            pending_unresolved=pending_unresolved, scan=scan, release=release, started=z,
        )

    if len(out) != len(part):
        missing = [int(c["r"]) for c in part if int(c["r"]) not in out]
        raise SystemExit(f"WORKER_INCOMPLETE_AFTER_BATCHES missing={missing[:20]} count={len(missing)}")

    final = [out[int(c["r"])] for c in part]
    core.v2.dump(d / "results.jsonl", final)
    S = Counter(x["status"] for x in final)
    reasons = Counter(x.get("reason") for x in final)
    summ = {
        "worker": a.worker_index,
        "attempted": len(part),
        "statuses": dict(S),
        "reasons": dict(reasons),
        "high_verified_no_website": S.get("HIGH", 0),
        "owned_sites_found": sum(str(x.get("reason", "")).startswith("OWNED_SITE") for x in final),
        "unresolved_web_challenged": unresolved_challenged,
        "resolved_weak_web_challenged": resolved_weak_challenged,
        "strict_high_candidates": strict_high_pending,
        "second_pass_candidates": second_pass_candidates,
        "web_batch_size": batch_size,
        "web_batches": (len(pending) + batch_size - 1) // batch_size if pending else 0,
        "scan_seconds": scan,
        "scan_error": "",
        "overture_release": release,
        "overture_rows": len(P),
        "queue_files": len(qmeta["files"]),
        "elapsed_seconds": round(time.time() - z, 2),
        "cert_version": core.CERT_VERSION,
    }
    _atomic_json(d / "summary.json", summ)
    print("GWS_V55_WORKER=" + json.dumps(summ, separators=(",", ":")), flush=True)
