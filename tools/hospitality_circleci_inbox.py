#!/usr/bin/env python3
"""Ingest immutable CircleCI hospitality bundles into the GitHub canonical writer.

CircleCI is transport/compute only. This script runs on GitHub, restores the
canonical SQLite, aggregates one or more immutable CircleCI inbox bundles,
persists canonical/history assets, and deletes inbox assets only after success.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"


def token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def req(url: str, method: str = "GET", accept: str = "application/vnd.github+json"):
    r = urllib.request.Request(url, method=method)
    r.add_header("Accept", accept)
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token():
        r.add_header("Authorization", f"Bearer {token()}")
    return r


def api_json(url: str):
    with urllib.request.urlopen(req(url), timeout=45) as x:
        return json.loads(x.read())


def download_asset(repo: str, asset_id: int, dest: Path):
    url = f"{API}/repos/{repo}/releases/assets/{asset_id}"
    with urllib.request.urlopen(req(url, accept="application/octet-stream"), timeout=120) as x:
        dest.write_bytes(x.read())


def delete_asset(repo: str, asset_id: int):
    url = f"{API}/repos/{repo}/releases/assets/{asset_id}"
    with urllib.request.urlopen(req(url, method="DELETE"), timeout=45) as x:
        x.read()


def release_assets(repo: str, tag: str):
    try:
        rel = api_json(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        return rel.get("assets") or []
    except Exception:
        return []


def safe_extract(tar_path: Path, dest: Path):
    dest_resolved = dest.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise RuntimeError(f"unsafe tar path: {member.name}")
        tf.extractall(dest)


def run(cmd: list[str]):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "walidgdg1-ai/evergreenleadminer"))
    ap.add_argument("--max-assets", type=int, default=4)
    a = ap.parse_args()
    if not token():
        raise SystemExit("GitHub token missing")

    assets = [
        x for x in release_assets(a.repo, "harvest-inbox")
        if str(x.get("name") or "").startswith("circleci-hospitality-inbox-")
        and str(x.get("name") or "").endswith(".tar.gz")
    ]
    assets.sort(key=lambda x: (x.get("created_at") or "", int(x.get("id") or 0)))
    assets = assets[: max(0, a.max_assets)]
    if not assets:
        print(json.dumps({"status": "empty", "processed_assets": 0}))
        return

    now = dt.datetime.now(dt.timezone.utc)
    cycle = now.strftime("%Y%m%dT%H%M%SZ") + "-circleci-inbox"
    canonical = ROOT / "canonical" / "hospitality-canonical.sqlite"
    outdir = ROOT / "aggregate_out"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    # Restore canonical SQLite if present.
    state_assets = [x for x in release_assets(a.repo, "harvest-state") if x.get("name") == "hospitality-canonical.sqlite"]
    if state_assets:
        download_asset(a.repo, int(state_assets[-1]["id"]), canonical)

    processed = []
    with tempfile.TemporaryDirectory(prefix="hospitality-cc-inbox-") as td:
        incoming = Path(td) / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        for n, asset in enumerate(assets):
            archive = Path(td) / f"asset-{asset['id']}.tar.gz"
            target = incoming / f"asset-{n:03d}-{asset['id']}"
            target.mkdir(parents=True, exist_ok=True)
            download_asset(a.repo, int(asset["id"]), archive)
            safe_extract(archive, target)
            processed.append({"id": int(asset["id"]), "name": asset["name"], "created_at": asset.get("created_at")})

        run([
            sys.executable, "tools/hospitality_fleet_aggregate.py",
            "--results-root", str(incoming),
            "--cycle-id", cycle,
            "--provider", "circleci",
            "--canonical-db", str(canonical),
            "--outdir", str(outdir),
        ])

        # Keep fast-ready observations beyond transport lifetime.
        obs = outdir / f"observations-{cycle}.jsonl.gz"
        nobs = 0
        with gzip.open(obs, "wt", encoding="utf-8") as z:
            for p in incoming.rglob("v6_fast_ready.csv"):
                with p.open(encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        row["_transport_path"] = str(p)
                        z.write(json.dumps(row, ensure_ascii=False) + "\n")
                        nobs += 1

    bundle = outdir / f"hospitality-bundle-{cycle}.tar.gz"
    candidates = []
    for pattern in ("partition-*.jsonl.gz", "gpt-review-*.jsonl", "observations-*.jsonl.gz", "aggregate_summary.json"):
        candidates.extend(outdir.glob(pattern))
    with tarfile.open(bundle, "w:gz") as tf:
        for p in sorted(set(candidates)):
            tf.add(p, arcname=p.name)

    history_tag = "harvest-history-" + now.strftime("%Y-%m-%d")
    run([sys.executable, "tools/fleet_runtime.py", "upload", "--repo", a.repo, "--tag", "harvest-state", "--file", str(canonical)])
    run([sys.executable, "tools/fleet_runtime.py", "upload", "--repo", a.repo, "--tag", history_tag, "--file", str(bundle)])

    # Only now is it safe to remove transport assets.
    deleted = []
    for asset in processed:
        delete_asset(a.repo, asset["id"])
        deleted.append(asset["id"])

    metrics = {}
    try:
        metrics = json.loads((ROOT / "metrics/latest.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    state = {
        "schema_version": 1,
        "last_ingest": now.isoformat().replace("+00:00", "Z"),
        "cycle_id": cycle,
        "processed_assets": processed,
        "deleted_asset_ids": deleted,
        "observations_persisted": nobs,
        "latest_metrics": metrics,
    }
    state_path = ROOT / "state/hospitality_circleci_inbox.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "success", "cycle_id": cycle, "processed_assets": len(processed), "observations": nobs, "deleted": deleted}, indent=2))


if __name__ == "__main__":
    main()
