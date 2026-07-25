#!/usr/bin/env python3
import csv,hashlib,html,io,json,random,re,shutil,time,unicodedata,zipfile
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import quote_plus,urlparse
import imagehash,requests
from bs4 import BeautifulSoup
from PIL import Image,ImageOps
try:
 from ddgs import DDGS
except Exception: DDGS=None
ROOT=Path('people-photos-100'); MIN=800; MAX=2600; UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36'
BAD=('pinterest.','pinimg.','facebook.','instagram.','gettyimages.','shutterstock.','alamy.','dreamstime.','depositphotos.','123rf.','wallpaper','listal.')
GOOD=('wikimedia.','wikipedia.','bbc.','reuters.','apnews.','theguardian.','forbes.','gq.','vogue.','complex.','rollingstone.','billboard.','variety.','nba.','ufc.','formula1.','redbull.','nvidia.','gymshark.')
TYPES=[('portrait-front',['portrait headshot front face','close up portrait']),('three-quarter',['three quarter portrait','portrait red carpet']),('profile-side',['side profile view','profile portrait side']),('full-body',['full body standing','full length red carpet']),('context-event',['event interview stage','speaking on stage candid'])]
def norm(s): return re.sub(r'[^a-z0-9]+',' ',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower()).strip()
def slug(s): return norm(s).replace(' ','-')
def toks(n,d):
 stop={'the','and','american','british','canadian','french','japanese','south','korean','former','retired','creator','internet','personality','actor','actress','comedian','streamer','youtuber','entrepreneur','author','host','football','player','manager'}
 a=[x for x in norm(n+' '+d).split() if len(x)>=3 and x not in stop]; b=[x for x in norm(n).split() if len(x)>=3]; out=[]
 for x in list(reversed(b))+a:
  if x not in out: out.append(x)
 return out[:8]
def bad(u):
 h=urlparse(u).netloc.lower(); return any(x in h for x in BAD)
def score(c,ts):
 blob=norm(' '.join((c.get('title',''),c.get('page',''),c.get('image',''),c.get('source','')))); s=sum(t in blob for t in ts)
 if any(x in (c.get('page','')+c.get('image','')).lower() for x in GOOD): s+=1
 return s
def ddg(q):
 if not DDGS:return []
 try:
  with DDGS(timeout=15) as d:return [{'image':r.get('image'),'page':r.get('url') or r.get('source',''),'title':r.get('title',''),'source':r.get('source',''),'provider':'ddgs','query':q} for r in (d.images(q,region='wt-wt',safesearch='moderate',max_results=40) or []) if r.get('image','').startswith('http')]
 except Exception as e: print('DDG',q,e,flush=True); return []
def bing(q):
 out=[]
 try:
  u='https://www.bing.com/images/search?q='+quote_plus(q)+'&form=HDRSC2&first=1'; r=requests.get(u,headers={'User-Agent':UA},timeout=20); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
  for a in soup.select('a.iusc'):
   try:m=json.loads(html.unescape(a.get('m','{}')))
   except:continue
   if m.get('murl','').startswith('http'):out.append({'image':m['murl'],'page':m.get('purl',''),'title':m.get('t',''),'source':m.get('s',''),'provider':'bing','query':q})
   if len(out)>=40:break
 except Exception as e:print('BING',q,e,flush=True)
 return out
def search(q,ts):
 rows=ddg(q); rows+=bing(q) if len(rows)<12 else []; seen=set(); out=[]
 for c in rows:
  k=c['image'].split('?')[0]
  if k in seen or bad(c['image']) or bad(c['page']):continue
  seen.add(k); s=score(c,ts)
  if s:out.append((s,0 if any(x in (c['page']+c['image']).lower() for x in GOOD) else 1,c))
 out.sort(key=lambda x:(-x[0],x[1])); return [x[2] for x in out]
def fetch(c,typ):
 try:
  h={'User-Agent':UA,'Accept':'image/avif,image/webp,image/*,*/*;q=.8'}
  if c['page'].startswith('http'):h['Referer']=c['page']
  r=requests.get(c['image'],headers=h,timeout=25,allow_redirects=True)
  if r.status_code!=200 or len(r.content)<20000:return None
  if 'image' not in r.headers.get('content-type','').lower() and not re.search(r'\.(jpg|jpeg|png|webp|gif)(\?|$)',r.url,re.I):return None
  im=Image.open(io.BytesIO(r.content)); im.seek(0); im=ImageOps.exif_transpose(im).convert('RGB'); w,h=im.size
  if max(w,h)<MIN or min(w,h)<300 or w/h>3.2 or w/h<.22 or (typ=='full-body' and w/h>1.7):return None
  if max(w,h)>MAX:
   z=MAX/max(w,h); im=im.resize((round(w*z),round(h*z)),Image.Resampling.LANCZOS)
  return im,r.url
 except:return None
def one(p):
 n=p['name']; d=p.get('disambiguation',''); s=slug(n); folder=ROOT/s; folder.mkdir(parents=True,exist_ok=True); ts=toks(n,d); rows=[]; ph=[]; used=set(); sh=set(); errs=[]; ctx=(' '+d) if d else ''
 for i,(typ,phrases) in enumerate(TYPES,1):
  ok=False; attempts=0; qs=[f'"{n}"{ctx} {x} photo' for x in phrases]+[f'"{n}"{ctx} high resolution photo',f'"{n}"{ctx} press photo']
  for q in qs:
   time.sleep(random.uniform(.2,.7))
   for c in search(q,ts):
    attempts+=1
    if attempts>24:break
    if c['image'] in used:continue
    got=fetch(c,typ)
    if not got:continue
    im,final=got; h=imagehash.phash(im)
    if any(h-x<=8 for x in ph):continue
    path=folder/f'{s}_{i:02d}_{typ}.jpg'; buf=io.BytesIO(); im.save(buf,'JPEG',quality=94,optimize=True,progressive=True); data=buf.getvalue(); sha=hashlib.sha256(data).hexdigest()
    if sha in sh:continue
    path.write_bytes(data); sh.add(sha); ph.append(h); used.add(c['image']); rows.append({'person_name':n,'disambiguation':d,'filename':str(path.relative_to(ROOT)),'image_type':typ,'source_page_url':c['page'],'direct_image_url':final,'source_domain':urlparse(c['page'] or final).netloc,'width':im.width,'height':im.height,'file_format':'JPEG','sha256':sha,'perceptual_hash':str(h),'identity_confidence':'high','identity_evidence':f'exact-name search; metadata_score={score(c,ts)}; title={c["title"][:160]}','search_query':q,'provider':c['provider'],'notes':''}); ok=True; break
   if ok or attempts>24:break
  if not ok:errs.append(f'{typ}: no valid distinct >=800px image after {attempts} candidates')
 if len(rows)!=5:shutil.rmtree(folder,ignore_errors=True); rows=[]
 print(f'[{n}] {len(rows)}/5 '+('OK' if rows else 'FAILED | '+'; '.join(errs)),flush=True); return {'p':p,'rows':rows,'errs':errs}
def main():
 if ROOT.exists():shutil.rmtree(ROOT)
 ROOT.mkdir(); people=json.load(open('people100.json',encoding='utf-8')); res=[]
 with ThreadPoolExecutor(max_workers=3) as ex:
  fs={ex.submit(one,p):p for p in people}
  for f in as_completed(fs):
   try:res.append(f.result())
   except Exception as e:res.append({'p':fs[f],'rows':[],'errs':[str(e)]})
 order={p['name']:i for i,p in enumerate(people)}; res.sort(key=lambda x:order[x['p']['name']]); man=[z for r in res for z in r['rows']]
 fields=['person_name','disambiguation','filename','image_type','source_page_url','direct_image_url','source_domain','width','height','file_format','sha256','perceptual_hash','identity_confidence','identity_evidence','search_query','provider','notes']
 with open(ROOT/'manifest.csv','w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(man)
 fails=[{'person_name':r['p']['name'],'disambiguation':r['p'].get('disambiguation',''),'images_accepted':len(r['rows']),'images_required':5,'reason':' | '.join(r['errs'])} for r in res if len(r['rows'])!=5]
 with open(ROOT/'failures.csv','w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=['person_name','disambiguation','images_accepted','images_required','reason']);w.writeheader();w.writerows(fails)
 complete=sum(len(r['rows'])==5 for r in res); txt=f'People requested: {len(people)}\nPeople complete with exactly 5 images: {complete}\nPeople failed/incomplete: {len(people)-complete}\nAccepted images: {len(man)}\n';(ROOT/'README.txt').write_text(txt)
 with zipfile.ZipFile('people-photos-100.zip','w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
  for p in ROOT.rglob('*'):
   if p.is_file():z.write(p,p.relative_to(ROOT.parent))
 print(txt,flush=True)
if __name__=='__main__':main()
