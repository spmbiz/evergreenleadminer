#!/usr/bin/env python3
import io, json, math, re, shutil, subprocess, traceback
from collections import defaultdict
from pathlib import Path
import cv2, imagehash, numpy as np, requests
from PIL import Image, ImageDraw

ROOT=Path('HD_WEAK_SUPPLEMENT')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36'
S=requests.Session(); S.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})

GAMES={
 'ratchet_clank_3': {
  'name':'Ratchet & Clank 3 / Up Your Arsenal',
  'aliases':[['ratchet','clank','up','your','arsenal'],['ratchet','clank','3']],
  'queries':[
   'Ratchet and Clank Up Your Arsenal PCSX2 4K gameplay walkthrough part',
   'Ratchet and Clank 3 HD texture gameplay no commentary',
   'Ratchet Clank Up Your Arsenal 1080p gameplay combat',
   'Ratchet and Clank 3 PCSX2 gameplay full game',
   'Ratchet Up Your Arsenal gameplay mission part',
  ]},
 'dbz_budokai_tenkaichi_2_3': {
  'name':'Dragon Ball Z Budokai Tenkaichi 3',
  'aliases':[['dragon','ball','z','budokai','tenkaichi','3'],['budokai','tenkaichi','3'],['tenkaichi','3']],
  'queries':[
   'Dragon Ball Z Budokai Tenkaichi 3 PCSX2 4K gameplay match',
   'Budokai Tenkaichi 3 HD texture gameplay battle no commentary',
   'Budokai Tenkaichi 3 4K tournament gameplay',
   'Dragon Ball Z Tenkaichi 3 story mode gameplay part',
   'Budokai Tenkaichi 3 gameplay full match HD',
  ]},
 'bleach_soul_resonance': {
  'name':'Bleach Soul Resonance',
  'aliases':[['bleach','soul','resonance']],
  'queries':[
   'Bleach Soul Resonance 4K gameplay combat no commentary',
   'Bleach Soul Resonance PC gameplay open world',
   'Bleach Soul Resonance boss fight gameplay',
   'Bleach Soul Resonance chapter gameplay walkthrough',
   'Bleach Soul Resonance gameplay full fight HD',
  ]},
 'dragon_ball_fusions': {
  'name':'Dragon Ball Fusions',
  'aliases':[['dragon','ball','fusions']],
  'queries':[
   'Dragon Ball Fusions Citra 4K gameplay battle',
   'Dragon Ball Fusions HD texture gameplay walkthrough',
   'Dragon Ball Fusions open world gameplay Citra',
   'Dragon Ball Fusions gameplay part no commentary',
   'Dragon Ball Fusions battle gameplay HD emulator',
  ]},
 'inazuma_eleven_3': {
  'name':'Inazuma Eleven 3',
  'aliases':[['inazuma','eleven','3'],['inazuma','eleven','team','ogre'],['inazuma','eleven','ogre']],
  'queries':[
   'Inazuma Eleven 3 Team Ogre Attacks gameplay walkthrough part',
   'Inazuma Eleven 3 Citra HD match gameplay',
   'Inazuma Eleven 3 Les Ogres attaquent gameplay',
   'Inazuma Eleven 3 La Amenaza del Ogro gameplay',
   'Inazuma Eleven 3 Team Ogre match gameplay no commentary',
   'Inazuma Eleven 3 3DS gameplay walkthrough',
   'Inazuma Eleven 3 gameplay episode match',
  ]},
 'pokemon_diamond': {
  'name':'Pokemon Diamond',
  'aliases':[['pokemon','diamond'],['pokémon','diamond']],
  'queries':[
   'Pokemon Diamond DS gameplay walkthrough part',
   'Pokemon Diamond 4K emulator gameplay no commentary',
   'Pokemon Diamond DeSmuME HD gameplay',
   'Pokemon Diamond playthrough episode',
   'Pokemon Diamond battle gameplay HD emulator',
   'Pokemon Diamond Pearl DS walkthrough gameplay',
   'Pokémon Diamond gameplay part no commentary',
  ]},
}

BAD=['trailer','teaser','review','reaction','comparison','cutscene movie','all cutscenes','opening','ending','ost','soundtrack','music video','shorts','speedrun','glitch','benchmark','retrospective','analysis','documentary','nsfw','nude','naked',' sex ','hentai','porn','18+','adult mod','uncensored','bikini mod','sexy mod','nude mod']
CONTEXT=['gameplay','walkthrough','playthrough','longplay','part','episode','mission','match','battle','fight','combat','free roam','open world','full game','chapter']

def norm(s): return re.sub(r'[^a-z0-9]+',' ',(s or '').lower()).strip()
def title_ok(e,cfg):
 t=norm(e.get('title')); padded=' '+t+' '; w=set(t.split())
 if any(x in padded for x in BAD): return False
 if not any(x in t for x in CONTEXT): return False
 return any(all(token in w for token in group) for group in cfg['aliases'])

def vscore(e):
 t=norm(e.get('title')); d=float(e.get('duration') or 0); s=0
 if '4k' in t or '2160p' in t:s+=15
 if '1080p' in t or 'hd' in t:s+=5
 if 'no commentary' in t:s+=10
 if 'gameplay' in t:s+=8
 if any(x in t for x in ['battle','fight','combat','match']):s+=7
 if any(x in t for x in ['walkthrough','playthrough','part','episode','mission']):s+=5
 if 180<=d<=7200:s+=10
 elif d>21600:s-=4
 return s

def search(q):
 print('SEARCH',q,flush=True)
 p=subprocess.run(['yt-dlp','--flat-playlist','--ignore-config','--dump-json',f'ytsearch20:{q}'],text=True,capture_output=True)
 if p.returncode:
  print('SEARCH_ERR',p.stderr[-1000:],flush=True); return []
 out=[]
 for line in p.stdout.splitlines():
  try:e=json.loads(line);e['_query']=q;out.append(e)
  except:pass
 return out

def get(url):
 r=S.get(url,timeout=30,allow_redirects=True);r.raise_for_status();return r.content

def decode(data):
 im=Image.open(io.BytesIO(data));im.load();return im.convert('RGB')

def crop_black(im):
 a=np.array(im);g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY);rm=g.mean(1);cm=g.mean(0)
 top=0
 while top<len(rm)//4 and rm[top]<8:top+=1
 bot=len(rm)
 while bot>len(rm)*3//4 and rm[bot-1]<8:bot-=1
 left=0
 while left<len(cm)//5 and cm[left]<8:left+=1
 right=len(cm)
 while right>len(cm)*4//5 and cm[right-1]<8:right-=1
 if bot-top>=im.height*.62 and right-left>=im.width*.65:return Image.fromarray(a[top:bot,left:right])
 return im

def metrics(im):
 a=np.array(im);g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY);h=cv2.cvtColor(a,cv2.COLOR_RGB2HSV)
 hist=cv2.calcHist([g],[0],None,[256],[0,256]).ravel();p=hist/max(hist.sum(),1);ent=float(-(p[p>0]*np.log2(p[p>0])).sum())
 return {'sharp':float(cv2.Laplacian(g,cv2.CV_64F).var()),'contrast':float(g.std()),'sat':float(h[...,1].mean()),'luma':float(g.mean()),'entropy':ent,'edge':float((cv2.Canny(g,65,150)>0).mean())}

def accept(im,m):return im.width>=960 and im.height>=500 and im.width/im.height>=1.25 and 13<m['luma']<243 and m['contrast']>=16 and m['entropy']>=4.4 and m['sharp']>=18 and m['edge']>=.008

def iscore(im,m):
 mp=im.width*im.height/1e6
 return min(mp,4)*35+min(m['sharp'],1000)*.36+m['contrast']*1.6+m['sat']*.5+m['entropy']*28+min(m['edge'],.22)*500

def harvest(slug,cfg):
 pack=ROOT/slug
 if pack.exists():shutil.rmtree(pack)
 (pack/'candidates').mkdir(parents=True)
 videos={}
 for q in cfg['queries']:
  for e in search(q):
   if not e.get('id') or not title_ok(e,cfg):continue
   if e['id'] not in videos or vscore(e)>vscore(videos[e['id']]):videos[e['id']]=e
 ranked=sorted(videos.values(),key=vscore,reverse=True)[:60]
 print('VIDEOS',slug,len(ranked),flush=True)
 records=[]
 for v in ranked:
  for pos in ('maxres1','maxres2','maxres3'):
   url=f"https://i.ytimg.com/vi/{v['id']}/{pos}.jpg"
   try:
    im=crop_black(decode(get(url)));m=metrics(im)
    if not accept(im,m):continue
    records.append({'image':im,'phash':imagehash.phash(im),'score':iscore(im,m)+vscore(v),'metrics':m,'source_url':url,'video_id':v['id'],'video_title':v.get('title'),'video_url':v.get('webpage_url') or v.get('url'),'query':v.get('_query'),'position':pos})
   except Exception as x:print('IMG_ERR',v.get('id'),pos,repr(x),flush=True)
 records.sort(key=lambda r:r['score'],reverse=True);selected=[];per=defaultdict(int)
 for r in records:
  if per[r['video_id']]>=3:continue
  if any((r['phash']-x['phash'])<7 for x in selected):continue
  selected.append(r);per[r['video_id']]+=1
  if len(selected)>=60:break
 rows=[]
 for i,r in enumerate(selected,1):
  im=r['image'].copy();path=pack/'candidates'/f'{i:02d}.jpg';im.save(path,'JPEG',quality=96,subsampling=0,optimize=True)
  row={k:v for k,v in r.items() if k not in {'image','phash','metrics'}};row.update(index=i,local_path=str(path.relative_to(ROOT)),width=im.width,height=im.height,phash=str(r['phash']),score=round(r['score'],3),metrics={k:round(v,3) for k,v in r['metrics'].items()});rows.append(row)
 meta={'slug':slug,'display':cfg['name'],'policy':'Exact whole-word title lock; explicit/off-topic blacklist; only maxres1-3 generated video stills; manual review required.','candidate_count':len(rows),'videos':[{'id':e.get('id'),'title':e.get('title'),'query':e.get('_query')} for e in ranked],'candidates':rows}
 (pack/'manifest.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
 cols=6;tw,th=320,225;head=62;sheet=Image.new('RGB',(cols*tw,head+math.ceil(max(len(rows),1)/cols)*th),'white');d=ImageDraw.Draw(sheet);d.text((12,10),f"{cfg['name']} — supplemental HD gameplay candidates ({len(rows)})",fill='black');d.text((12,32),'Exact-title maxres video stills — select pure gameplay only',fill='black')
 for j,row in enumerate(rows):
  im=Image.open(ROOT/row['local_path']).convert('RGB');im.thumbnail((304,171),Image.Resampling.LANCZOS);tile=Image.new('RGB',(tw,th),'white');tile.paste(im,((tw-im.width)//2,4+(171-im.height)//2));td=ImageDraw.Draw(tile);td.text((7,180),f"{j+1:02d} {row['width']}x{row['height']} {row['position']}",fill='black');td.text((7,198),(row.get('video_title') or '')[:43],fill='black');sheet.paste(tile,((j%cols)*tw,head+(j//cols)*th))
 sheet.save(pack/'contact_sheet.jpg',quality=94)
 print('DONE',slug,len(rows),flush=True)
 return len(rows)

def main():
 qa={};failed=[]
 for slug,cfg in GAMES.items():
  try:qa[slug]=harvest(slug,cfg)
  except Exception as x:
   traceback.print_exc();qa[slug]=0;failed.append(slug);(ROOT/slug).mkdir(parents=True,exist_ok=True);(ROOT/slug/'error.json').write_text(json.dumps({'error':repr(x),'traceback':traceback.format_exc()},indent=2),encoding='utf-8')
 qa['failed']=failed;(ROOT/'QA.json').write_text(json.dumps(qa,indent=2),encoding='utf-8');print(json.dumps(qa,indent=2))
if __name__=='__main__':main()
