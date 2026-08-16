#!/usr/bin/env python3
"""Build a premium/high-end outreach shortlist from Hospitality Intelligence V2.

The canonical database deliberately remains recall-first. This gate is downstream:
- recover V1 cheap-screen/live-verifier signals from the V2 plan shards;
- combine them with V2 portfolio, contactability and semantic classification;
- assign S/A/B/C/REJECT commercial tiers;
- expose only contactable S/A and strong B accounts as outreach-ready.

Two valid commercial paths are intentionally supported:
1) premium multi-property operators / managers with leverage;
2) clearly premium standalone hotels, resorts and visually strong properties.

Nothing here deletes or rewrites canonical rows. Missing evidence stays missing.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any, Iterable

PREMIUM_TERMS = (
    "luxury", "luxurious", "boutique", "villa", "villas", "chalet", "resort",
    "retreat", "estate", "residence", "residences", "penthouse", "beachfront",
    "oceanfront", "waterfront", "private pool", "infinity pool", "private beach",
    "ski-in", "ski in", "design hotel", "designer hotel", "exclusive", "five-star",
    "five star", "5-star", "5 star", "spa resort", "wellness hotel",
)
CONTACT_FIELDS = ("public_email", "public_phone", "instagram", "facebook", "whatsapp", "contact_page")
GOOD_FIT = {"STRONG_FIT", "FIT"}
V1_LIVE = {"HIGH", "MEDIUM", "PERMISSIVE"}
V1_TIERS = {"A", "B"}
PREMIUM_PROPERTY_TYPES = {
    "BOUTIQUE_HOTEL", "RESORT", "SERVICED_ACCOMMODATION", "SHORT_STAY_OPERATOR", "OTHER_HOSPITALITY"
}


def truthy(v: Any) -> bool:
    return bool(str(v or "").strip())


def as_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v or 0))
    except Exception:
        return default


def as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return default


def load_config(path: str) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return cfg.get("outreach_gate") or {}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    mode = "rt" if path.suffix == ".gz" else "r"
    with opener(path, mode, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_plan_records(plan_root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(plan_root.rglob("shard-*.jsonl")):
        for rec in iter_jsonl(p):
            aid = str(rec.get("account_id") or "").strip()
            if aid:
                out[aid] = rec
    return out


def load_v2_accounts(results_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(results_root.rglob("accounts.jsonl.gz")):
        rows.extend(iter_jsonl(p))
    return rows


def premium_hits(v2: dict[str, Any], v1: dict[str, Any]) -> list[str]:
    raw = v1.get("raw") or {}
    parts: list[str] = [
        str(v1.get("name") or v2.get("name") or ""),
        str(raw.get("brand") or ""),
        str(raw.get("category") or raw.get("categories") or ""),
        str(v2.get("portfolio_url") or ""),
        str(v2.get("portfolio_url_first_party") or ""),
        str(v2.get("classification_reason") or ""),
        " ".join(str(x) for x in (v2.get("matching_evidence") or [])),
        " ".join(str(x) for x in (v2.get("sample_property_urls") or [])),
    ]
    text = " ".join(parts).lower()
    found = []
    for term in PREMIUM_TERMS:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text):
            found.append(term)
    return found


def contact_routes(v2: dict[str, Any]) -> list[str]:
    return [k for k in CONTACT_FIELDS if truthy(v2.get(k))]


def evaluate(v2: dict[str, Any], v1: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    fit = str(v2.get("fit_decision") or "MAYBE").upper()
    entity = str(v2.get("entity_match") or "UNCERTAIN").upper()
    business = str(v2.get("business_type") or "UNCERTAIN").upper()
    confidence = as_float(v2.get("confidence"))
    commercial = as_int(v2.get("commercial_score"))
    contactability = as_int(v2.get("contactability_score"))
    leverage = as_int(v2.get("portfolio_leverage_score"))
    properties = as_int(v2.get("property_count_known"))
    operator = as_int(v1.get("operator_score"))
    premium = as_int(v1.get("premium_score"))
    v1_fit = str(v1.get("fit_tier") or "").upper()
    live = str(v1.get("live_status") or (v1.get("raw") or {}).get("live_status") or "").upper()
    routes = contact_routes(v2)
    hits = premium_hits(v2, v1)

    min_conf = as_float(cfg.get("min_confidence"), 0.70)
    premium_min = as_int(cfg.get("premium_signal_score"), 24)
    strong_premium_min = as_int(cfg.get("strong_premium_score"), 42)
    portfolio_min = as_int(cfg.get("portfolio_signal_count"), 3)
    strong_portfolio_min = as_int(cfg.get("strong_portfolio_count"), 10)
    b_contact_min = as_int(cfg.get("good_b_contactability_min"), 48)
    premium_property_score_min = as_int(cfg.get("premium_property_score_min"), 60)
    premium_property_commercial_min = as_int(cfg.get("premium_property_commercial_min"), 58)

    v1_permissive = live in V1_LIVE and v1_fit in V1_TIERS
    premium_signal = premium >= premium_min or bool(hits)
    strong_premium = premium >= strong_premium_min or len(hits) >= 2
    portfolio_signal = properties >= portfolio_min or leverage >= 55 or operator >= 60
    strong_portfolio = properties >= strong_portfolio_min or leverage >= 75 or operator >= 80
    premium_property_path = (
        fit in GOOD_FIT
        and confidence >= max(min_conf, 0.75)
        and live in V1_LIVE
        and business in PREMIUM_PROPERTY_TYPES
        and premium >= premium_property_score_min
        and strong_premium
        and contactability >= b_contact_min
    )
    hard_reject = fit == "REJECT_OBVIOUS" or entity == "WRONG" or live == "REJECT"
    classifier_uncertain = truthy(v2.get("classifier_error")) or confidence < min_conf

    reasons: list[str] = []
    if hard_reject:
        tier = "REJECT"
        reasons.append("clear_negative_semantic_or_live_evidence")
    elif classifier_uncertain:
        tier = "C"
        reasons.append("semantic_evidence_needs_review")
    elif fit in GOOD_FIT and commercial >= 85 and strong_premium and strong_portfolio:
        tier = "S"
        reasons.append("strong_fit_premium_and_portfolio")
    elif premium_property_path and commercial >= premium_property_commercial_min:
        tier = "A"
        reasons.append("premium_standalone_property_path")
    elif fit in GOOD_FIT and commercial >= 72 and premium_signal and portfolio_signal:
        tier = "A"
        reasons.append("fit_plus_premium_plus_portfolio")
    elif (
        fit in GOOD_FIT and commercial >= 62 and v1_permissive and premium_signal
        and (portfolio_signal or live == "HIGH")
    ):
        tier = "B"
        reasons.append("good_permissive_v1_plus_v2_fit")
    else:
        tier = "C"
        if not premium_signal:
            reasons.append("insufficient_premium_evidence")
        if not portfolio_signal and not premium_property_path:
            reasons.append("insufficient_portfolio_or_premium_property_evidence")
        if fit not in GOOD_FIT:
            reasons.append("semantic_fit_not_confirmed")
        if commercial < min(62, premium_property_commercial_min):
            reasons.append("commercial_score_below_outreach_floor")

    ready = False
    if tier in {"S", "A"} and routes:
        ready = True
    elif tier == "B" and routes:
        good_b = contactability >= b_contact_min and (
            properties >= 5 or strong_premium or commercial >= 70 or leverage >= 65
        )
        ready = good_b
        if not good_b:
            reasons.append("b_tier_not_strong_enough_for_outreach")
    elif tier in {"S", "A", "B"} and not routes:
        reasons.append("qualified_but_no_public_contact_route")

    rank = {"S": 4, "A": 3, "B": 2, "C": 1, "REJECT": 0}[tier]
    out = dict(v2)
    out.update({
        "commercial_tier": tier,
        "outreach_ready": ready,
        "outreach_rank": rank,
        "outreach_reasons": reasons,
        "public_contact_routes": routes,
        "v1_live_status": live,
        "v1_fit_tier": v1_fit,
        "v1_operator_score": operator,
        "v1_premium_score": premium,
        "v1_permissive_pass": v1_permissive,
        "premium_signal": premium_signal,
        "premium_evidence_hits": hits[:12],
        "portfolio_signal": portfolio_signal,
        "premium_property_path": premium_property_path,
    })
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]], gzip_output: bool = False) -> None:
    if gzip_output:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--plan-root", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--outdir", required=True)
    a = ap.parse_args()

    cfg = load_config(a.config)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    v1 = load_plan_records(Path(a.plan_root))
    v2 = load_v2_accounts(Path(a.results_root))

    scored = [evaluate(r, v1.get(str(r.get("account_id") or ""), {}), cfg) for r in v2]
    scored.sort(key=lambda r: (
        -as_int(r.get("outreach_rank")), -as_int(r.get("commercial_score")),
        -as_int(r.get("property_count_known")), str(r.get("domain") or ""),
    ))
    ready = [r for r in scored if bool(r.get("outreach_ready"))]
    followup = [r for r in scored if not r.get("outreach_ready") and r.get("commercial_tier") != "REJECT"]
    rejected = [r for r in scored if r.get("commercial_tier") == "REJECT"]

    write_jsonl(outdir / "outreach-tiered.jsonl.gz", scored, gzip_output=True)
    write_jsonl(outdir / "outreach-ready.jsonl", ready)
    write_jsonl(outdir / "outreach-followup.jsonl", followup)
    write_jsonl(outdir / "outreach-rejected.jsonl", rejected)

    tier_counts = {t: sum(1 for r in scored if r.get("commercial_tier") == t) for t in ("S", "A", "B", "C", "REJECT")}
    summary = {
        "accounts_scored": len(scored),
        "plan_records_matched": sum(1 for r in scored if str(r.get("account_id") or "") in v1),
        "tiers": tier_counts,
        "outreach_ready": len(ready),
        "outreach_ready_s": sum(1 for r in ready if r.get("commercial_tier") == "S"),
        "outreach_ready_a": sum(1 for r in ready if r.get("commercial_tier") == "A"),
        "outreach_ready_b": sum(1 for r in ready if r.get("commercial_tier") == "B"),
        "premium_property_path": sum(1 for r in scored if r.get("premium_property_path")),
        "qualified_missing_contact": sum(
            1 for r in scored
            if r.get("commercial_tier") in {"S", "A", "B"} and not r.get("public_contact_routes")
        ),
        "rule": "V1 permissive evidence + V2 semantic/portfolio scoring; operator and premium-property paths; canonical untouched; outreach exposes S/A + strong B only.",
    }
    (outdir / "outreach-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
