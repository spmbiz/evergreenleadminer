#!/usr/bin/env python3
"""Expiring work-unit leases for overlapping hospitality harvest cycles.

Plan jobs are already serialized by the global broker concurrency group. This
small Release-backed state lets a new plan skip geographic/source units that an
older harvest has selected but whose aggregate has not checkpointed coverage yet.
Canonical writes remain separately serialized.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

import fleet_runtime as fr

API = "https://api.github.com"
TAG = "hospitality-work-leases"
ASSET = "hospitality-inflight.json"


def token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("FLEET_GH_TOKEN") or ""


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(x):
    return x.isoformat().replace("+00:00", "Z")


def parse_ts(v):
    try:
        x = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def request(url: str, accept="application/vnd.github+json"):
    r = urllib.request.Request(url)
    r.add_header("Accept", accept)
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    r.add_header("User-Agent", "ai-prod-hospitality-inflight/1.0")
    if token():
        r.add_header("Authorization", f"Bearer {token()}")
    return r


def api_json(url):
    with urllib.request.urlopen(request(url), timeout=30) as x:
        return json.loads(x.read())


def load(repo: str):
    default = {"schema_version": 1, "leases": [], "updated_at": None}
    try:
        rel = api_json(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(TAG, safe='')}")
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == ASSET), None)
        if not asset:
            return default
        with urllib.request.urlopen(request(f"{API}/repos/{repo}/releases/assets/{asset['id']}", "application/octet-stream"), timeout=30) as x:
            return json.loads(x.read())
    except Exception:
        return default


def save(repo: str, doc: dict):
    doc["updated_at"] = iso(now())
    with tempfile.TemporaryDirectory(prefix="hospitality-inflight-") as td:
        p = Path(td) / ASSET
        p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        fr.release_upload(repo, TAG, str(p))


def prune(doc: dict):
    t = now()
    keep = []
    for row in doc.get("leases") or []:
        exp = parse_ts(row.get("expires_at"))
        if exp and exp > t:
            keep.append(row)
    doc["leases"] = keep
    return keep


def child_keys(item: dict):
    payload = str(item.get("batch_cells_b64") or "")
    if payload:
        try:
            cells = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            return [str(c.get("key") or "") for c in cells if isinstance(c, dict) and c.get("key")]
        except Exception:
            pass
    key = str(item.get("key") or "")
    return [key] if key else []


def snapshot(args):
    doc = load(args.repo)
    leases = prune(doc)
    keys = sorted({str(x.get("key") or "") for x in leases if x.get("key")})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("".join(k + "\n" for k in keys), encoding="utf-8")
    print(json.dumps({"active_keys": len(keys), "leases": len(leases), "out": args.out}))


def reserve(args):
    doc = load(args.repo)
    leases = prune(doc)
    prior = {str(x.get("key") or ""): x for x in leases if x.get("key")}
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    selected = []
    for item in plan.get("include") or []:
        selected.extend(child_keys(item))
    expiry = iso(now() + dt.timedelta(minutes=max(10, int(args.ttl_minutes))))
    for key in sorted(set(selected)):
        prior[key] = {
            "key": key,
            "cycle_id": args.cycle_id,
            "run_id": str(args.run_id),
            "created_at": iso(now()),
            "expires_at": expiry,
        }
    doc["leases"] = list(prior.values())
    save(args.repo, doc)
    print(json.dumps({"reserved_keys": len(set(selected)), "cycle_id": args.cycle_id, "expires_at": expiry}))


def release(args):
    doc = load(args.repo)
    leases = prune(doc)
    before = len(leases)
    doc["leases"] = [x for x in leases if str(x.get("cycle_id") or "") != str(args.cycle_id)]
    save(args.repo, doc)
    print(json.dumps({"released": before - len(doc["leases"]), "cycle_id": args.cycle_id}))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("snapshot")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p = sp.add_parser("reserve")
    p.add_argument("--repo", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--cycle-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--ttl-minutes", type=int, default=75)
    p = sp.add_parser("release")
    p.add_argument("--repo", required=True)
    p.add_argument("--cycle-id", required=True)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        snapshot(a)
    elif a.cmd == "reserve":
        reserve(a)
    else:
        release(a)


if __name__ == "__main__":
    main()
