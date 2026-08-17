#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def load_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: str | Path, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sval(task: dict[str, Any], key: str, default: str = "") -> str:
    v = task.get(key)
    return str(default if v is None or v == "" else v)


def ival(task: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(task.get(key) if task.get(key) is not None else default)
    except Exception:
        return default


def run_task(task: dict[str, Any], cycle_id: str, canonical_domains: str, results_root: Path, total_tasks: int) -> dict[str, Any]:
    key = sval(task, "key", f"task-{abs(hash(json.dumps(task, sort_keys=True))) % 10_000_000}")
    out = results_root / key
    out.mkdir(parents=True, exist_ok=True)

    # Original design allocated a full VM per task and could afford 32-64 local
    # network threads per task. In bundled mode we cap the aggregate fan-out so
    # one 4-vCPU runner stays responsive while still keeping the network busy.
    per_task_http = max(4, min(12, 96 // max(1, total_tasks)))
    per_task_contact = max(2, min(8, 64 // max(1, total_tasks)))
    local_workers = min(max(1, ival(task, "local_workers", per_task_http)), per_task_http)
    contact_workers = min(max(1, ival(task, "contact_workers", per_task_contact)), per_task_contact)

    task_type = sval(task, "task_type")
    if task_type == "atp_spider":
        cmd = [
            sys.executable, "tools/hospitality_atp_worker.py",
            "--provider", "github",
            "--cycle-id", cycle_id,
            "--spider", sval(task, "spider"),
            "--expected-release", sval(task, "release"),
            "--canonical-domains", canonical_domains,
            "--local-workers", str(local_workers),
            "--outdir", str(out),
        ]
    elif task_type == "osm_geofabrik":
        cmd = [
            sys.executable, "tools/hospitality_osm_worker.py",
            "--provider", "github",
            "--cycle-id", cycle_id,
            "--extract-id", sval(task, "extract_id"),
            "--expected-release", sval(task, "release"),
            "--canonical-domains", canonical_domains,
            "--local-workers", str(local_workers),
            "--contact-workers", str(contact_workers),
            "--outdir", str(out),
        ]
    else:
        cmd = [
            sys.executable, "tools/hospitality_worker.py",
            "--provider", "github",
            "--cycle-id", cycle_id,
            "--name", sval(task, "name"),
            "--country", sval(task, "country"),
            "--region", sval(task, "region"),
            f"--bbox={sval(task, 'bbox')}",
            "--release", sval(task, "release"),
            "--max-rows", sval(task, "max_rows", "250000"),
            "--lane", sval(task, "lane", "fast_email"),
            "--canonical-domains", canonical_domains,
            "--local-workers", str(local_workers),
            "--contact-workers", str(contact_workers),
            "--contact-timeout", sval(task, "contact_timeout", "7"),
            "--contact-max-pages", sval(task, "contact_max_pages", "4"),
            "--contact-max-bytes", sval(task, "contact_max_bytes", "600000"),
            "--verify-engine", "thread",
            "--outdir", str(out),
        ]

    started = time.time()
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = out / "bundle-worker.log"
    log.write_text(proc.stdout or "", encoding="utf-8")
    if proc.returncode != 0 and not (out / "worker_summary.json").is_file():
        write_json(out / "worker_summary.json", {
            "provider": "github",
            "cycle_id": cycle_id,
            "status": "failed_retryable",
            "error": f"bundled worker exit {proc.returncode}",
            "task": task,
            "elapsed_seconds": round(time.time() - started, 2),
        })
    return {
        "key": key,
        "task_type": task_type or "hospitality_worker",
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 2),
        "local_workers": local_workers,
        "contact_workers": contact_workers,
    }


def harvest(args: argparse.Namespace) -> int:
    plan = load_json(args.plan, {}) or {}
    tasks = list(plan.get("include") or [])
    if not tasks:
        write_json(Path(args.results_root) / "bundle-summary.json", {"status": "idle", "tasks": 0})
        print(json.dumps({"status": "idle", "tasks": 0}))
        return 0
    results_root = Path(args.results_root)
    results_root.mkdir(parents=True, exist_ok=True)
    workers = min(max(1, int(args.parallel)), len(tasks))
    outcomes: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_task, t, str(plan.get("cycle_id") or args.cycle_id), args.canonical_domains, results_root, len(tasks)) for t in tasks]
        for fut in cf.as_completed(futs):
            try:
                result = fut.result()
            except Exception as exc:
                result = {"returncode": 99, "error": f"{type(exc).__name__}: {exc}"}
            outcomes.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    ok = sum(int(x.get("returncode") or 0) == 0 for x in outcomes)
    summary = {
        "status": "success" if ok else "failed_retryable",
        "tasks": len(tasks),
        "succeeded": ok,
        "failed": len(tasks) - ok,
        "physical_runners": 1,
        "logical_parallelism": workers,
        "outcomes": outcomes,
    }
    write_json(results_root / "bundle-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if ok else 2


def run_intel(task: dict[str, Any], planner_root: Path, results_root: Path, qwen_url: str, config: str) -> dict[str, Any]:
    key = sval(task, "key")
    recs = ival(task, "compound_intel_records", 0)
    if not key or recs <= 0:
        return {"key": key, "skipped": True, "returncode": 0}
    out = results_root / key / "compound_intelligence"
    out.mkdir(parents=True, exist_ok=True)
    input_path = planner_root / sval(task, "compound_intel_path")
    cmd = [
        sys.executable, "tools/hospitality_intelligence_worker_fast.py",
        "--input", str(input_path),
        "--config", config,
        "--qwen-url", qwen_url,
        "--shard", sval(task, "compound_intel_shard", "0"),
        "--outdir", str(out),
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=os.environ.copy())
    (out / "bundle-intelligence.log").write_text(proc.stdout or "", encoding="utf-8")
    return {"key": key, "records": recs, "returncode": proc.returncode}


def intelligence(args: argparse.Namespace) -> int:
    plan = load_json(args.plan, {}) or {}
    tasks = [t for t in (plan.get("include") or []) if ival(t, "compound_intel_records", 0) > 0]
    if not tasks:
        print(json.dumps({"status": "idle", "intelligence_tasks": 0}))
        return 0
    parallel = min(max(1, int(args.parallel)), len(tasks))
    outcomes: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = [ex.submit(run_intel, t, Path(args.planner_root), Path(args.results_root), args.qwen_url, args.config) for t in tasks]
        for fut in cf.as_completed(futs):
            try:
                r = fut.result()
            except Exception as exc:
                r = {"returncode": 99, "error": f"{type(exc).__name__}: {exc}"}
            outcomes.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
    ok = sum(int(x.get("returncode") or 0) == 0 for x in outcomes)
    print(json.dumps({"status": "success" if ok else "failed_open", "intelligence_tasks": len(tasks), "succeeded": ok, "failed": len(tasks)-ok}))
    # Intelligence is explicitly fail-open; deterministic harvest quality is not weakened.
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Execute many Hospitality logical shards inside one physical runner.")
    sub = ap.add_subparsers(dest="command", required=True)

    h = sub.add_parser("harvest")
    h.add_argument("--plan", required=True)
    h.add_argument("--cycle-id", default="")
    h.add_argument("--canonical-domains", required=True)
    h.add_argument("--results-root", required=True)
    h.add_argument("--parallel", type=int, default=20)
    h.set_defaults(func=harvest)

    i = sub.add_parser("intelligence")
    i.add_argument("--plan", required=True)
    i.add_argument("--planner-root", required=True)
    i.add_argument("--results-root", required=True)
    i.add_argument("--qwen-url", required=True)
    i.add_argument("--config", required=True)
    i.add_argument("--parallel", type=int, default=4)
    i.set_defaults(func=intelligence)

    args = ap.parse_args()
    raise SystemExit(int(args.func(args) or 0))


if __name__ == "__main__":
    main()
