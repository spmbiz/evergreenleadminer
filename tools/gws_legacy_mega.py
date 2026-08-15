#!/usr/bin/env python3
from __future__ import annotations
import argparse,asyncio,base64,gzip,json,os,re,time,unicodedata,urllib.parse
from collections import Counter,defaultdict
from difflib import SequenceMatcher
from pathlib import Path

REL='2026-06-17.0'; OVT=f's3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*'
BBOX=(4.20,50.75,4.55,50.95)
PCS={'1000','1020','1030','1040','1050','1060','1070','1080','1081','1082','1083','1090','1120','1130','1140','1150','1160','1170','1180','1190','1200','1210'}
PLAT=('facebook.','instagram.','linkedin.','tiktok.','youtube.','google.','g.page','maps.apple.','waze.','pagesdor.','goudengids.','bizique.','cylex.','opendi.','openingsuren.','heures.','selfcity.','treatwell.','planity.','fresha.','salonkee.','nearcut.','booking.','tripadvisor.','yelp.','companyweb.','infobel.','bottin.')
STOP={'the','de','la','le','les','du','des','et','and','a','au','aux','sa','sprl','srl','bv','nv','bruxelles','brussels','belgium','belgique','be','services','service'}
UA='GWS-Legacy-Mega-Verify/1.0'; MAXBODY=350000

def t(v): return re.sub(r'\s+',' ',str(v or '')).strip()
def n(v):
 s=unicodedata.normalize('NFKD',t(v)).encode('ascii','ignore').decode().lower(); return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',s)).strip()
def dg(v):
 d=re.sub(r'\D+','',t(v));
 if d.startswith('0032'): d=d[2:]
 if d.startswith('0') and len(d)>=9: d='32'+d[1:]
 return d
def toks(v): return {x for x in n(v).split() if len(x)>1 and x not in STOP}
def sim(a,b):
 a,b=n(a),n(b)
 if not a or not b:return 0.0
 s=SequenceMatcher(None,a,b).ratio(); A,B=toks(a),toks(b); j=len(A&B)/max(1,len(A|B)); return max(s,.62*s+.38*j)
def ov(a,b): A,B=toks(a),toks(b); return len(A&B)/max(1,len(A))
def host(u):
 try:
  h=(urllib.parse.urlparse(u if '://' in u else 'https://'+u).hostname or '').lower().strip('.'); return h[4:] if h.startswith('www.') else h
 except:return ''
def platform(u):
 h=host(u); return (not h) or any(x in h for x in PLAT)
def owned(v):
 if v is None:return ''
 if not isinstance(v,(list,tuple)):v=[v]
 for u in v:
  u=t(u)
  if u and not platform(u):return u
 return ''
def queue(p):
 raw=gzip.decompress(base64.b64decode(Path(p).read_text().strip())); return [json.loads(x) for x in raw.decode().splitlines() if x.strip()]
def dump(path,rows):
 Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).write_text(''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n' for r in rows),encoding='utf-8')
def in_scope(c): return re.sub(r'\D','',t(c.get('p')))[:4] in PCS

def load_places(threads):
 import duckdb
 w,s,e,nn=BBOX; con=duckdb.connect(); con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';"); con.execute(f'SET threads={threads}')
 q=f'''SELECT id,names.primary name,websites,phones,addresses,confidence,operating_status FROM read_parquet('{OVT}',hive_partitioning=1) WHERE bbox.xmax>={w} AND bbox.xmin<={e} AND bbox.ymax>={s} AND bbox.ymin<={nn} AND names.primary IS NOT NULL'''
 z=time.time(); cur=con.execute(q); cols=[d[0] for d in cur.description]; rows=[dict(zip(cols,x)) for x in cur.fetchall()]; return rows,round(time.time()-z,3)
def indexes(P):
 ph=defaultdict(list); ex=defaultdict(list); tk=defaultdict(list)
 for i,p in enumerate(P):
  ex[n(p['name'])].append(i)
  for x in (p.get('phones') or []):
   d=dg(x)
   if d: ph[d].append(i)
  for x in list(toks(p['name']))[:4]:tk[x].append(i)
 return ph,ex,tk
def resolve(c,P,I):
 ph,ex,tk=I; cp=dg(c.get('ph')); cn=n(c.get('n')); ids=set(ph.get(cp,[]))|set(ex.get(cn,[]))
 for x in list(toks(c.get('n')))[:4]: ids.update(tk.get(x,[])[:1200])
 best=None; pc=t(c.get('p'))[:4]
 for i in ids:
  p=P[i]; pphones={dg(x) for x in (p.get('phones') or [])}; px=bool(cp and cp in pphones); ns=sim(c.get('n'),p.get('name')); ab=json.dumps(p.get('addresses'),ensure_ascii=False,default=str); ao=ov(c.get('a'),ab); pm=bool(pc and pc in ab); sc=(1.6 if px else 0)+.8*ns+.18*ao+(.12 if pm else 0)
  if best is None or sc>best[0]:best=(sc,p,px,ns,ao,pm)
 if not best:return None,{'resolved':False}
 _,p,px,ns,ao,pm=best; ok=px or (ns>=.91 and (pm or ao>=.2)) or (ns>=.82 and pm and ao>=.25)
 return (p if ok else None),{'resolved':ok,'phone_exact':px,'name_similarity':round(ns,3),'address_overlap':round(ao,3),'postcode_match':pm,'overture_id':t(p.get('id')),'overture_name':t(p.get('name'))}

def roots(name):
 x=[w for w in n(name).split() if len(w)>2 and w not in STOP][:4]; out=[]
 for r in (''.join(x),'-'.join(x)):
  if 4<=len(r)<=40 and r not in out:out.append(r)
 return out[:2]
def guesses(c): return [r+s for r in roots(c.get('n')) for s in ('.be','.com')][:4]
def hrefs(body,base):
 out=[]
 for h in re.findall(r'''href\s*=\s*["']([^"'#]+)''',body,re.I):
  u=urllib.parse.urljoin(base,h.strip())
  if u.startswith('http') and not platform(u) and u not in out:out.append(u)
 return out[:8]
def textish(body): return n(re.sub(r'<[^>]+>',' ',body))
def identity(c,body):
 tx=textish(body); p=dg(c.get('ph')); pm=bool(p and p in dg(tx)); ns=ov(c.get('n'),tx); ao=ov(c.get('a'),tx); return {'matched':pm or ns>=.66 or (ns>=.4 and ao>=.35),'phone':pm,'name_overlap':round(ns,3),'address_overlap':round(ao,3)}

async def webcheck(rows,conc):
 import aiohttp
 sem=asyncio.Semaphore(conc); timeout=aiohttp.ClientTimeout(total=11,connect=4,sock_read=7); ans={}
 async with aiohttp.ClientSession(timeout=timeout,headers={'User-Agent':UA}) as sess:
  async def get(url):
   try:
    async with sem:
     async with sess.get(url,allow_redirects=True,ssl=False) as r:
      b=(await r.content.read(MAXBODY)).decode(errors='ignore'); return {'ok':True,'status':r.status,'url':str(r.url),'body':b}
   except Exception as e:return {'ok':False,'error':type(e).__name__}
  async def one(c):
   ev={'source_ok':False,'source_identity':{},'checked':0,'owned':''}; src=t(c.get('su'))
   links=[]
   if src:
    q=await get(src); ev['source_ok']=q['ok']
    if q['ok']:
     ev['source_identity']=identity(c,q['body']); links=hrefs(q['body'],q['url'])
   cand=links[:5]+['https://'+x for x in guesses(c)]
   seen=set()
   for u in cand:
    h=host(u)
    if not h or h in seen:continue
    seen.add(h); q=await get(u); ev['checked']+=1
    if q['ok'] and q['status']<500:
     ide=identity(c,q['body'])
     if ide['matched'] and not platform(q['url']): ev['owned']=q['url']; break
   return int(c['r']),ev
  for r,e in await asyncio.gather(*(one(c) for c in rows)): ans[r]=e
 return ans

def classify(c,p,pe,w,ovok):
 r=int(c['r'])
 if not in_scope(c):return {'r':r,'status':'REJECT','reason':'OUT_OF_SCOPE','candidate':c}
 if p:
  s=owned(p.get('websites'))
  if s:return {'r':r,'status':'REJECT','reason':'OWNED_SITE_OVERTURE','owned_site':s,'candidate':c,'place':pe}
 if w.get('owned'):return {'r':r,'status':'REJECT','reason':'OWNED_SITE_HTTP','owned_site':w['owned'],'candidate':c,'place':pe,'web':w}
 if not ovok:return {'r':r,'status':'ERROR_RETRYABLE','reason':'OVERTURE_UNAVAILABLE','candidate':c,'web':w}
 resolved=bool(pe.get('resolved')); source=bool(w.get('source_ok') and (w.get('source_identity') or {}).get('matched')); checked=int(w.get('checked') or 0)
 ready=resolved and source and checked>=2
 if resolved:return {'r':r,'status':'MEDIUM','reason':'RESOLVED_NO_OWNED_SITE_BULK_CHECKS','strict_review_ready':ready,'candidate':c,'place':pe,'web':w}
 return {'r':r,'status':'UNCERTAIN','reason':'CURRENT_IDENTITY_NOT_RESOLVED','candidate':c,'place':pe,'web':w}

def worker(a):
 rows=queue(a.queue); part=[x for i,x in enumerate(rows) if i%a.worker_count==a.worker_index]; z=time.time(); ok=True
 try:P,scan=load_places(a.threads); I=indexes(P)
 except Exception as e:P=[];I=(defaultdict(list),)*3;scan=-1;ok=False
 W=asyncio.run(webcheck(part,a.http_concurrency)); out=[]
 for c in part:
  p,pe=resolve(c,P,I) if ok and in_scope(c) else (None,{'resolved':False}); out.append(classify(c,p,pe,W.get(int(c['r']),{}),ok))
 d=Path(a.outdir);d.mkdir(parents=True,exist_ok=True);dump(d/'results.jsonl',out); S=Counter(x['status'] for x in out); summ={'worker':a.worker_index,'attempted':len(part),'statuses':dict(S),'strict_review_ready':sum(bool(x.get('strict_review_ready')) for x in out),'scan_seconds':scan,'elapsed_seconds':round(time.time()-z,2)};(d/'summary.json').write_text(json.dumps(summ,indent=2));print('MEGA_WORKER='+json.dumps(summ,separators=(',',':')))
def aggregate(a):
 root=Path(a.input_root); out=[]; sums=[]
 for p in root.rglob('results.jsonl'):
  out += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
 for p in root.rglob('summary.json'):
  try:sums.append(json.loads(p.read_text()))
  except:pass
 by={int(x['r']):x for x in out}; out=sorted(by.values(),key=lambda x:int(x['r'])); ready=[x for x in out if x.get('strict_review_ready')]; S=Counter(x['status'] for x in out); raw=('\n'.join(json.dumps(x,ensure_ascii=False,separators=(',',':')) for x in out)+'\n').encode(); enc=base64.b64encode(gzip.compress(raw,9)).decode(); d=Path(a.outdir);d.mkdir(parents=True,exist_ok=True);(d/'results.jsonl.gz.b64').write_text(enc+'\n');dump(d/'strict_review_ready.jsonl',ready); summ={'schema_version':1,'expected':a.expected,'attempted_unique':len(out),'statuses':dict(S),'strict_review_ready':len(ready),'worker_summaries':len(sums),'run_id':os.getenv('GITHUB_RUN_ID','local'),'updated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())};(d/'summary.json').write_text(json.dumps(summ,indent=2));print('MEGA_AGG='+json.dumps(summ,separators=(',',':')))
def main():
 ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True); w=sub.add_parser('worker');w.add_argument('--queue',required=True);w.add_argument('--worker-index',type=int,required=True);w.add_argument('--worker-count',type=int,default=20);w.add_argument('--threads',type=int,default=16);w.add_argument('--http-concurrency',type=int,default=64);w.add_argument('--outdir',required=True); g=sub.add_parser('aggregate');g.add_argument('--input-root',required=True);g.add_argument('--outdir',required=True);g.add_argument('--expected',type=int,default=5003);a=ap.parse_args(); worker(a) if a.cmd=='worker' else aggregate(a)
if __name__=='__main__':main()
