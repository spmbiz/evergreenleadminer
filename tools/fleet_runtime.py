#!/usr/bin/env python3
"""Autonomous provider-neutral fleet runtime for the Overture V6 hospitality harvester.

Subcommands:
  init       create durable small state files if missing
  probe      best-effort public GitHub fleet capacity
  plan       choose unfinished/stale disjoint shards
  worker     run existing V6 bulk + live verification
  aggregate  single-writer SQLite canonicalization and GPT handoff
  upload     upload immutable asset to a GitHub Release (CircleCI inbox)
"""
from __future__ import annotations
import argparse, csv, datetime as dt, gzip, hashlib, json, os, sqlite3, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
API, UPLOAD = "https://api.github.com", "https://uploads.github.com"

DEFAULT_STATE = {
 "state/coverage.json":{"schema_version":1,"shards":{}},
 "state/checkpoints.json":{"schema_version":1,"last_cycle":None,"cycles":{}},
 "state/source_state.json":{"schema_version":1,"overture_hospitality_v6":{"recommended_local_http_workers":64,"recent_error_rate":0.0,"recent_429_rate":0.0,"recent_timeout_rate":0.0,"last_release":"2026-06-17.0","last_updated":None}},
 "state/provider_capacity.json":{"schema_version":1,"github":{"limit":20,"worker_target":18},"circleci":{"limit":30,"enabled":False,"eligibility_verified":False}},
 "metrics/latest.json":{"status":"not_run_yet"},
 "gpt/latest_summary.json":{"status":"not_run_yet","gpt_required_for_harvest":False},
 "gpt/pending_batches.json":{"schema_version":1,"batches":[]},
}

def load_json(path, default):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:return default

def write_json(path,obj):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

def init_state():
    for rel,obj in DEFAULT_STATE.items():
        p=ROOT/rel
        if not p.exists(): write_json(p,obj)
    hist=ROOT/"metrics/history.jsonl";hist.parent.mkdir(parents=True,exist_ok=True);hist.touch(exist_ok=True)

def api_get(url,token=""):
    r=urllib.request.Request(url);r.add_header("Accept","application/vnd.github+json");r.add_header("X-GitHub-Api-Version","2022-11-28")
    if token:r.add_header("Authorization",f"Bearer {token}")
    with urllib.request.urlopen(r,timeout=20) as x:return json.loads(x.read())

def probe(owner,limit,reserve,default_workers,ignore_run_id,out):
    token=os.environ.get("GITHUB_TOKEN","");active=queued=0;partial=False;errors=[];active_repos=[]
    try:
        repos=api_get(f"{API}/users/{urllib.parse.quote(owner)}/repos?per_page=100&type=owner",token)
        for repo in repos:
            if repo.get("private"):partial=True;continue
            full=repo.get("full_name")
            if not full:continue
            try:
                runs=[]
                for status in ("in_progress","queued"):
                    runs += api_get(f"{API}/repos/{full}/actions/runs?status={status}&per_page=20",token).get("workflow_runs") or []
                a=q=0
                for run in runs:
                    if str(run.get("id"))==str(ignore_run_id):continue
                    try:
                        jobs=api_get(run["jobs_url"],token).get("jobs") or []
                        a+=sum(j.get("status")=="in_progress" for j in jobs);q+=sum(j.get("status")=="queued" for j in jobs)
                    except Exception:
                        if run.get("status")=="in_progress":a+=1
                        else:q+=1
                        partial=True
                if a or q:active_repos.append({"repo":full,"active_jobs":a,"queued_jobs":q})
                active+=a;queued+=q
            except Exception as e:partial=True;errors.append(f"{full}:{type(e).__name__}")
    except Exception as e:partial=True;errors.append(type(e).__name__)
    available=max(0,min(default_workers,limit-reserve-active))
    payload={"schema_version":1,"updated_at":dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"),
      "github":{"limit":limit,"control_reserve":reserve,"active_jobs_observed":active,"queued_jobs_observed":queued,"available_worker_capacity":available,"public_owner_scan_partial":partial,"active_repositories":active_repos,"errors":errors},
      "circleci":{"limit":30,"enabled":False,"eligibility_verified":False}}
    write_json(out,payload);print(json.dumps(payload,separators=(",",":")))

def shard_key(s):
    raw="|".join(str(s.get(k,"")).strip().lower() for k in ("country","region","bbox"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

def parse_ts(v):
    if not v:return None
    try:return dt.datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except:return None

def catalog():
    seen={}
    for pattern in ("*TRIGGER.json","AMERICAS_WAVE*.json"):
        for p in sorted(ROOT.glob(pattern)):
            cfg=load_json(p,{})
            release=str(cfg.get("release") or "2026-06-17.0");max_rows=int(cfg.get("max_rows_per_shard") or 250000)
            for i,item in enumerate(cfg.get("shards") or []):
                if not isinstance(item,dict) or not item.get("bbox"):continue
                s={"name":str(item.get("name") or f"{p.stem}-{i}"),"country":str(item.get("country") or ""),"region":str(item.get("region") or item.get("name") or ""),"bbox":str(item["bbox"]),"release":release,"max_rows":max_rows,"source_file":p.name}
                s["key"]=shard_key(s);seen[s["key"]]=s
    return list(seen.values())

def plan(provider,capacity,out,node_index=None):
    init_state()
    desired=load_json(ROOT/"control/desired_state.json",{});providers=load_json(ROOT/"config/providers.json",{}).get("providers") or {}
    pcfg=providers.get(provider) or {};fleet=load_json(ROOT/"config/fleet.json",{});coverage=load_json(ROOT/"state/coverage.json",{}).get("shards") or {}
    src=load_json(ROOT/"state/source_state.json",{}).get("overture_hospitality_v6") or {}
    now=dt.datetime.now(dt.timezone.utc);cycle=now.strftime("%Y%m%dT%H%M%SZ")+"-"+provider
    local=int(src.get("recommended_local_http_workers") or fleet.get("local_http_workers",{}).get("default",64))
    enabled=bool(desired.get("enabled")) and bool(pcfg.get("enabled"))
    if not enabled:
        payload={"enabled":False,"cycle_id":cycle,"local_workers":local,"include":[]}
    else:
        stale=float(fleet.get("stale_after_hours",168));ranked=[]
        for s in catalog():
            c=coverage.get(s["key"]) or {};last=parse_ts(c.get("last_success"));changed=c.get("release")!=s.get("release")
            age=1e9 if changed or not last else max(0,(now-last).total_seconds()/3600)
            useful=changed or not last or age>=stale or c.get("status") in ("partial","failed_retryable")
            if useful: ranked.append((age-int(c.get("consecutive_failures") or 0)*72,s["key"],s))
        ranked.sort(key=lambda x:(x[0],x[1]),reverse=True)
        order=["github","circleci"];offset=0
        for n in order:
            if n==provider:break
            if (providers.get(n) or {}).get("enabled"):offset+=int((providers.get(n) or {}).get("worker_target") or 0)
        selected=[x[2] for x in ranked[offset:offset+max(0,int(capacity))]]
        for i,s in enumerate(selected):s["slot"]=offset+i;s["local_workers"]=local
        payload={"enabled":True,"cycle_id":cycle,"provider":provider,"offset":offset,"capacity":capacity,"local_workers":local,"catalog_size":len(catalog()),"useful_backlog":len(ranked),"include":selected}
    if node_index is not None:
        arr=payload.get("include") or [];payload["selected"]=arr[node_index] if 0<=node_index<len(arr) else None
    write_json(out,payload);print(json.dumps(payload,separators=(",",":")))

def run(cmd):
    p=subprocess.run(cmd,cwd=ROOT,text=True)
    if p.returncode:raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}")

def worker(a):
    out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True);t0=time.time();status="success";err=""
    try:
        run([sys.executable,"tools/overture_v6_fastlane.py","--bbox",a.bbox,"--country",a.country,"--region",a.region,"--outdir",str(out),"--release",a.release,"--max-rows",str(a.max_rows)])
        run([sys.executable,"tools/v6_live_verify.py","--input",str(out/"v6_fast_ready.csv"),"--outdir",str(out),"--workers",str(a.local_workers),"--timeout","7"])
    except Exception as e:status="failed_retryable";err=f"{type(e).__name__}: {e}"
    fast=load_json(out/"v6_fast_summary.json",{});live=load_json(out/"v6_live_summary.json",{});reasons={}
    vp=out/"v6_live_verified.csv"
    if vp.exists():
        with vp.open(encoding="utf-8",newline="") as f:
            for r in csv.DictReader(f):
                x=r.get("live_reason") or "";reasons[x]=reasons.get(x,0)+1
    checked=sum(reasons.values());rate429=reasons.get("HTTP_429",0)/checked if checked else 0
    timeouts=sum(v for k,v in reasons.items() if "TIMEOUT" in k);errors=sum(v for k,v in reasons.items() if k.startswith("NETWORK_") or k.startswith("HTTP_5"))
    summary={"provider":a.provider,"cycle_id":a.cycle_id,"shard":{"name":a.name,"country":a.country,"region":a.region,"bbox":a.bbox,"release":a.release},"status":status,"error":err,"local_workers":a.local_workers,"elapsed_seconds":round(time.time()-t0,2),"raw_site_email_rows":int(fast.get("raw_site_email_rows") or 0),"fast_ready":int(fast.get("fast_ready") or 0),"live_high":int(live.get("live_high") or 0),"live_medium":int(live.get("live_medium") or 0),"live_ready":int(live.get("live_ready") or 0),"instagram_found":int(live.get("instagram_found") or 0),"http_429_rate":round(rate429,5),"timeout_rate":round(timeouts/checked if checked else 0,5),"error_rate":round(errors/checked if checked else 0,5),"live_reasons":reasons}
    write_json(out/"worker_summary.json",summary);print(json.dumps(summary,indent=2))
    if status!="success":raise SystemExit(2)

def host(u):
    try:
        h=(urlparse(u).hostname or "").lower().strip(".");return h[4:] if h.startswith("www.") else h
    except:return ""
def root_host(h):
    parts=(h or "").lower().strip(".").split(".");return ".".join(parts[-2:]) if len(parts)>=2 else (h or "").lower()

def init_db(c):
    c.executescript("""PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS leads(domain TEXT PRIMARY KEY,name TEXT,country TEXT,region TEXT,city TEXT,state TEXT,street TEXT,website TEXT,public_email TEXT,public_phone TEXT,instagram TEXT,live_status TEXT,fit_tier TEXT,operator_score INTEGER,premium_score INTEGER,source_url TEXT,overture_id TEXT,first_seen TEXT,last_seen TEXT,source_release TEXT,raw_json TEXT);
CREATE INDEX IF NOT EXISTS idx_email ON leads(public_email);CREATE INDEX IF NOT EXISTS idx_phone ON leads(public_phone);""")

def aggregate(a):
    init_state();now=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z");root=Path(a.results_root);out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    caps=list(root.rglob("provider-capacity.json"))
    if caps:write_json(ROOT/"state/provider_capacity.json",load_json(caps[-1],{}))
    summaries=[load_json(p,{}) for p in root.rglob("worker_summary.json")];rows=[]
    for p in root.rglob("v6_live_ready.csv"):
        with p.open(encoding="utf-8",newline="") as f:rows.extend(csv.DictReader(f))
    db=Path(a.canonical_db);db.parent.mkdir(parents=True,exist_ok=True);con=sqlite3.connect(db);con.row_factory=sqlite3.Row;init_db(con)
    old={r["domain"]:dict(r) for r in con.execute("SELECT * FROM leads")};new=[];changed=[];dups=0
    for r in rows:
        d=root_host(r.get("domain") or host(r.get("website") or ""))
        if not d:continue
        r["domain"]=d;o=old.get(d)
        if o:
            dups+=1
            if any((o.get(k) or "")!=(r.get(k) or "") for k in ("website","public_email","public_phone","instagram")):changed.append(r)
        else:new.append(r)
        first=(o or {}).get("first_seen") or now
        con.execute("""INSERT INTO leads(domain,name,country,region,city,state,street,website,public_email,public_phone,instagram,live_status,fit_tier,operator_score,premium_score,source_url,overture_id,first_seen,last_seen,source_release,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(domain) DO UPDATE SET name=excluded.name,country=excluded.country,region=excluded.region,city=excluded.city,state=excluded.state,street=excluded.street,website=excluded.website,public_email=excluded.public_email,public_phone=excluded.public_phone,instagram=COALESCE(NULLIF(excluded.instagram,''),leads.instagram),live_status=excluded.live_status,fit_tier=excluded.fit_tier,operator_score=excluded.operator_score,premium_score=excluded.premium_score,source_url=excluded.source_url,overture_id=excluded.overture_id,last_seen=excluded.last_seen,source_release=excluded.source_release,raw_json=excluded.raw_json""",(d,r.get("name"),r.get("country"),r.get("region"),r.get("city"),r.get("state"),r.get("street"),r.get("website"),r.get("public_email"),r.get("public_phone"),r.get("instagram"),r.get("live_status"),r.get("fit_tier"),int(r.get("operator_score") or 0),int(r.get("premium_score") or 0),r.get("source_url"),r.get("overture_id"),first,now,r.get("release") or r.get("source_release"),json.dumps(r,ensure_ascii=False)))
        old[d]={"website":r.get("website"),"public_email":r.get("public_email"),"public_phone":r.get("public_phone"),"instagram":r.get("instagram")}
    con.commit();total=con.execute("SELECT COUNT(*) FROM leads").fetchone()[0];con.close()
    part=new+changed;pp=out/f"partition-{a.cycle_id}.jsonl.gz"
    with gzip.open(pp,"wt",encoding="utf-8") as f:
        for r in part:f.write(json.dumps(r,ensure_ascii=False)+"\n")
    review_cfg=load_json(ROOT/"config/fleet.json",{}).get("gpt_review") or {};review=[r for r in part if (review_cfg.get("include_live_medium") and r.get("live_status")=="MEDIUM") or (review_cfg.get("include_free_webmail") and r.get("email_domain_match")=="FREE_WEBMAIL")][:int(review_cfg.get("max_records_per_batch") or 500)]
    rp=out/f"gpt-review-{a.cycle_id}.jsonl";rp.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in review),encoding="utf-8")
    covdoc=load_json(ROOT/"state/coverage.json",{"schema_version":1,"shards":{}});cov=covdoc.setdefault("shards",{})
    for s in summaries:
        sh=s.get("shard") or {};k=shard_key(sh);prior=cov.get(k) or {};ok=s.get("status")=="success"
        cov[k]={"name":sh.get("name"),"country":sh.get("country"),"region":sh.get("region"),"bbox":sh.get("bbox"),"release":sh.get("release"),"status":"complete" if ok else "failed_retryable","last_attempt":now,"last_success":now if ok else prior.get("last_success"),"consecutive_failures":0 if ok else int(prior.get("consecutive_failures") or 0)+1,"last_live_ready":int(s.get("live_ready") or 0),"last_elapsed_seconds":float(s.get("elapsed_seconds") or 0)}
    write_json(ROOT/"state/coverage.json",covdoc)
    checked=sum(int(s.get("fast_ready") or 0) for s in summaries);w=lambda key:sum(float(s.get(key) or 0)*int(s.get("fast_ready") or 0) for s in summaries)/(checked or 1)
    r429,tout,er=w("http_429_rate"),w("timeout_rate"),w("error_rate");fleet=load_json(ROOT/"config/fleet.json",{});bounds=fleet.get("local_http_workers") or {};th=fleet.get("autoscale_thresholds") or {}
    srcdoc=load_json(ROOT/"state/source_state.json",{"schema_version":1});src=srcdoc.setdefault("overture_hospitality_v6",{});workers=int(src.get("recommended_local_http_workers") or bounds.get("default",64));step=int(bounds.get("step",16))
    if r429>float(th.get("429_rate_down",.02)) or tout>float(th.get("timeout_rate_down",.15)) or er>float(th.get("error_rate_down",.20)):workers=max(int(bounds.get("min",32)),workers-step)
    elif checked>=50 and er<float(th.get("healthy_error_rate_up",.08)) and r429<.005 and tout<.05:workers=min(int(bounds.get("max",96)),workers+step)
    src.update({"recommended_local_http_workers":workers,"recent_error_rate":round(er,5),"recent_429_rate":round(r429,5),"recent_timeout_rate":round(tout,5),"last_updated":now});write_json(ROOT/"state/source_state.json",srcdoc)
    raw=sum(int(s.get("raw_site_email_rows") or 0) for s in summaries);ready=sum(int(s.get("live_ready") or 0) for s in summaries);ig=sum(int(s.get("instagram_found") or 0) for s in summaries);failed=sum(s.get("status")!="success" for s in summaries)
    metrics={"cycle_id":a.cycle_id,"provider":a.provider,"finished_at":now,"workers_completed":len(summaries),"workers_failed":failed,"raw_discovered":raw,"live_ready_before_canonical_dedupe":ready,"new_unique":len(new),"changed_existing":len(changed),"duplicates":dups,"instagram_found":ig,"gpt_review_pending":len(review),"errors":failed,"peak_wall_seconds":round(max([float(s.get("elapsed_seconds") or 0) for s in summaries] or [0]),2),"new_unique_per_worker_minute":round(len(new)/max(.01,sum(float(s.get("elapsed_seconds") or 0) for s in summaries)/60),3),"canonical_total":total,"health":{"429_rate":round(r429,5),"timeout_rate":round(tout,5),"error_rate":round(er,5)},"next_local_http_workers":workers}
    write_json(ROOT/"metrics/latest.json",metrics)
    with (ROOT/"metrics/history.jsonl").open("a",encoding="utf-8") as f:f.write(json.dumps(metrics,ensure_ascii=False)+"\n")
    cp=load_json(ROOT/"state/checkpoints.json",{"schema_version":1,"last_cycle":None,"cycles":{}});cp["last_cycle"]=a.cycle_id;cp.setdefault("cycles",{})[a.cycle_id]={"finished_at":now,"provider":a.provider,"new_unique":len(new),"canonical_total":total,"partition_asset":pp.name,"review_asset":rp.name if review else None}
    for k in sorted(cp["cycles"])[:-100]:cp["cycles"].pop(k,None)
    write_json(ROOT/"state/checkpoints.json",cp)
    pending=load_json(ROOT/"gpt/pending_batches.json",{"schema_version":1,"batches":[]})
    if review:pending.setdefault("batches",[]).append({"batch":rp.name,"created_at":now,"records":len(review),"status":"pending","cycle_id":a.cycle_id});pending["batches"]=pending["batches"][-100:]
    write_json(ROOT/"gpt/pending_batches.json",pending)
    write_json(ROOT/"gpt/latest_summary.json",{"run_id":a.cycle_id,"finished_at":now,"raw_discovered":raw,"new_unique":len(new),"duplicates":dups,"qualified":ready,"enriched":ready,"instagram_found":ig,"gpt_review_pending":len(review),"errors":failed,"provider_usage":{a.provider:{"workers_completed":len(summaries)}},"coverage_delta":{"shards_attempted":len(summaries),"shards_succeeded":len(summaries)-failed},"canonical_total":total,"next_local_http_workers":workers})
    write_json(out/"aggregate_summary.json",metrics);print(json.dumps(metrics,indent=2))

def release_req(url,token,method="GET",data=None,ctype="application/json",accept="application/vnd.github+json"):
    r=urllib.request.Request(url,method=method,data=data);r.add_header("Authorization",f"Bearer {token}");r.add_header("Accept",accept);r.add_header("X-GitHub-Api-Version","2022-11-28")
    if data is not None:r.add_header("Content-Type",ctype)
    with urllib.request.urlopen(r,timeout=60) as x:return x.read()
def release_upload(repo,tag,file):
    token=os.environ.get("FLEET_GH_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:raise SystemExit("FLEET_GH_TOKEN/GH_TOKEN required")
    try:rel=json.loads(release_req(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}",token))
    except urllib.error.HTTPError as e:
        if e.code!=404:raise
        rel=json.loads(release_req(f"{API}/repos/{repo}/releases",token,"POST",json.dumps({"tag_name":tag,"name":tag,"body":"Autonomous harvester durable store.","draft":False,"prerelease":False}).encode()))
    name=Path(file).name
    for x in rel.get("assets") or []:
        if x.get("name")==name:release_req(f"{API}/repos/{repo}/releases/assets/{x['id']}",token,"DELETE")
    data=Path(file).read_bytes();url=f"{UPLOAD}/repos/{repo}/releases/{rel['id']}/assets?name={urllib.parse.quote(name)}"
    print(release_req(url,token,"POST",data,"application/octet-stream").decode())

def cli():
    ap=argparse.ArgumentParser();sp=ap.add_subparsers(dest="cmd",required=True)
    sp.add_parser("init")
    p=sp.add_parser("probe");p.add_argument("--owner",required=True);p.add_argument("--limit",type=int,default=20);p.add_argument("--reserve",type=int,default=2);p.add_argument("--default-workers",type=int,default=18);p.add_argument("--ignore-run-id",default="");p.add_argument("--out",required=True)
    p=sp.add_parser("plan");p.add_argument("--provider",choices=("github","circleci"),required=True);p.add_argument("--capacity",type=int,required=True);p.add_argument("--out",required=True);p.add_argument("--node-index",type=int)
    p=sp.add_parser("worker");p.add_argument("--provider",required=True);p.add_argument("--cycle-id",required=True);p.add_argument("--name",required=True);p.add_argument("--country",default="");p.add_argument("--region",default="");p.add_argument("--bbox",required=True);p.add_argument("--release",default="2026-06-17.0");p.add_argument("--max-rows",type=int,default=250000);p.add_argument("--local-workers",type=int,default=64);p.add_argument("--outdir",required=True)
    p=sp.add_parser("aggregate");p.add_argument("--results-root",required=True);p.add_argument("--cycle-id",required=True);p.add_argument("--provider",default="github");p.add_argument("--canonical-db",required=True);p.add_argument("--outdir",required=True)
    p=sp.add_parser("upload");p.add_argument("--repo",required=True);p.add_argument("--tag",required=True);p.add_argument("--file",required=True)
    a=ap.parse_args()
    if a.cmd=="init":init_state()
    elif a.cmd=="probe":probe(a.owner,a.limit,a.reserve,a.default_workers,a.ignore_run_id,a.out)
    elif a.cmd=="plan":plan(a.provider,a.capacity,a.out,a.node_index)
    elif a.cmd=="worker":worker(a)
    elif a.cmd=="aggregate":aggregate(a)
    elif a.cmd=="upload":release_upload(a.repo,a.tag,a.file)
if __name__=="__main__":cli()
