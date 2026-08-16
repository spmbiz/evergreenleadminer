#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    This semantic stage is deliberately bounded. It is triage, not the final strict
    certificate, so it searches a compact exact-identity set concurrently and leaves
    exhaustive proof to the strict recheck.
    """

    def __init__(
        self,
        config_path: str = "config/gws_search_verify.json",
        *,
        max_queries: int = 3,
        query_concurrency: int = 2,
        probe_concurrency: int = 3,
        candidate_budget_seconds: float = 55.0,
    ):
        cfg = _load(config_path, {})
        search_cfg = dict(cfg.get("search") or {})
        search_cfg["max_results"] = min(8, max(4, int(search_cfg.get("max_results") or 8)))
        search_cfg["max_queries_per_candidate"] = min(5, max(2, int(max_queries or 3)))
        search_cfg["stop_after_first_successful_provider"] = True
        self.search_cfg = search_cfg
        self.max_queries = int(search_cfg["max_queries_per_candidate"])
        self.query_concurrency = min(3, max(1, int(query_concurrency or 2)))
        self.probe_concurrency = min(4, max(1, int(probe_concurrency or 3)))
        self.candidate_budget_seconds = max(20.0, float(candidate_budget_seconds or 55.0))
        self.fabric = SearchFabric(search_cfg)

    def _search_one(self, family: str, query: str) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        fabric = SearchFabric(dict(self.search_cfg))
        results, events = fabric.search(family, query)
        return family, query, results, events

    def enrich(self, rec: dict[str, Any], max_candidates: int = 12, max_probes: int = 5) -> dict[str, Any]:
        started = time.monotonic()
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

        specs = query_specs(search_c, max_queries=self.max_queries)
        searched = len(specs)
        if specs:
            workers = min(self.query_concurrency, len(specs))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gws-semantic-query") as pool:
                futures = [pool.submit(self._search_one, family, query) for family, query in specs]
                for fut in as_completed(futures):
                    try:
                        family, query, results, evs = fut.result()
                    except Exception as exc:
                        events.append({"provider": "semantic_query", "status": "ERROR", "error": type(exc).__name__})
                        continue
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
                            if cur is None or (candidate["source"] == "serp_result" and cur.get("source") != "serp_result"):
                                by_host[h] = candidate

        candidates = list(by_host.values())
        candidates.sort(key=lambda x: (0 if x.get("host_class") == "UNKNOWN" else 1, int(x.get("rank") or 999), x.get("host") or ""))

        probe_targets: list[dict[str, Any]] = []
        for item in candidates:
            if len(probe_targets) >= max_probes:
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
            if plausible:
                probe_targets.append(item)

        confirmed: list[dict[str, Any]] = []
        probes = 0
        budget_exhausted = (time.monotonic() - started) >= self.candidate_budget_seconds
        if probe_targets and not budget_exhausted:
            workers = min(self.probe_concurrency, len(probe_targets))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gws-semantic-probe") as pool:
                futs = {pool.submit(home.probe_host, proof_c, item["url"]): item for item in probe_targets}
                for fut in as_completed(futs):
                    item = futs[fut]
                    probes += 1
                    try:
                        ev = fut.result()
                    except Exception as exc:
                        ev = {"ok": False, "status": None, "dns_negative": False, "matched": False, "final": "", "error": type(exc).__name__, "identity": {}}
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
        elapsed = time.monotonic() - started
        row["candidate_set"] = candidate_set
        row["targeted_search"] = {
            "status": "OK" if searched else "NO_QUERIES",
            "queries_attempted": searched,
            "query_concurrency": self.query_concurrency,
            "probe_concurrency": self.probe_concurrency,
            "openserp_ready": bool(self.fabric.openserp_healthy()),
            "events": events[:20],
            "candidates_discovered": len(candidates),
            "candidates_returned": len(candidate_set),
            "direct_probes": probes,
            "first_party_confirmed": confirmed[:4],
            "budget_exhausted_before_probes": bool(budget_exhausted),
            "elapsed_seconds": round(elapsed, 3),
            "overture_phone_used_as_search_clue_only": bool((src.get("overture_phone") or "") and not (src.get("hub_phone") or "")),
        }
        return row
