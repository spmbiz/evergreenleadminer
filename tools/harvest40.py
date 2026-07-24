import argparse,hashlib,html,json,math,os,re,shutil,subprocess,time
from pathlib import Path
from urllib.parse import urljoin,unquote
import cv2,imagehash,numpy as np,requests
from bs4 import BeautifulSoup
from PIL import Image,ImageDraw,ImageFont,ImageOps
S=requests.Session();S.headers['User-Agent']='Mozilla/5.0 Chrome/126 Safari/537.36';T=30
D=[
("P","A Bug's Life",1998,"a-bug-s-life","Flik|Princess Atta|Dot|Hopper|Heimlich|Francis|Slim"),("P","Toy Story 2",1999,"toy-story-2","Woody|Buzz Lightyear|Jessie|Bullseye|Stinky Pete|Al McWhiggin|Emperor Zurg"),("P","Monsters, Inc.",2001,"monsters-inc","Sulley|Mike Wazowski|Boo|Randall Boggs|Celia Mae|Roz|Henry J. Waternoose"),("P","Finding Nemo",2003,"finding-nemo","Marlin|Nemo|Dory|Gill|Bruce|Crush|Darla"),("P","The Incredibles",2004,"the-incredibles","Mr. Incredible|Elastigirl|Violet Parr|Dash Parr|Jack-Jack|Frozone|Syndrome|Edna Mode"),("P","Cars",2006,"cars","Lightning McQueen|Mater|Sally Carrera|Doc Hudson|Luigi|Guido|Chick Hicks|Ramone"),("P","Toy Story 3",2010,"toy-story-3","Woody|Buzz Lightyear|Jessie|Lotso|Ken|Barbie|Big Baby|Bonnie"),("P","Cars 2",2011,"cars-2","Lightning McQueen|Mater|Finn McMissile|Holley Shiftwell|Francesco Bernoulli|Professor Zündapp"),("P","Brave",2012,"brave","Merida|Queen Elinor|King Fergus|Harris Hubert and Hamish|Mor'du|The Witch"),("P","Monsters University",2013,"monsters-university","Mike Wazowski|Sulley|Randall Boggs|Dean Hardscrabble|Squishy|Art|Don Carlton"),
("P","Inside Out",2015,"inside-out","Joy|Sadness|Anger|Fear|Disgust|Riley Andersen|Bing Bong"),("P","The Good Dinosaur",2015,"the-good-dinosaur","Arlo|Spot|Poppa Henry|Momma Ida|Butch|Nash|Ramsey"),("P","Finding Dory",2016,"finding-dory","Dory|Marlin|Nemo|Hank|Destiny|Bailey|Jenny|Charlie"),("P","Cars 3",2017,"cars-3","Lightning McQueen|Cruz Ramirez|Jackson Storm|Mater|Sally Carrera|Smokey|Sterling"),("P","Incredibles 2",2018,"incredibles-2","Elastigirl|Mr. Incredible|Violet Parr|Dash Parr|Jack-Jack|Frozone|Screenslaver|Winston Deavor"),("P","Toy Story 4",2019,"toy-story-4","Woody|Buzz Lightyear|Bo Peep|Forky|Gabby Gabby|Duke Caboom|Ducky|Bunny|Giggle McDimples"),("P","Onward",2020,"onward","Ian Lightfoot|Barley Lightfoot|Laurel Lightfoot|Wilden Lightfoot|The Manticore|Colt Bronco"),("P","Lightyear",2022,"lightyear","Buzz Lightyear|Sox|Izzy Hawthorne|Mo Morrison|Darby Steel|Zurg|Alisha Hawthorne"),("P","Inside Out 2",2024,"inside-out-2","Joy|Anxiety|Sadness|Anger|Fear|Disgust|Envy|Ennui|Embarrassment|Riley Andersen"),("P","Elio",2025,"elio","Elio Solis|Olga Solis|Glordon|Lord Grigon|Ambassador Questa|Ooooo|Gunther Melmac|Ambassador Helix"),("P","Hoppers",2026,"hoppers","Mabel Tanaka|Mabel Beaver|King George|Mayor Jerry Generazzo|Dr. Sam|Titus|Loaf|Nisha|Tom Lizard|Ellen"),("P","Toy Story 5",2026,"toy-story-5","Woody|Buzz Lightyear|Jessie|Bullseye|Lilypad|Forky|Bo Peep"),
("D","Dinosaur",2000,"dinosaur","Aladar|Neera|Kron|Bruton|Baylene|Eema|Zini|Plio"),("D","Chicken Little",2005,"chicken-little","Chicken Little|Abby Mallard|Runt of the Litter|Fish Out of Water|Buck Cluck|Foxy Loxy"),("D","Meet the Robinsons",2007,"meet-the-robinsons","Lewis|Wilbur Robinson|Bowler Hat Guy|Goob|DOR-15|Doris|Franny Robinson|Cornelius Robinson"),("D","Bolt",2008,"bolt","Bolt|Penny|Mittens|Rhino|Dr. Calico"),("D","Tangled",2010,"tangled","Rapunzel|Flynn Rider|Eugene Fitzherbert|Mother Gothel|Pascal|Maximus|Stabbington Brothers"),("D","Wreck-It Ralph",2012,"wreck-it-ralph","Wreck-It Ralph|Vanellope von Schweetz|Fix-It Felix Jr.|Sergeant Calhoun|King Candy|Turbo|Sour Bill"),("D","Frozen",2013,"frozen","Elsa|Anna|Kristoff|Olaf|Sven|Hans"),("D","Big Hero 6",2014,"big-hero-6","Hiro Hamada|Baymax|Go Go Tomago|Wasabi|Honey Lemon|Fred|Tadashi Hamada|Yokai|Robert Callaghan"),("D","Zootopia",2016,"zootopia","Judy Hopps|Nick Wilde|Chief Bogo|Officer Clawhauser|Dawn Bellwether|Gazelle|Mr. Big"),("D","Moana",2016,"moana","Moana|Maui|Gramma Tala|Chief Tui|Sina|Heihei|Pua|Tamatoa|Te Ka|Te Fiti"),("D","Ralph Breaks the Internet",2018,"ralph-breaks-the-internet","Wreck-It Ralph|Vanellope von Schweetz|Shank|Yesss|KnowsMore|Fix-It Felix Jr.|Sergeant Calhoun"),("D","Frozen 2",2019,"frozen-2","Elsa|Anna|Kristoff|Olaf|Sven|Honeymaren|Ryder|Yelana|Lieutenant Mattias|Bruni|The Nokk"),("D","Raya and the Last Dragon",2021,"raya-and-the-last-dragon","Raya|Sisu|Namaari|Tuk Tuk|Boun|Tong|Little Noi|Chief Benja|Virana"),("D","Encanto",2021,"encanto","Mirabel Madrigal|Alma Madrigal|Bruno Madrigal|Isabela Madrigal|Luisa Madrigal|Julieta Madrigal|Pepa Madrigal|Antonio Madrigal|Dolores Madrigal|Camilo Madrigal"),("D","Strange World",2022,"strange-world","Searcher Clade|Ethan Clade|Jaeger Clade|Meridian Clade|Legend|Splat|Callisto Mal"),("D","Wish",2023,"wish","Asha|Star|King Magnifico|Queen Amaya|Valentino|Dahlia|Simon"),("D","Moana 2",2024,"moana-2","Moana|Maui|Simea|Moni|Loto|Kele|Matangi|Nalo|Heihei|Pua"),("D","Zootopia 2",2025,"zootopia-2","Judy Hopps|Nick Wilde|Gary De'Snake|Nibbles Maplestick|Chief Bogo|Officer Clawhauser|Lynxley Family")]
SLOTS=['signature_establishing','day_exterior','night_exterior','signature_interior','hero_medium','hero_closeup','group_composition','action','emotional','comedy','materials','fx_atmosphere','color_script_peak','silhouette_scale','wildcard_signature']
def req(u):
 for i in range(4):
  try:r=S.get(u,timeout=T);r.raise_for_status();return r
  except Exception as e:time.sleep(i+1)
 raise e
def pages(x):
 st,t,y,sl,ch=x
 return ([f'https://www.pixar.com/{sl}'] if st=='P' else [f'https://disneyanimation.com/films/{sl}/',f'https://movies.disney.com/{sl}'])
def urls(page,txt):
 soup=BeautifulSoup(txt,'html.parser');out={}
 for z in soup.find_all(['img','source','a','video']):
  alt=' '.join([z.get('alt',''),z.get('title',''),z.get('aria-label','')]);par=z.find_parent(['figure','li','section','article','div']);ctx=par.get_text(' ',strip=True)[:500] if par else ''
  vv=[z.get(a) for a in ['src','data-src','data-image','data-lazy-src','data-original','poster','href'] if z.get(a)]
  for a in ['srcset','data-srcset']:
   if z.get(a):vv += [q.strip().split()[0] for q in z.get(a).split(',')]
  for u in vv:
   u=html.unescape(u).replace('\\/','/');u=urljoin(page,u)
   if any(e in u.lower() for e in ['.jpg','.jpeg','.png','.webp','images.squarespace-cdn.com','cdn.disneyanimation.com']):out[u]=(u,alt,ctx)
 for m in re.findall(r'https?:\\?/\\?/[^"\'<> ]+?(?:\.jpe?g|\.png|\.webp)(?:\?[^"\'<> ]*)?',txt,re.I):
  u=html.unescape(m).replace('\\/','/');out.setdefault(u,(u,'','embedded'))
 return list(out.values())
def norm(b,p):
 try:
  import io
  im=ImageOps.exif_transpose(Image.open(io.BytesIO(b))).convert('RGB');w,h=im.size
  if max(w,h)<640 or min(w,h)<300:return
  if max(w,h)>1600:
   q=1600/max(w,h);im=im.resize((round(w*q),round(h*q)),Image.Resampling.LANCZOS)
  p.parent.mkdir(parents=True,exist_ok=True);im.save(p,'JPEG',quality=88,optimize=True,progressive=True);raw=p.read_bytes();g=cv2.imread(str(p));gray=cv2.cvtColor(g,cv2.COLOR_BGR2GRAY);qual=float(np.log1p(cv2.Laplacian(gray,cv2.CV_64F).var())+gray.std()/25);return {'p':str(p),'w':im.width,'h':im.height,'ph':str(imagehash.phash(im)),'sha':hashlib.sha256(raw).hexdigest(),'q':qual}
 except:return
def sitepool(x,fd,log):
 out=[];seen=set();k=0
 for pg in pages(x):
  try:a=urls(pg,req(pg).text);log.append(f'{pg} discovered {len(a)}')
  except Exception as e:log.append(f'page fail {pg} {e}');continue
  for u,alt,ctx in a:
   if u in seen or k>120:continue
   seen.add(u);lo=(u+' '+alt).lower()
   if any(v in lo for v in ['logo','favicon','sprite','icon-','rating','facebook','twitter','instagram']):continue
   if 'images.squarespace-cdn.com' in u and 'format=' not in u:u += ('&' if '?' in u else '?')+'format=1600w'
   try:
    r=req(u)
    if len(r.content)<15000:continue
    q=norm(r.content,fd/'SITE_POOL'/f'site_{k:03}.jpg')
    if q:q.update(url=u,alt=alt,ctx=ctx,src='official_site');out.append(q);k+=1
   except Exception as e:pass
 return out
def run(c,to=300):return subprocess.run(c,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=to)
def trailer(x,fd,log):
 st,t,y,sl,ch=x;query=f"ytsearch8:{'Pixar' if st=='P' else 'Walt Disney Animation Studios'} {t} official trailer";u=None
 try:
  p=run(['yt-dlp','--flat-playlist','--dump-json','--no-warnings',query],120)
  for l in p.stdout.splitlines():
   e=json.loads(l);chan=' '.join(str(e.get(k,'')) for k in ['channel','uploader','channel_id']).lower();tt=e.get('title','').lower()
   if (('pixar' in chan) if st=='P' else ('disney' in chan)) and ('trailer' in tt or 'teaser' in tt):u=e.get('webpage_url') or 'https://youtube.com/watch?v='+e['id'];break
 except Exception as e:log.append('search fail '+str(e))
 if not u:return []
 td=fd/'_trailer';td.mkdir(exist_ok=True);p=run(['yt-dlp','-f','bv*[height<=720]+ba/b[height<=720]','--merge-output-format','mp4','-o',str(td/'v.%(ext)s'),u],420);vid=next((z for z in td.glob('v.*') if z.suffix in ['.mp4','.mkv','.webm']),None)
 if not vid:log.append('download fail '+p.stderr[-400:]);return []
 try:dur=float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(vid)],30).stdout)
 except:dur=120
 out=[];(fd/'TRAILER_POOL').mkdir(exist_ok=True)
 for i,ts in enumerate(np.linspace(dur*.07,dur*.91,90)):
  z=fd/'TRAILER_POOL'/f'frame_{i:03}.jpg';p=run(['ffmpeg','-loglevel','error','-ss',f'{ts:.2f}','-i',str(vid),'-frames:v','1','-vf','scale=min(1280\\,iw):-2','-q:v','2',str(z)],30)
  if z.exists():
   q=norm(z.read_bytes(),z)
   if q:q.update(url=u,alt=f'trailer {ts:.2f}s',ctx='official trailer',src='official_trailer');out.append(q)
 shutil.rmtree(td,ignore_errors=True);log.append(f'trailer {u} frames {len(out)}');return out
def dedupe(a):
 o=[];hs=[]
 for c in sorted(a,key=lambda x:x['q'],reverse=True):
  h=imagehash.hex_to_hash(c['ph'])
  if any(h-x<7 for x in hs):continue
  o.append(c);hs.append(h)
  if len(o)>=180:break
 return o
def feat(p):
 im=cv2.resize(cv2.imread(p),(240,135));h=cv2.cvtColor(im,cv2.COLOR_BGR2HSV);v=cv2.calcHist([h],[0,1],None,[12,5],[0,180,0,256]).flatten();v/=v.sum()+1e-7;return v/(np.linalg.norm(v)+1e-7)
def diverse(a,n):
 if len(a)<=n:return a
 f=np.stack([feat(x['p']) for x in a]);q=np.array([x['q'] for x in a]);q=(q-q.min())/(np.ptp(q)+1e-7);sel=[int(q.argmax())];md=np.ones(len(a))*9
 while len(sel)<n:
  md=np.minimum(md,1-np.clip(f@f[sel[-1]],-1,1));u=md+.12*q;u[sel]=-9;sel.append(int(u.argmax()))
 return [a[i] for i in sel]
def toks(n):return [x.lower() for x in re.findall(r"[A-Za-zÀ-ÿ0-9'-]+",n) if len(x)>2 and x.lower() not in ['the','and','officer','chief','king','queen']]
def charpick(a,n):
 tt=toks(n);m=[]
 for c in a:
  s=(c['alt']+' '+c['ctx']+' '+Path(c['url']).name).lower();hit=sum(x in s for x in tt)
  if hit:m.append((hit,c['q'],c))
 m.sort(reverse=True,key=lambda z:(z[0],z[1]));o=[z[2] for z in m[:4]];method='metadata_match'
 if len(o)<4:o += diverse([x for x in a if x not in o],4-len(o));method='metadata_plus_review_candidates'
 return o[:4],method
def sheet(items,dest,cols=5):
 if not items:return
 W,H=330,200;M,L=12,32;rows=math.ceil(len(items)/cols);can=Image.new('RGB',(M+cols*(W+M),M+rows*(H+L+M)),(25,25,25));dr=ImageDraw.Draw(can)
 try:fo=ImageFont.truetype('DejaVuSans.ttf',15)
 except:fo=ImageFont.load_default()
 for i,(p,l) in enumerate(items):
  im=Image.open(p).convert('RGB');im.thumbnail((W,H));bg=Image.new('RGB',(W,H),(8,8,8));bg.paste(im,((W-im.width)//2,(H-im.height)//2));x=M+i%cols*(W+M);y=M+i//cols*(H+L+M);can.paste(bg,(x,y));dr.text((x,y+H+5),l[:36],font=fo,fill='white')
 can.save(dest,'JPEG',quality=86,optimize=True)
def process(x,root):
 st,t,y,sl,chs=x;fd=root/f'{y}_{sl}';fd.mkdir(parents=True,exist_ok=True);log=[];a=dedupe(sitepool(x,fd,log)+trailer(x,fd,log));sty=diverse(a,min(15,len(a)));sm=[]
 for i,c in enumerate(sty):
  d=fd/'STYLE_PACK'/f'{i+1:02}_{SLOTS[i]}.jpg';d.parent.mkdir(exist_ok=True);shutil.copy2(c['p'],d);sm.append({'slot':SLOTS[i],'file':str(d.relative_to(fd)),'source':c})
 cm={};ci=[]
 for n in chs.split('|'):
  rr,m=charpick(a,n);ee=[]
  for i,c in enumerate(rr):
   d=fd/'CHARACTER_PACK'/re.sub(r'[^A-Za-z0-9_-]+','_',n)/f'ref_{i+1:02}.jpg';d.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(c['p'],d);ee.append({'file':str(d.relative_to(fd)),'source':c});ci.append((d,f'{n} {i+1}'))
  cm[n]={'method':m,'human_identity_review':m!='metadata_match','refs':ee}
 sheet([(fd/z['file'],z['slot']) for z in sm],fd/'STYLE_CONTACT_SHEET.jpg');sheet(ci,fd/'CHARACTER_CONTACT_SHEET.jpg',4)
 man={'title':t,'year':y,'studio':'Pixar' if st=='P' else 'Walt Disney Animation Studios','official_pages':pages(x),'counts':{'unique_candidates':len(a),'style':len(sty),'characters':len(cm),'character_refs':sum(len(v['refs']) for v in cm.values())},'style_complete':len(sty)==15,'style':sm,'characters':cm,'log':log};(fd/'MANIFEST.json').write_text(json.dumps(man,ensure_ascii=False,indent=2));(fd/'LOG.txt').write_text('\n'.join(log));return {'title':t,'status':'complete' if len(sty)==15 else 'partial','counts':man['counts']}
def main():
 p=argparse.ArgumentParser();p.add_argument('--group',type=int,required=True);p.add_argument('--output',default='output');q=p.parse_args();root=Path(q.output)/f'remaining40_group_{q.group+1}';root.mkdir(parents=True,exist_ok=True);res=[]
 for x in D[q.group*10:(q.group+1)*10]:
  try:print('PROCESS',x[1],flush=True);res.append(process(x,root))
  except Exception as e:import traceback;traceback.print_exc();res.append({'title':x[1],'status':'failed','error':str(e)})
 (root/'GROUP_SUMMARY.json').write_text(json.dumps({'group':q.group+1,'results':res},ensure_ascii=False,indent=2));print(json.dumps(res,indent=2))
if __name__=='__main__':main()
