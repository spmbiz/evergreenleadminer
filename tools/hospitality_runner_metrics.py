#!/usr/bin/env python3
from __future__ import annotations

"""Measure exact GitHub harvest-runner minutes for a Hospitality workflow run."""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests


def ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--new-unique", type=int, required=True)
    ap.add_argument("--out", required=True)
    a=ap.parse_args()
    token=os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"hospitality-runner-metrics/1.0"}
    if token: headers["Authorization"]=f"Bearer {token}"
    jobs=[]; page=1
    while True:
        r=requests.get(f"https://api.github.com/repos/{a.repo}/actions/runs/{a.run_id}/jobs",headers=headers,params={"per_page":100,"page":page},timeout=30)
        r.raise_for_status(); batch=r.json().get("jobs") or []; jobs.extend(batch)
        if len(batch)<100: break
        page+=1
    harvest=[j for j in jobs if str(j.get("name") or "").startswith("harvest (")]
    seconds=0.0; rows=[]
    for j in harvest:
        start,end=ts(j.get("started_at")),ts(j.get("completed_at"))
        s=max(0.0,(end-start).total_seconds()) if start and end else 0.0
        seconds+=s
        rows.append({"job_id":j.get("id"),"name":j.get("name"),"runner_seconds":round(s,3),"conclusion":j.get("conclusion")})
    minutes=seconds/60.0
    payload={
        "run_id":str(a.run_id),"harvest_runner_jobs":len(harvest),"runner_seconds_exact":round(seconds,3),
        "runner_minutes_exact":round(minutes,6),"new_unique":a.new_unique,
        "new_unique_per_runner_minute_exact":round(a.new_unique/minutes,6) if minutes else 0.0,
        "failed_harvest_jobs":sum(1 for j in harvest if j.get("conclusion")!="success"),"jobs":rows,
    }
    Path(a.out).write_text(json.dumps(payload,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in payload.items() if k!="jobs"},indent=2))

if __name__=="__main__": main()
