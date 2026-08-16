#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import resource
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from gws_qwen_semantic import classify_batch
from gws_semantic_targeted_search import TargetedSearchEnricher


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def read_rows(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def benchmark_pass(rec, out):
    kind = rec.get("benchmark_kind")
    if kind == "OWNED_SITE_POSITIVE":
        return out.get("decision") in {"MATCH", "PROBABLE"} and out.get("website_state") != "NO_SITE"
    if kind == "STRICT_NO_SITE":
        return (not rec.get("candidate_url")) and out.get("decision") == "UNCERTAIN" and out.get("website_state") in {"NO_SITE", "UNCERTAIN"}
    return None


def deterministic_semantic(rec):
    candidate_set = rec.get("candidate_set") or []
    has_non_third_party = any(
        str(x.get("host_class") or "") not in {"KNOWN_THIRD_PARTY", "EDITORIAL_OR_PROFILE_PAGE"}
        for x in candidate_set
        if isinstance(x, dict)
    )
    if rec.get("candidate_host_class") == "KNOWN_THIRD_PARTY" and not has_non_third_party:
        return {
            "business_id": str(rec.get("business_id") or ""),
            "candidate_url": str(rec.get("candidate_url") or ""),
            "decision": "WRONG",
            "confidence": 1.0,
            "matching_evidence": [],
            "contradictions": ["candidate_host_class=KNOWN_THIRD_PARTY"],
            "website_state": "DIRECTORY_ONLY",
            "needs_gpt_review": False,
            "reason": "Deterministic third-party host; targeted search found no plausible first-party alternative.",
            "_deterministic_short_circuit": "KNOWN_THIRD_PARTY",
        }
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--config", default="config/gws_semantic_v1.json")
    ap.add_argument("--qwen-url", default="http://127.0.0.1:8080")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--worker-index", default="0")
    a = ap.parse_args()

    cfg = load(a.config, {})
    q = cfg.get("qwen") or {}
    runtime = cfg.get("targeted_search") or {}
    rows = read_rows(a.input)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    batch = min(4, max(1, int(q.get("batch_size") or 2)))
    model = str(q.get("model_label") or "qwen3-4b-q4_k_m")
    prompt = str(q.get("prompt_version") or "gws-semantic-v1")
    qwen_timeout = max(20.0, float(q.get("timeout_seconds") or 45))
    search_workers = min(4, max(1, int(runtime.get("record_concurrency") or 3)))
    max_queries = min(5, max(2, int(runtime.get("max_queries") or 3)))
    query_concurrency = min(3, max(1, int(runtime.get("query_concurrency") or 2)))
    probe_concurrency = min(4, max(1, int(runtime.get("probe_concurrency") or 3)))
    max_candidates = min(20, max(6, int(runtime.get("max_candidates") or 12)))
    max_probes = min(10, max(2, int(runtime.get("max_probes") or 5)))
    candidate_budget = max(20.0, float(runtime.get("candidate_budget_seconds") or 55.0))

    started = time.time()
    results = []
    counts = Counter()
    bench_total = 0
    bench_passed = 0
    qwen_attempted = 0
    live_out = set(cfg.get("selection", {}).get("live_outcomes") or [])
    live_st = set(cfg.get("selection", {}).get("live_statuses") or [])
    local = threading.local()

    def is_live(rec):
        return rec.get("source_outcome") in live_out or rec.get("source_verification_status") in live_st

    def get_enricher():
        enricher = getattr(local, "enricher", None)
        if enricher is None:
            enricher = TargetedSearchEnricher(
                max_queries=max_queries,
                query_concurrency=query_concurrency,
                probe_concurrency=probe_concurrency,
                candidate_budget_seconds=candidate_budget,
            )
            local.enricher = enricher
        return enricher

    def search_one(rec):
        if not is_live(rec):
            return rec, None
        try:
            enriched = get_enricher().enrich(rec, max_candidates=max_candidates, max_probes=max_probes)
            ts = enriched.get("targeted_search") or {}
            stats = {
                "targeted_search_records": 1,
                "targeted_candidates": int(ts.get("candidates_returned") or 0),
                "targeted_probes": int(ts.get("direct_probes") or 0),
                "targeted_first_party_confirmed": len(ts.get("first_party_confirmed") or []),
                "targeted_search_errors": 0,
            }
            return enriched, stats
        except Exception as exc:
            enriched = dict(rec)
            enriched["targeted_search"] = {
                "status": "ERROR",
                "error": f"{type(exc).__name__}:{str(exc)[:180]}",
                "candidate_set": [],
            }
            enriched.setdefault("candidate_set", [])
            return enriched, {
                "targeted_search_records": 1,
                "targeted_candidates": 0,
                "targeted_probes": 0,
                "targeted_first_party_confirmed": 0,
                "targeted_search_errors": 1,
            }

    def write_enriched_checkpoint(rec):
        with (outdir / "enriched_checkpoint.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def current_summary():
        elapsed = max(0.001, time.time() - started)
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return {
            "worker_index": str(a.worker_index),
            "records_input": len(rows),
            "records_output": len(results),
            "deterministic_short_circuit": counts.get("deterministic_short_circuit", 0),
            "qwen_input": qwen_attempted,
            "qwen_classified": counts.get("classified", 0),
            "qwen_unavailable_or_invalid": counts.get("classifier_error", 0),
            "decisions": {k: v for k, v in counts.items() if k in {"MATCH", "PROBABLE", "WRONG", "UNCERTAIN"}},
            "benchmark_total": bench_total,
            "benchmark_passed": bench_passed,
            "benchmark_agreement": round(bench_passed / max(1, bench_total), 4),
            "targeted_search_records": counts.get("targeted_search_records", 0),
            "targeted_search_errors": counts.get("targeted_search_errors", 0),
            "targeted_candidates": counts.get("targeted_candidates", 0),
            "targeted_probes": counts.get("targeted_probes", 0),
            "targeted_first_party_confirmed": counts.get("targeted_first_party_confirmed", 0),
            "hallucinated_contact_count": 0,
            "elapsed_seconds": round(elapsed, 2),
            "candidates_per_minute": round(len(results) / elapsed * 60, 3),
            "peak_rss_kb": peak,
            "model": model,
            "prompt_version": prompt,
            "checkpointed": True,
            "targeted_record_concurrency": search_workers,
            "targeted_query_concurrency": query_concurrency,
            "targeted_probe_concurrency": probe_concurrency,
        }

    def checkpoint():
        body = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True, default=str) + "\n" for x in results)
        (outdir / "records.jsonl").write_text(body, encoding="utf-8")
        (outdir / "partial.jsonl").write_text(body, encoding="utf-8")
        (outdir / "summary.json").write_text(json.dumps(current_summary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def emit(rec, sem):
        nonlocal bench_total, bench_passed
        bid = str(rec.get("business_id") or "")
        bp = benchmark_pass(rec, sem)
        if bp is not None:
            bench_total += 1
            bench_passed += int(bp)
        record = {
            "business_id": bid,
            "semantic_fingerprint": rec.get("semantic_fingerprint"),
            "source_fingerprint": rec.get("source_fingerprint"),
            "certificate_digest": rec.get("certificate_digest"),
            "territory": rec.get("territory"),
            "name": rec.get("name"),
            "address": rec.get("address"),
            "postcode": rec.get("postcode"),
            "candidate_url": rec.get("candidate_url") or "",
            "candidate_host_class": rec.get("candidate_host_class") or "",
            "candidate_set": rec.get("candidate_set") or [],
            "targeted_search": rec.get("targeted_search") or {},
            "source_outcome": rec.get("source_outcome"),
            "source_reason": rec.get("source_reason"),
            "source_verification_status": rec.get("source_verification_status"),
            "model": model,
            "prompt_version": prompt,
            "semantic": sem,
            "benchmark_kind": rec.get("benchmark_kind"),
            "benchmark_expected": rec.get("benchmark_expected"),
            "benchmark_pass": bp,
            "source": rec.get("source") or {},
        }
        results.append(record)
        if sem.get("_deterministic_short_circuit"):
            counts["deterministic_short_circuit"] += 1
        elif sem.get("_classifier_error"):
            counts["classifier_error"] += 1
        else:
            counts["classified"] += 1
        counts[str(sem.get("decision") or "UNCERTAIN")] += 1
        checkpoint()

    def classify_and_emit(chunk):
        nonlocal qwen_attempted
        if not chunk:
            return
        qwen_attempted += len(chunk)
        classified = classify_batch(chunk, a.qwen_url, model, timeout=qwen_timeout)
        by_id = {str(x.get("business_id") or ""): x for x in classified}
        for rec in chunk:
            bid = str(rec.get("business_id") or "")
            sem = by_id.get(bid) or {
                "business_id": bid,
                "candidate_url": "",
                "decision": "UNCERTAIN",
                "confidence": 0.0,
                "matching_evidence": [],
                "contradictions": [],
                "website_state": "UNCERTAIN",
                "needs_gpt_review": True,
                "reason": "Missing classifier item",
                "_classifier_error": "MISSING_ITEM",
            }
            emit(rec, sem)

    checkpoint()
    pending_model = []

    live_rows = [r for r in rows if is_live(r)]
    non_live_rows = [r for r in rows if not is_live(r)]

    with ThreadPoolExecutor(max_workers=min(search_workers, max(1, len(live_rows))), thread_name_prefix="gws-semantic-record") as pool:
        futures = [pool.submit(search_one, rec) for rec in live_rows]

        for rec in non_live_rows:
            write_enriched_checkpoint(rec)
            sem = deterministic_semantic(rec)
            if sem:
                emit(rec, sem)
            else:
                pending_model.append(rec)
                if len(pending_model) >= batch:
                    chunk, pending_model = pending_model[:batch], pending_model[batch:]
                    classify_and_emit(chunk)

        for fut in as_completed(futures):
            rec, stats = fut.result()
            if stats:
                counts.update(stats)
            write_enriched_checkpoint(rec)
            sem = deterministic_semantic(rec)
            if sem:
                emit(rec, sem)
            else:
                pending_model.append(rec)
                if len(pending_model) >= batch:
                    chunk, pending_model = pending_model[:batch], pending_model[batch:]
                    classify_and_emit(chunk)

    if pending_model:
        classify_and_emit(pending_model)

    checkpoint()
    summary = current_summary()
    print("GWS_SEMANTIC_WORKER=" + json.dumps(summary, separators=(",", ":")))


if __name__ == "__main__":
    main()
