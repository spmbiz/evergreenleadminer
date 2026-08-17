#!/usr/bin/env python3
"""Recover historical GWS false REJECTs after an ownership-gate correction.

This tool is deliberately recall-safe and non-promoting:
- it scans durable autonomous observations/changes;
- it only considers terminal REJECTs whose reason is owned-site related;
- it re-evaluates the old ownership evidence with the *current* ownership gate;
- if ownership is no longer confidently first-party, it creates exactly one
  derived remediation fingerprint and queues a fresh strict search verification;
- it never emits HIGH and never edits canonical stores.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import gws_ownership_gate as ownership

REMEDIATION_VERSION = "gws-ownership-recall-v1"


def load_json(path: str | Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def iter_jsonl(paths):
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield path, json.loads(line)
        except Exception:
            continue


def ownership_pass_for(row: dict[str, Any]) -> dict[str, Any]:
    reason = str(row.get("reason") or "").upper()
    candidates = []
    if "PASS2" in reason:
        candidates.extend([row.get("web_pass2") or {}, row.get("web_pass1") or {}])
    else:
        candidates.extend([row.get("web_pass1") or {}, row.get("web_pass2") or {}])
    owned = str(row.get("owned_website") or "")
    for p in candidates:
        if p.get("owned"):
            return p
    return {"owned": owned, "owned_identity": {}, "owned_via": "historical_owned_website"}


def is_owned_site_reject(row: dict[str, Any]) -> bool:
    if str(row.get("outcome") or "").upper() != "REJECT":
        return False
    reason = str(row.get("reason") or "").upper()
    return "OWNED_SITE" in reason or "OWNED WEBSITE" in reason


def newest_rows() -> dict[str, dict[str, Any]]:
    paths = sorted(Path("data/gws/observations").glob("*.jsonl")) + sorted(Path("data/gws/changes").glob("*.jsonl"))
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for _, row in iter_jsonl(paths):
        key = str(row.get("record_key") or row.get("hub_objectid") or "")
        if not key:
            continue
        ts = str(row.get("observed_at") or "")
        prev = latest.get(key)
        if prev is None or ts >= prev[0]:
            latest[key] = (ts, row)
    return {k: v[1] for k, v in latest.items()}


def remediation_fingerprint(row: dict[str, Any]) -> str:
    original = str(row.get("fingerprint") or row.get("source_fingerprint") or "")
    basis = original or json.dumps(
        {
            "record_key": row.get("record_key"),
            "hub_name": row.get("hub_name"),
            "hub_address": row.get("hub_address"),
            "overture_id": row.get("overture_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256((basis + "|" + REMEDIATION_VERSION).encode("utf-8")).hexdigest()


def build_recovery_row(row: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    old = {
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
        "owned_website": row.get("owned_website"),
        "verification_status": row.get("verification_status"),
        "verification_provider": row.get("verification_provider"),
        "ownership_assessment_current": assessment,
    }
    r["original_fingerprint"] = str(row.get("fingerprint") or row.get("source_fingerprint") or "")
    r["fingerprint"] = remediation_fingerprint(row)
    r["outcome"] = "REVIEW"
    r["reason"] = "OWNERSHIP_GATE_RECALL_REMEDIATION"
    r["verification_status"] = "PENDING_SEARCH_VERIFY"
    r["needs_gpt_review"] = True
    r["owned_website"] = ""
    r["certificate"] = {"verified": False, "reason": "REVERIFY_AFTER_OWNERSHIP_GATE_CHANGE"}
    r["certificate_digest"] = ""
    r["reverification_reason"] = REMEDIATION_VERSION
    r["historical_reject_evidence"] = old
    r["remediation_enqueued_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    return r


def existing_remediation_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for _, row in iter_jsonl(sorted(Path("gpt/gws_review").glob("*.jsonl"))):
        key = str(row.get("record_key") or "")
        fp = str(row.get("fingerprint") or "")
        if key and fp:
            pairs.add((key, fp))
    idx = load_json("state/gws_verify_index.json", {"records": {}}).get("records", {})
    for key, state in idx.items():
        fp = str((state or {}).get("source_fingerprint") or "")
        if key and fp:
            pairs.add((str(key), fp))
    return pairs


def scan() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    latest = newest_rows()
    existing = existing_remediation_pairs()
    suspects: list[dict[str, Any]] = []
    stats = {
        "version": REMEDIATION_VERSION,
        "latest_entities_scanned": len(latest),
        "owned_site_rejects_seen": 0,
        "still_confident_first_party": 0,
        "suspect_false_rejects": 0,
        "already_remediated": 0,
        "third_party": 0,
        "unbranded": 0,
        "weak_identity": 0,
    }
    for key, row in latest.items():
        if not is_owned_site_reject(row):
            continue
        stats["owned_site_rejects_seen"] += 1
        p = ownership_pass_for(row)
        assessment = ownership.assess(row, p)
        if assessment.get("confident"):
            stats["still_confident_first_party"] += 1
            continue
        if assessment.get("third_party"):
            stats["third_party"] += 1
        elif not assessment.get("branded_host"):
            stats["unbranded"] += 1
        else:
            stats["weak_identity"] += 1
        recovery = build_recovery_row(row, assessment)
        pair = (key, str(recovery.get("fingerprint") or ""))
        if pair in existing:
            stats["already_remediated"] += 1
            continue
        suspects.append(recovery)
    stats["suspect_false_rejects"] = len(suspects)
    return suspects, stats


def _fresh_batch_path(batch_id: str) -> Path:
    base = Path("gpt/gws_review") / f"ownership_recall_{batch_id}.jsonl"
    if not base.exists():
        return base
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = Path("gpt/gws_review") / f"ownership_recall_{batch_id}_{stamp}.jsonl"
    n = 2
    while candidate.exists():
        candidate = Path("gpt/gws_review") / f"ownership_recall_{batch_id}_{stamp}_{n}.jsonl"
        n += 1
    return candidate


def apply(rows: list[dict[str, Any]], batch_id: str) -> str | None:
    if not rows:
        return None
    batch_id = "".join(ch for ch in str(batch_id or "manual") if ch.isalnum() or ch in "-_")[:80] or "manual"
    path = _fresh_batch_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")

    pending = load_json("gpt/gws_pending_batches.json", {"schema_version": 1, "batches": [], "pending_records": 0})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    pending.setdefault("batches", []).append(
        {
            "batch": str(path),
            "created_at": now,
            "fleet_run_id": f"ownership-recall-{batch_id}",
            "provider": "ownership_recovery",
            "records": len(rows),
            "reviewed_at": None,
            "status": "pending",
            "verification_remaining": len(rows),
            "verification_total": len(rows),
        }
    )
    pending["pending_records"] = int(pending.get("pending_records") or 0) + len(rows)
    Path("gpt/gws_pending_batches.json").write_text(json.dumps(pending, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="results/gws_ownership_recovery")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch-id", default=os.getenv("GITHUB_RUN_ID") or "manual")
    args = ap.parse_args()

    rows, stats = scan()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "suspects.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    batch = apply(rows, args.batch_id) if args.apply else None
    stats["applied"] = bool(args.apply)
    stats["batch"] = batch
    (outdir / "report.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("GWS_OWNERSHIP_RECOVERY=" + json.dumps(stats, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
