#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import gws_home_openserp_worker_v55 as home
import gws_legacy_deep_v2 as v2
import gws_ownership_gate as own
from gws_search_fabric import SearchFabric, query_specs

DOMAIN_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)")


def _load(path: str, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _candidate_from_record(rec: dict[str, Any], include_overture_phone: bool) -> dict[str, Any]:
    src = rec.get("source") or {}
    hub_phone = str(src.get("hub_phone") or "").strip()
    overture_phone = str(src.get("overture_phone") or "").strip()
    return {
        "n": str(rec.get("name") or src.get("hub_name") or "").strip(),
        "p": str(rec.get("postcode") or src.get("hub_postalcode") or "").strip(),
        "a": str(rec.get("address") or src.get("hub_address") or "").strip(),
        "ph": hub_phone or (overture_phone if include_overture_phone else ""),
        "em": str(src.get("hub_email") or "").strip(),
        "alias": str(rec.get("overture_name") or src.get("overture_name") or "").strip(),
    }


def _host_class(url: str) -> str:
    if not url:
        return "EMPTY"
    if own.is_third_party(url) or v2.platform(url):
        return "KNOWN_THIRD_PARTY"
    return "UNKNOWN"


def _domain_seeds(url: str, title: str, snippet: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    direct = str(url or "").strip()
    if direct:
        out.append((direct, "serp_result"))
    text = " ".join((str(title or ""), str(snippet or "")))
    for host in DOMAIN_RE.findall(text):
        host = host.strip(". ,;:()[]{}<>'\"").lower()
        if host:
            out.append(("https://" + host, "serp_snippet_domain"))
    return out


class TargetedSearchEnricher:
    """Build a bounded current-web candidate set before Qwen sees an ambiguous case.

    Search clues may use Overture phone for recall. Ownership proof never does: direct
    probes use only Hub phone/name/address evidence, preserving the strict anti-circular
    rule used by the main verifier.
    """

    def __init__(self, config_path: str = "config/gws_search_verify.json"):
        cfg = _load(config_path, {})
        search_cfg = dict(cfg.get("search") or {})
        search_cfg["max_results"] = min(8, max(4, int(search_cfg.get("max_results") or 8)))
        search_cfg["max_queries_per_candidate"] = min(5, max(3, int(search_cfg.get("max_queries_per_candidate") or 5)))
        self.max_queries = int(search_cfg["max_queries_per_candidate"])
        self.fabric = SearchFabric(search_cfg)

    def enrich(self, rec: dict[str, Any], max_candidates: int = 20, max_probes: int = 10) -> dict[str, Any]:
        row = deepcopy(rec)
        src = row.get("source") or {}
        search_c = _candidate_from_record(row, include_overture_phone=True)
        proof_c = _candidate_from_record(row, include_overture_phone=False)
        if not search_c.get("n"):
            row["targeted_search"] = {"status": "SKIPPED_NO_NAME", "candidate_set": []}
            row["candidate_set"] = []
            return row

        events: list[dict[str, Any]] = []
        by_host: dict[str, dict[str, Any]] = {}
        searched = 0

        # Carry forward candidates already surfaced by the strict verifier so Qwen
        # compares them against newly discovered exact-identity candidates.
        carry: list[str] = []
        for key in ("candidate_url",):
            u = str(row.get(key) or "").strip()
            if u:
                carry.append(u)
        for u in row.get("search_candidates") or []:
            if str(u or "").strip():
                carry.append(str(u).strip())
        for u in carry:
            h = v2.host(u)
            if h and h not in by_host:
                by_host[h] = {
                    "url": u,
                    "host": h,
                    "host_class": _host_class(u),
                    "source": "strict_carry_forward",
                    "query_family": "",
                    "query": "",
                    "provider": "existing",
                    "rank": 0,
                    "title": "",
                    "snippet": "",
                }

        for family, query in query_specs(search_c, max_queries=self.max_queries):
            searched += 1
            results, evs = self.fabric.search(family, query)
            events.extend(evs)
            for item in results:
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "")
                snippet = str(item.get("snippet") or "")
                for seed, seed_source in _domain_seeds(url, title, snippet):
                    h = v2.host(seed)
                    if not h:
                        continue
                    cur = by_host.get(h)
                    candidate = {
                        "url": seed,
                        "host": h,
                        "host_class": _host_class(seed),
                        "source": seed_source,
                        "query_family": family,
                        "query": query,
                        "provider": str(item.get("provider") or ""),
                        "rank": int(item.get("rank") or 0),
                        "title": title[:240],
                        "snippet": snippet[:420],
                    }
                    # Prefer direct SERP evidence over snippet-only or carry-forward.
                    if cur is None or (candidate["source"] == "serp_result" and cur.get("source") != "serp_result"):
                        by_host[h] = candidate

        probes = 0
        confirmed: list[dict[str, Any]] = []
        candidates = list(by_host.values())
        candidates.sort(key=lambda x: (0 if x.get("host_class") == "UNKNOWN" else 1, int(x.get("rank") or 999), x.get("host") or ""))

        for item in candidates:
            if probes >= max_probes:
                break
            if item.get("host_class") != "UNKNOWN":
                continue
            probe_item = {
                "url": item["url"],
                "host": item["host"],
                "title": item.get("title") or "",
                "description": item.get("snippet") or "",
            }
            plausible, hint = home.plausible(proof_c, probe_item)
            item["plausible"] = bool(plausible)
            item["plausibility_hint"] = hint
            if not plausible:
                continue
            probes += 1
            ev = home.probe_host(proof_c, item["url"])
            item["probe"] = {
                "ok": bool(ev.get("ok")),
                "status": ev.get("status"),
                "dns_negative": bool(ev.get("dns_negative")),
                "matched": bool(ev.get("matched")),
                "final": ev.get("final") or "",
                "error": ev.get("error") or "",
                "identity": ev.get("identity") or {},
            }
            if ev.get("matched"):
                p = {
                    "owned": str(ev.get("final") or item["url"]),
                    "owned_identity": ev.get("identity") or {},
                    "owned_via": "semantic_targeted_search",
                }
                assessment = own.assess(src, p)
                item["ownership_assessment"] = assessment
                if assessment.get("confident"):
                    confirmed.append({"url": p["owned"], "assessment": assessment})

        # Re-rank after probing: confirmed first-party candidates first, then matched
        # but not ownership-confirmed, then other unknown domains, then directories.
        def score(x: dict[str, Any]) -> tuple[int, int, int, str]:
            ass = x.get("ownership_assessment") or {}
            probe = x.get("probe") or {}
            if ass.get("confident"):
                bucket = 0
            elif probe.get("matched"):
                bucket = 1
            elif x.get("host_class") == "UNKNOWN":
                bucket = 2
            else:
                bucket = 3
            return (bucket, 0 if x.get("source") == "serp_result" else 1, int(x.get("rank") or 999), str(x.get("host") or ""))

        candidates.sort(key=score)
        candidate_set = candidates[:max_candidates]
        row["candidate_set"] = candidate_set
        row["targeted_search"] = {
            "status": "OK" if searched else "NO_QUERIES",
            "queries_attempted": searched,
            "openserp_ready": bool(self.fabric.openserp_healthy()),
            "events": events[:20],
            "candidates_discovered": len(candidates),
            "candidates_returned": len(candidate_set),
            "direct_probes": probes,
            "first_party_confirmed": confirmed[:4],
            "overture_phone_used_as_search_clue_only": bool((src.get("overture_phone") or "") and not (src.get("hub_phone") or "")),
        }
        return row
