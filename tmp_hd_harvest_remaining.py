#!/usr/bin/env python3
import io, json, math, re, shutil, subprocess, sys, traceback
from collections import defaultdict
from pathlib import Path
import cv2, imagehash, numpy as np, requests
from PIL import Image, ImageDraw

ROOT=Path('HD_REMAINING')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36'
S=requests.Session(); S.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})
TARGET=36; MINIMUM=15

GAMES={
'assassins_creed_3':dict(name="Assassin's Creed III Remastered",aliases=[['assassin','creed','iii'],['assassin','creed','3']],queries=["Assassin's Creed III Remastered 4K gameplay no commentary","Assassin's Creed 3 Remastered frontier gameplay HD","Assassin's Creed III Remastered combat gameplay 4K"],steam=911400),
'assassins_creed_unity':dict(name="Assassin's Creed Unity",aliases=[['assassin','creed','unity']],queries=["Assassin's Creed Unity Paris free roam 4K gameplay","Assassin's Creed Unity parkour gameplay no commentary","Assassin's Creed Unity combat gameplay 4K"],steam=289650),
'resident_evil_4_og':dict(name='Resident Evil 4 (2005) HD Project',aliases=[['resident','evil','4']],queries=['Resident Evil 4 2005 HD Project gameplay 4K no commentary','Resident Evil 4 original UHD gameplay village','Resident Evil 4 2005 boss fight gameplay HD'],steam=254700),
'ac_black_flag_resynced':dict(name="Assassin's Creed IV Black Flag",aliases=[['black','flag'],['assassin','creed','iv']],queries=["Assassin's Creed IV Black Flag naval gameplay 4K","Assassin's Creed Black Flag open world gameplay no commentary","Black Flag combat gameplay HD"],steam=242050),
'assassins_creed_2':dict(name="Assassin's Creed II",aliases=[['assassin','creed','ii'],['assassin','creed','2']],queries=["Assassin's Creed II Venice gameplay 4K no commentary","Assassin's Creed 2 free roam gameplay HD","Assassin's Creed II combat gameplay 4K"],steam=33230),
'assassins_creed_1':dict(name="Assassin's Creed 1",aliases=[['assassin','creed','1'],['assassin','creed','2007']],queries=["Assassin's Creed 1 2007 gameplay 4K no commentary","Assassin's Creed 2007 free roam gameplay HD","Assassin's Creed 1 combat gameplay 4K"],steam=15100),
'ratchet_clank_3':dict(name='Ratchet & Clank 3 / Up Your Arsenal',aliases=[['ratchet','clank','3'],['ratchet','clank','up','your','arsenal']],queries=['Ratchet and Clank 3 PCSX2 4K gameplay no commentary','Ratchet and Clank Up Your Arsenal HD texture gameplay','Up Your Arsenal combat gameplay 4K']),
'yakuza_0':dict(name='Yakuza 0',aliases=[['yakuza','0'],['yakuza','zero']],queries=['Yakuza 0 Kamurocho free roam 4K gameplay','Yakuza 0 combat gameplay no commentary','Yakuza 0 PC max settings gameplay'],steam=638970),
'dbz_budokai_tenkaichi_2_3':dict(name='Dragon Ball Z Budokai Tenkaichi 3',aliases=[['budokai','tenkaichi','3'],['tenkaichi','3']],queries=['Budokai Tenkaichi 3 PCSX2 4K gameplay no commentary','Dragon Ball Z Budokai Tenkaichi 3 HD texture gameplay','Budokai Tenkaichi 3 story mode combat gameplay']),
'bleach_soul_resonance':dict(name='Bleach Soul Resonance',aliases=[['bleach','soul','resonance']],queries=['Bleach Soul Resonance PC 4K gameplay no commentary','Bleach Soul Resonance open world gameplay','Bleach Soul Resonance combat gameplay HD']),
'one_piece_odyssey':dict(name='One Piece Odyssey',aliases=[['one','piece','odyssey']],queries=['One Piece Odyssey open world exploration 4K gameplay','One Piece Odyssey combat gameplay no commentary','One Piece Odyssey PC max settings gameplay'],steam=814000),
'naruto_ultimate_ninja_storm':dict(name='Naruto Ultimate Ninja Storm',aliases=[['naruto','ultimate','ninja','storm'],['naruto','storm','1']],queries=['Naruto Ultimate Ninja Storm 1 free roam gameplay 4K','Naruto Ultimate Ninja Storm combat gameplay no commentary','Naruto Storm 1 PC gameplay HD'],steam=495140),
'inazuma_eleven_3':dict(name='Inazuma Eleven 3',aliases=[['inazuma','eleven','3'],['inazuma','eleven','team','ogre']],queries=['Inazuma Eleven 3 Citra HD gameplay match','Inazuma Eleven 3 Team Ogre gameplay no commentary','Inazuma Eleven 3 4K gameplay emulator']),
'pokemon_diamond':dict(name='Pokemon Diamond',aliases=[['pokemon','diamond']],queries=['Pokemon Diamond DS 4K emulator gameplay no commentary','Pokemon Diamond HD texture gameplay','Pokemon Diamond DS battle gameplay HD']),
'dragon_ball_fusions':dict(name='Dragon Ball Fusions',aliases=[['dragon','ball','fusions']],queries=['Dragon Ball Fusions Citra 4K gameplay no commentary','Dragon Ball Fusions HD texture gameplay','Dragon Ball Fusions open world gameplay HD']),
'shenmue':dict(name='Shenmue I HD',aliases=[['shenmue','1'],['shenmue','i'],['shenmue']],queries=['Shenmue 1 HD Remaster 4K gameplay no commentary','Shenmue I PC free roam gameplay','Shenmue 1 combat gameplay HD'],steam=758330),
}

BAD={'trailer','teaser','review','reaction','comparison','cutscene','cutscenes','movie','opening','ending','ost','soundtrack','music video','shorts','speedrun','glitch','benchmark','retrospective','analysis','documentary','nsfw','nude','naked','sex','hentai','porn','18+','adult mod','uncensored','bikini mod','sexy mod','nude mod'}
GOOD={'gameplay','walkthrough','longplay','playthrough','combat','boss','fight','free','roam','exploration'}

def norm(s): return re.sub(r'[^a-z0-9]+',' ',(s or '').lower()).strip()
def words(s): return set(norm(s).split())

def title_ok(e,cfg):
 t=norm(e.get('title')); d=norm(e.get('description'))[:500]; combo=t+' '+d; w=set(t.split())
 if any(x in combo for x in BAD): return False
 if not (w & GOOD): return False
 return any(all(token in w for token in group) for group in cfg['aliases'])

def score_video(e):
 t=norm(e.get('title')); dur=float(e.get('duration') or 0); s=0
 if '4k' in t or '2160p' in t: s+=12
 if '60fps' in t or '60 fps' in t: s+=4
 if 'no commentary' in t: s+=10
 if 'gameplay' in t: s+=8
 if 'combat' in t or 'boss fight' in t: s+=6
 if 'free roam' in t or 'exploration' in t: s+=6
 if 300<=dur<=5400:s+=12
 elif 120<=dur<=10800:s+=7
 elif dur>21600:s-=5
 return s

def flat_search(q):
 print('SEARCH',q,flush=True)
 p=subprocess.run(['yt-dlp','--flat-playlist','--ignore-config','--dump-json',f'ytsearch10:{q}'],text=True,capture_output=True)
 if p.returncode: print('SEARCH_ERR',p.stderr[-800:],flush=True); return []
 out=[]
 for line in p.stdout.splitlines():
  try: e=json.loads(line); e['_query']=q; out.append(e)
  except: pass
 return out

def search(cfg):
 unique={}
 for q in cfg['queries']:
  for e in flat_search(q):
   if not title_ok(e,cfg) or not e.get('id'): continue
   if e['id'] not in unique or score_video(e)>score_video(unique[e['id']]): unique[e['id']]=e
 ranked=sorted(unique.values(),key=score_video,reverse=True)[:30]
 for e in ranked[:12]: print('VIDEO',score_video(e),e.get('id'),e.get('title'),flush=True)
 return ranked

def get(url):
 r=S.get(url,timeout=30,allow_redirects=True); r.raise_for_status(); return r.content

def decode(data): im=Image.open(io.BytesIO(data)); im.load(); return im.convert('RGB')

def crop_black(im):
 a=np.array(im); g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY); rm=g.mean(1); cm=g.mean(0)
 top=0
 while top<len(rm)//4 and rm[top]<8: top+=1
 bot=len(rm)
 while bot>len(rm)*3//4 and rm[bot-1]<8: bot-=1
 left=0
 while left<len(cm)//5 and cm[left]<8:left+=1
 right=len(cm)
 while right>len(cm)*4//5 and cm[right-1]<8:right-=1
 if bot-top>=im.height*.65 and right-left>=im.width*.70:return Image.fromarray(a[top:bot,left:right])
 return im

def measure(im):
 a=np.array(im); g=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY); h=cv2.cvtColor(a,cv2.COLOR_RGB2HSV)
 hist=cv2.calcHist([g],[0],None,[256],[0,256]).ravel(); p=hist/max(hist.sum(),1); ent=float(-(p[p>0]*np.log2(p[p>0])).sum())
 return dict(sharp=float(cv2.Laplacian(g,cv2.CV_64F).var()),contrast=float(g.std()),sat=float(h[...,1].mean()),luma=float(g.mean()),entropy=ent,edge=float((cv2.Canny(g,70,160)>0).mean()))

def accept(im,m):
 return im.width>=960 and im.height>=540 and im.width/im.height>=1.30 and 15<m['luma']<241 and m['contrast']>=19 and m['entropy']>=4.7 and m['sharp']>=25 and m['edge']>=.011

def score_image(im,m,typ):
 mp=im.width*im.height/1e6
 return (90 if typ=='steam' else 0)+min(mp,4)*35+min(m['sharp'],900)*.38+m['contrast']*1.7+m['sat']*.55+m['entropy']*30+min(m['edge'],.2)*550

def youtube_records(videos):
 out=[]
 for v in videos:
  for pos in ('maxres1','maxres2','maxres3'):
   url=f"https://i.ytimg.com/vi/{v['id']}/{pos}.jpg"
   try:
    im=crop_black(decode(get(url))); m=measure(im)
    if not accept(im,m): continue
    out.append(dict(image=im,phash=imagehash.phash(im),score=score_image(im,m,'yt')+score_video(v),metrics=m,source_type='youtube_generated_still',source_url=url,video_id=v['id'],video_title=v.get('title'),video_url=v.get('webpage_url') or v.get('url'),channel=v.get('channel') or v.get('uploader'),query=v.get('_query'),position=pos))
   except Exception as x: print('YT_ERR',v.get('id'),pos,repr(x),flush=True)
 return out

def steam_records(appid):
 out=[]
 if not appid:return out
 try:
  node=S.get(f'https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=us',timeout=40).json().get(str(appid),{})
  if not node.get('success'):return out
  data=node.get('data') or {}
  for shot in data.get('screenshots') or []:
   url=shot.get('path_full') or shot.get('path_thumbnail')
   if not url:continue
   try:
    im=crop_black(decode(get(url))); m=measure(im)
    if not accept(im,m):continue
    out.append(dict(image=im,phash=imagehash.phash(im),score=score_image(im,m,'steam'),metrics=m,source_type='steam_official',source_url=url,steam_appid=appid,steam_name=data.get('name'),position=f"steam_{shot.get('id')}"))
   except Exception as x: print('STEAM_ERR',appid,repr(x),flush=True)
 except Exception as x: print('STEAM_API_ERR',appid,repr(x),flush=True)
 return out

def choose(records):
 records=sorted(records,key=lambda r:r['score'],reverse=True); out=[]; per=defaultdict(int)
 for r in records:
  vid=r.get('video_id')
  if vid and per[vid]>=2:continue
  if any((r['phash']-q['phash'])<9 for q in out):continue
  out.append(r)
  if vid:per[vid]+=1
  if len(out)>=TARGET:break
 if len(out)<MINIMUM:
  for r in records:
   if r in out or any((r['phash']-q['phash'])<6 for q in out):continue
   out.append(r)
   if len(out)>=MINIMUM:break
 return out

def sheet(pack,name,rows):
 cols=6; tw,th=320,225; head=62
 canvas=Image.new('RGB',(cols*tw,head+math.ceil(len(rows)/cols)*th),'white'); d=ImageDraw.Draw(canvas)
 d.text((12,10),f'{name} — strict HD gameplay candidates ({len(rows)})',fill='black'); d.text((12,32),'YT generated video still / ST exact official Steam screenshot — manual gameplay review required',fill='black')
 for j,r in enumerate(rows):
  im=Image.open(ROOT/r['local_path']).convert('RGB'); im.thumbnail((304,171),Image.Resampling.LANCZOS)
  tile=Image.new('RGB',(tw,th),'white'); tile.paste(im,((tw-im.width)//2,4+(171-im.height)//2)); td=ImageDraw.Draw(tile)
  tag='ST' if r['source_type']=='steam_official' else 'YT'; src=(r.get('video_title') or r.get('steam_name') or '')[:42]
  td.text((7,180),f"{j+1:02d} {tag} {r['width']}x{r['height']} {r.get('position','')}",fill='black'); td.text((7,198),src,fill='black')
  canvas.paste(tile,((j%cols)*tw,head+(j//cols)*th))
 canvas.save(pack/'contact_sheet.jpg',quality=94)

def harvest(slug):
 cfg=GAMES[slug]; pack=ROOT/slug
 if pack.exists():shutil.rmtree(pack)
 (pack/'candidates').mkdir(parents=True)
 try:
  vids=search(cfg); recs=youtube_records(vids)+steam_records(cfg.get('steam')); selected=choose(recs)
  if len(selected)<MINIMUM:raise RuntimeError(f'only {len(selected)} acceptable unique candidates')
  rows=[]
  for i,r in enumerate(selected,1):
   im=r['image'].copy();
   if max(im.size)>2560:im.thumbnail((2560,2560),Image.Resampling.LANCZOS)
   path=pack/'candidates'/f'{i:02d}.jpg'; im.save(path,'JPEG',quality=96,subsampling=0,optimize=True)
   row={k:v for k,v in r.items() if k not in {'image','phash','metrics'}}; row.update(index=i,local_path=str(path.relative_to(ROOT)),width=im.width,height=im.height,phash=str(r['phash']),score=round(float(r['score']),3),metrics={k:round(float(v),3) for k,v in r['metrics'].items()}); rows.append(row)
  meta=dict(slug=slug,display=cfg['name'],policy='Exact-title whole-word lock; NSFW/off-topic title filter; maxres1-3 generated video stills only; exact Steam app screenshots.',candidate_count=len(rows),videos=[dict(id=v.get('id'),title=v.get('title'),query=v.get('_query'),duration=v.get('duration')) for v in vids],candidates=rows)
  (pack/'manifest.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8'); sheet(pack,cfg['name'],rows); print('DONE',slug,len(rows),flush=True)
 except Exception as x:
  (pack/'error.json').write_text(json.dumps(dict(slug=slug,error=repr(x),traceback=traceback.format_exc()),indent=2),encoding='utf-8'); traceback.print_exc(); return False
 return True

def main():
 failed=[]
 for slug in sys.argv[1:] or list(GAMES):
  if not harvest(slug):failed.append(slug)
 qa={s:len(list((ROOT/s/'candidates').glob('*.jpg'))) for s in GAMES}; qa['failed']=failed; (ROOT/'QA.json').write_text(json.dumps(qa,indent=2),encoding='utf-8')
 print('QA',json.dumps(qa,indent=2),flush=True)
 if failed:sys.exit(1)
if __name__=='__main__':main()
