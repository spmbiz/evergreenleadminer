#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import gws_search_verify as base


def classify_strict_fast(row: dict[str, Any], c: dict[str, Any], pe: dict[str, Any], fabric, max_queries: int) -> dict[str, Any]:
    p1 = base.strict_pass(c, fabric, 1, max_queries)
    owned = p1.get("owned")
    if owned:
        row.update({
            "outcome": "REJECT",
            "reason": "OWNED_SITE_SEARCH_CONFIRMED_PASS1",
            "needs_gpt_review": False,
            "verification_status": "REJECT",
            "verification_provider": "openserp_ci",
            "owned_website": str(owned),
            "web_pass1": p1,
            "web_pass2": {"skipped": True, "reason": "OWNED_FOUND_PASS1"},
            "certificate": {"verified": False, "reason": "OWNED_SITE_PASS1"},
            "certificate_digest": "",
        })
        return row

    p2 = base.strict_pass(c, fabric, 2, max_queries)
    owned = p2.get("owned")
    cert = base.prod.v5.certificate(c, pe, p1, p2)
    if owned:
        outcome, reason, verify = "REJECT", "OWNED_SITE_SEARCH_CONFIRMED_PASS2", "REJECT"
        review = False
    elif not base.prod.v5.coverage(p1).get("ok"):
        outcome, reason, verify = "REVIEW", "SEARCH_COVERAGE_INSUFFICIENT_PASS1", "ERROR_RETRYABLE"
        review = True
    elif not base.prod.v5.coverage(p2).get("ok"):
        outcome, reason, verify = "REVIEW", "SEARCH_COVERAGE_INSUFFICIENT_PASS2", "ERROR_RETRYABLE"
        review = True
    elif cert.get("unresolved_plausible_domains"):
        outcome, reason, verify = "UNCERTAIN", "PLAUSIBLE_DOMAIN_UNRESOLVED", "UNCERTAIN"
        review = True
    elif not cert.get("gates", {}).get("current_identity_strong"):
        outcome, reason, verify = "MEDIUM", "IDENTITY_RESOLVED_NOT_STRONG_ENOUGH_FOR_HIGH", "MEDIUM"
        review = True
    elif cert.get("verified"):
        outcome, reason, verify = "HIGH", "VERIFIED_NO_WEBSITE", "HIGH"
        review = True
    else:
        outcome, reason, verify = "MEDIUM", "SURVIVED_BUT_CERTIFICATE_GATES_INCOMPLETE", "MEDIUM"
        review = True

    row.update({
        "outcome": outcome,
        "reason": reason,
        "needs_gpt_review": review,
        "verification_status": verify,
        "verification_provider": "openserp_ci",
        "owned_website": str(owned or row.get("owned_website") or ""),
        "web_pass1": p1,
        "web_pass2": p2,
        "certificate": cert,
        "certificate_digest": str(cert.get("evidence_digest") or ""),
    })
    return row


def classify_one(index: int, original: dict[str, Any], search_cfg: dict[str, Any], max_queries: int, openserp_ready: bool):
    row = dict(original)
    try:
        c = base.candidate_from_row(row, index + 1)
        pe = base.place_from_row(row)
        ident_ok, _ = base.prod.v5.complete_identity(c)
        if not ident_ok:
            row.update({
                "outcome": "UNCERTAIN",
                "reason": "SOURCE_IDENTITY_INCOMPLETE",
                "verification_status": "UNCERTAIN",
                "verification_provider": "identity_gate",
                "needs_gpt_review": True,
            })
            return index, row

        # One requests.Session per candidate avoids sharing mutable session state across threads.
        fabric = base.SearchFabric(search_cfg)
        if openserp_ready:
            row = classify_strict_fast(row, c, pe, fabric, max_queries)
        else:
            row = base.classify_fallback(row, c, fabric, max_queries)
        return index, row
    except Exception as exc:
        row.update({
            "outcome": "REVIEW",
            "reason": "SEARCH_VERIFY_EXCEPTION",
            "verification_status": "ERROR_RETRYABLE",
            "verification_provider": "parallel_verify_guard",
            "needs_gpt_review": True,
            "verification_error": type(exc).__name__,
        })
        return index, row


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    # Atomic checkpoint: preserve every completed candidate if GitHub cancels a long shard.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-dir", required=True)
    ap.add_argument("--config", default="config/gws_search_verify.json")
    args = ap.parse_args()

    shard = Path(args.shard_dir)
    records_path = shard / "records.jsonl"
    metrics_path = shard / "metrics.json"
    progress_path = shard / "search_verify_progress.json"
    cfg = base.load_json(Path(args.config), {})
    if not cfg.get("enabled", True) or not records_path.exists():
        print(json.dumps({"status": "noop", "reason": "disabled_or_no_records"}))
        return 0

    rows = [json.loads(x) for x in records_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    search_cfg = dict(cfg.get("search") or {})
    health_fabric = base.SearchFabric(search_cfg)
    openserp_ready = health_fabric.openserp_healthy()
    max_candidates = max(0, int(cfg.get("max_candidates_per_shard") or 0))
    max_queries = max(1, int(search_cfg.get("max_queries_per_candidate") or 5))
    concurrency = max(1, min(8, int(search_cfg.get("candidate_concurrency") or 4)))
    checkpoint_every = max(1, int(search_cfg.get("checkpoint_every") or 1))

    selected: list[tuple[int, dict[str, Any]]] = []
    attempted = 0
    for idx, row in enumerate(rows):
        if row.get("outcome") == "REJECT":
            row.setdefault("verification_status", "REJECT")
            row.setdefault("verification_provider", "deterministic_presearch")
            continue
        if max_candidates and attempted >= max_candidates:
            row.update({
                "verification_status": "ERROR_RETRYABLE",
                "verification_provider": "deferred_budget",
                "reason": "SEARCH_VERIFY_BUDGET_DEFERRED",
                "needs_gpt_review": True,
            })
            continue
        attempted += 1
        selected.append((idx, row))

    started = time.time()
    completed = 0

    def checkpoint() -> None:
        write_rows(records_path, rows)
        elapsed = time.time() - started
        progress_path.write_text(json.dumps({
            "attempted": attempted,
            "completed": completed,
            "candidate_concurrency": concurrency,
            "elapsed_seconds": round(elapsed, 2),
            "candidates_per_second": round(completed / elapsed, 4) if elapsed > 0 else 0,
        }, indent=2) + "\n", encoding="utf-8")

    if concurrency == 1:
        for idx, row in selected:
            out_idx, out_row = classify_one(idx, row, search_cfg, max_queries, openserp_ready)
            rows[out_idx] = out_row
            completed += 1
            if completed % checkpoint_every == 0:
                checkpoint()
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="gwsverify") as pool:
            futures = [pool.submit(classify_one, idx, row, search_cfg, max_queries, openserp_ready) for idx, row in selected]
            for fut in as_completed(futures):
                out_idx, out_row = fut.result()
                rows[out_idx] = out_row
                completed += 1
                if completed % checkpoint_every == 0:
                    checkpoint()

    checkpoint()
    elapsed = time.time() - started
    counts = Counter()
    outcomes = Counter(str(r.get("outcome") or "") for r in rows)
    providers = Counter(str(r.get("verification_provider") or "") for r in rows)
    for r in rows:
        status = str(r.get("verification_status") or "UNKNOWN")
        counts[status] += 1
    pass2_skipped = sum(1 for r in rows if isinstance(r.get("web_pass2"), dict) and r["web_pass2"].get("skipped"))
    review = sum(1 for r in rows if r.get("needs_gpt_review") and r.get("outcome") != "REJECT")

    metrics = base.load_json(metrics_path, {})
    metrics.update({
        "review_candidates": review,
        "uncertain": int(outcomes.get("UNCERTAIN", 0)),
        "owned_site_or_chain_rejects": int(outcomes.get("REJECT", 0)),
        "strict_high_precertified": int(outcomes.get("HIGH", 0)),
        "search_verification_attempted": attempted,
        "search_verification_completed": completed,
        "search_verification_statuses": dict(counts),
        "search_verification_providers": dict(providers),
        "openserp_ready": bool(openserp_ready),
        "fallback_no_high_guard": True,
        "candidate_concurrency": concurrency,
        "search_elapsed_seconds": round(elapsed, 2),
        "search_candidates_per_second": round(completed / elapsed, 4) if elapsed > 0 else 0,
        "pass2_skipped_owned": pass2_skipped,
        "checkpoint_every": checkpoint_every,
    })
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = {
        "status": "ok",
        "records": len(rows),
        "attempted": attempted,
        "completed": completed,
        "openserp_ready": openserp_ready,
        "candidate_concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 2),
        "statuses": dict(counts),
        "outcomes": dict(outcomes),
        "providers": dict(providers),
        "strict_high_precertified": int(outcomes.get("HIGH", 0)),
        "pass2_skipped_owned": pass2_skipped,
        "fallback_no_high_guard": True,
    }
    (shard / "search_verify_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("GWS_SEARCH_VERIFY_FAST=" + json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
