#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
REPO = os.getenv("GITHUB_REPOSITORY", "walidgdg1-ai/evergreenleadminer")
TAG = "harvest-inbox"
STATE = Path("state/gws_circleci_inbox.json")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def req(url: str, token: str, *, method: str = "GET", accept: str = "application/vnd.github+json") -> bytes:
    r = urllib.request.Request(url, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Accept", accept)
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(r, timeout=60) as x:
        return x.read()


def release(token: str) -> dict | None:
    try:
        return json.loads(req(f"{API}/repos/{REPO}/releases/tags/{urllib.parse.quote(TAG)}", token))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def download(asset: dict, token: str, dest: Path) -> None:
    data = req(asset["url"], token, accept="application/octet-stream")
    dest.write_bytes(data)


def safe_extract(archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise RuntimeError(f"unsafe archive member: {member.name}")
        tf.extractall(dest)


def aggregate_one(asset: dict, token: str) -> dict:
    asset_id = int(asset["id"])
    name = str(asset["name"])
    with tempfile.TemporaryDirectory(prefix="gws-circleci-inbox-") as td:
        td = Path(td)
        archive = td / name
        download(asset, token, archive)
        unpack = td / "unpack"
        unpack.mkdir()
        safe_extract(archive, unpack)
        results = unpack / "results"
        plan_dir = results / "fleet_plan"
        shards_root = results / "shards"
        if not (plan_dir / "plan.json").exists():
            raise RuntimeError(f"{name}: missing results/fleet_plan/plan.json")
        if not shards_root.exists():
            raise RuntimeError(f"{name}: missing results/shards")
        run_id = f"circleci-asset-{asset_id}"
        subprocess.check_call([
            sys.executable, "tools/gws_fleet_aggregate.py",
            "--provider", "circleci",
            "--plan-dir", str(plan_dir),
            "--shards-root", str(shards_root),
            "--run-id", run_id,
        ])
    req(f"{API}/repos/{REPO}/releases/assets/{asset_id}", token, method="DELETE")
    return {"asset_id": asset_id, "name": name, "status": "processed_and_deleted"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-assets", type=int, default=4)
    args = ap.parse_args()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN required")
    state = load_json(STATE, {"schema_version": 1, "processed": {}, "failed": {}})
    rel = release(token)
    if not rel:
        print(json.dumps({"status": "NO_INBOX_RELEASE", "processed": 0}))
        dump_json(STATE, state)
        return 0
    assets = [a for a in rel.get("assets", []) if str(a.get("name", "")).startswith("circleci-gws-inbox-") and str(a.get("name", "")).endswith(".tar.gz")]
    assets.sort(key=lambda a: (a.get("created_at") or "", int(a.get("id") or 0)))
    processed = []
    failures = []
    for asset in assets[: max(0, args.max_assets)]:
        aid = str(asset.get("id"))
        try:
            item = aggregate_one(asset, token)
            state.setdefault("processed", {})[aid] = item
            state.setdefault("failed", {}).pop(aid, None)
            processed.append(item)
        except Exception as exc:
            item = {"asset_id": asset.get("id"), "name": asset.get("name"), "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            state.setdefault("failed", {})[aid] = item
            failures.append(item)
    for key in sorted(state.get("processed", {}), key=lambda x: int(x))[:-200]:
        state["processed"].pop(key, None)
    dump_json(STATE, state)
    print(json.dumps({"status": "COMPLETE" if not failures else "PARTIAL", "processed": processed, "failures": failures, "remaining_visible": max(0, len(assets) - len(processed))}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
