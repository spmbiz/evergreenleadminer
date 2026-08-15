#!/usr/bin/env python3
"""Adversarial legacy no-site challenger v4.

Goal: maximize false-negative recovery without ever certifying VERIFIED_NO_WEBSITE.
This is intentionally a challenger: a survivor is MEDIUM/review-only; search/provider
failures fail closed as ERROR_RETRYABLE.
"""
from __future__ import annotations
import asyncio, base64, html, random, re, urllib.parse
import gws_legacy_deep_v2 as v2

FREE_MAIL={
 'gmail.com','googlemail.com','outlook.com','hotmail.com','hotmail.be','live.com','live.be',
 'msn.com','yahoo.com','yahoo.fr','icloud.com','me.com','proton.me','protonmail.com',
 'skynet.be','telenet.be','scarlet.be','voo.be','gmx.com','gmx.net'
}
DEAD_PHRASES=(
 'domain for sale','buy this domain','this domain is for sale','domain is parked','parkingcrew',
 'sedoparking','hugedomains','afternic','website suspended','account suspended','site not found'
)
LEGAL_WORDS={'srl','sprl','bv','nv','sa','company','co','services','service','belgium','belgique','brussels','bruxelles'}


def _email_domains(c):
    out=[]
    for email in re.split(r'[\s,;]+', v2.t(c.get('em'))):
        if '@' not in email: continue
        d=email.rsplit('@',1)[1].lower().strip(' .<>()[]{}\"\'')
        if d and '.' in d and d not in FREE_MAIL and d not in out: out.append(d)
    return out


def _roots(name):
    ws=[x for x in v2.n(name).split() if len(x)>2 and x not in v2.STOP and x not in LEGAL_WORDS][:5]
    out=[]
    for take in (ws[:2],ws[:3],ws[:4]):
        if not take: continue
        for r in (''.join(take),'-'.join(take)):
            if 4<=len(r)<=45 and r not in out: out.append(r)
    return out[:5]


def guesses(c):
    out=[]
    cow=v2.t(c.get('cow'))
    if cow and not v2.platform(cow): out.append(cow if '://' in cow else 'https://'+cow)
    for d in _email_domains(c):
        out += [f'https://{d}/', f'https://www.{d}/']
    for r in _roots(c.get('n')):
        for s in ('.be','.com','.eu','.net'):
            out.append('https://'+r+s+'/')
    seen=[]
    for u in out:
        h=v2.host(u)
        if h and h not in seen and not v2.platform(u): seen.append(h)
        else: continue
        if len(seen)>=16: break
    return ['https://'+h+'/' for h in seen]


def search_queries(c):
    name=v2.t(c.get('n')); pc=v2.t(c.get('p'))[:4]; addr=v2.t(c.get('a')); ph=v2.t(c.get('ph'))
    qs=[]
    if name and pc:
        qs += [f'"{name}" {pc}', f'{name} {pc} Brussels']
    if name and addr:
        street=' '.join(v2.n(addr).split()[:6])
        if street: qs.append(f'{name} "{street}"')
    if ph:
        digits=re.sub(r'\D','',ph)
        qs.append(f'"{ph}"')
        if digits and digits!=ph: qs.append(f'"{digits}"')
    if name: qs.append(f'{name} official website')
    out=[]
    for q in qs:
        q=re.sub(r'\s+',' ',q).strip()
        if q and q not in out: out.append(q)
    return out[:6]


def _unwrap(u):
    u=html.unescape(u.strip())
    try:
        p=urllib.parse.urlparse(u); q=urllib.parse.parse_qs(p.query); h=(p.hostname or '').lower()
        if 'google.' in h and p.path=='/url':
            u=(q.get('q') or q.get('url') or [u])[0]
        elif 'duckduckgo.com' in h and ('uddg' in q or p.path.startswith('/l/')):
            u=(q.get('uddg') or q.get('u') or [u])[0]
        elif 'bing.com' in h and p.path.startswith('/ck/'):
            raw=(q.get('u') or q.get('url') or [u])[0]
            if raw.startswith('a1'):
                try:
                    z=raw[2:]; z += '='*((4-len(z)%4)%4)
                    raw=base64.urlsafe_b64decode(z).decode('utf-8','ignore')
                except Exception: pass
            u=raw
    except Exception: pass
    return u


def hrefs(body, base, limit=30):
    out=[]; seen=set()
    for h in re.findall(r'''href\s*=\s*["']([^"'#]+)''', body, re.I):
        u=_unwrap(urllib.parse.urljoin(base, h.strip()))
        if u.startswith('http') and not v2.platform(u):
            hh=v2.host(u)
            if hh and hh not in seen: seen.add(hh); out.append(u)
        if len(out)>=limit: break
    return out


def identity(c, body, url=''):
    tx=v2.textish(body); ph=v2.dg(c.get('ph')); digits=v2.dg(tx)
    pm=bool(ph and ph in digits); ns=v2.ov(c.get('n'),tx); ao=v2.ov(c.get('a'),tx); pc=v2.t(c.get('p'))[:4]
    pcm=bool(pc and pc in tx); dh=v2.host(url); dno=v2.ov(c.get('n'),dh.replace('.',' '))
    matched=pm or (ns>=0.68 and (ao>=0.16 or pcm)) or (ns>=0.50 and ao>=0.34) or (dno>=0.62 and ns>=0.42 and (ao>=0.10 or pcm))
    return {'matched':matched,'phone':pm,'name_overlap':round(ns,3),'address_overlap':round(ao,3),'postcode':pcm,'domain_name_overlap':round(dno,3)}


def _dead(status, body):
    if status>=400: return True
    tx=v2.n(body[:120000])
    if any(x in tx for x in DEAD_PHRASES): return True
    if ('404' in tx and ('not found' in tx or 'page introuvable' in tx or 'page niet gevonden' in tx)) and len(tx)<7000: return True
    return False


async def webcheck(rows, conc, search_conc):
    import aiohttp
    sem=asyncio.Semaphore(conc); ssem=asyncio.Semaphore(search_conc)
    timeout=aiohttp.ClientTimeout(total=16,connect=5,sock_read=10)
    ans={}; headers={'User-Agent':v2.UA,'Accept-Language':'fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7'}
    async with aiohttp.ClientSession(timeout=timeout,headers=headers) as sess:
        async def get(url,is_search=False):
            attempts=3 if is_search else 2; last={}
            for attempt in range(attempts):
                try:
                    async with (ssem if is_search else sem):
                        if is_search: await asyncio.sleep(random.uniform(.15,.45)*(attempt+1))
                        async with sess.get(url,allow_redirects=True,ssl=True) as r:
                            b=(await r.content.read(v2.MAXBODY)).decode(errors='ignore'); low=b.lower()
                            blocked=r.status in (403,429) or any(x in low for x in ('unusual traffic','captcha','verify you are human','detected unusual'))
                            if blocked: last={'ok':False,'status':r.status,'blocked':True,'url':str(r.url)}
                            else: return {'ok':True,'status':r.status,'url':str(r.url),'body':b}
                except Exception as e: last={'ok':False,'error':type(e).__name__}
                await asyncio.sleep(.25*(2**attempt))
            return last or {'ok':False,'error':'UNKNOWN'}
        def providers(query):
            qq=urllib.parse.quote_plus(query)
            return [('google',f'https://www.google.com/search?hl=fr&num=10&filter=0&q={qq}'),('bing',f'https://www.bing.com/search?count=10&q={qq}'),('ddg',f'https://html.duckduckgo.com/html/?q={qq}')]
        async def one(c):
            ev={'search_queries':0,'search_usable_queries':0,'search_health':[],'search_candidates':[],'healthy_providers':[],'direct_checked':0,'direct_health':[],'owned':'','owned_identity':{},'owned_via':'','candidate_seeds':[]}
            seeds=guesses(c); ev['candidate_seeds']=seeds[:]
            for sq in search_queries(c):
                ev['search_queries']+=1; qlinks=[]; qseen=set(); qhealth=[]
                for provider,url in providers(sq):
                    q=await get(url,is_search=True)
                    links=hrefs(q.get('body',''),q.get('url',url),24) if q.get('ok') and q.get('status',999)<400 else []
                    healthy=bool(q.get('ok') and q.get('status',999)<400 and not q.get('blocked'))
                    if healthy and provider not in ev['healthy_providers']: ev['healthy_providers'].append(provider)
                    qhealth.append({'provider':provider,'http_ok':healthy,'status':q.get('status'),'blocked':bool(q.get('blocked')),'error':q.get('error'),'external_domains':len({v2.host(x) for x in links})})
                    for u in links:
                        h=v2.host(u)
                        if h and h not in qseen: qseen.add(h); qlinks.append(u)
                if qlinks: ev['search_usable_queries']+=1
                ev['search_health'].append({'query':sq,'providers':qhealth,'external_domains':len(qseen)})
                existing={v2.host(x) for x in ev['search_candidates']}
                for u in qlinks:
                    if v2.host(u) not in existing: ev['search_candidates'].append(u); existing.add(v2.host(u))
                if len(existing)>=16 and ev['search_usable_queries']>=2: break
            cand=seeds+ev['search_candidates']; seen=set()
            for u in cand:
                h=v2.host(u)
                if not h or h in seen or v2.platform(u): continue
                seen.add(h); q=await get(u); ev['direct_checked']+=1
                dh={'seed':u,'final':q.get('url',u),'status':q.get('status'),'ok':bool(q.get('ok')),'error':q.get('error')}
                if q.get('ok') and not _dead(int(q.get('status') or 999),q.get('body','')):
                    ide=identity(c,q.get('body',''),q.get('url',u)); dh['identity']=ide
                    if ide['matched'] and not v2.platform(q.get('url',u)):
                        ev['owned']=q.get('url',u); ev['owned_identity']=ide; ev['owned_via']='prior_or_email_domain' if u in seeds else 'persistent_search'; ev['direct_health'].append(dh); break
                ev['direct_health'].append(dh)
                if ev['direct_checked']>=20: break
            return int(c['r']),ev
        results=await asyncio.gather(*(one(c) for c in rows)); ans.update(results)
    return ans


def classify(c,p,pe,w,ovok):
    r=int(c['r']); base={'r':r,'candidate':c,'place':pe,'web':w}
    if not v2.in_scope(c): return {**base,'status':'REJECT','reason':'OUT_OF_SCOPE'}
    if p:
        s=v2.owned(p.get('websites'))
        if s: return {**base,'status':'REJECT','reason':'OWNED_SITE_OVERTURE','owned_site':s}
        if v2.t(p.get('operating_status')).lower() in {'closed','permanently_closed'}: return {**base,'status':'REJECT','reason':'CLOSED_OVERTURE'}
    if w.get('owned'): return {**base,'status':'REJECT','reason':'OWNED_SITE_SEARCH_CONFIRMED','owned_site':w['owned'],'owned_via':w.get('owned_via')}
    if not ovok: return {**base,'status':'ERROR_RETRYABLE','reason':'OVERTURE_UNAVAILABLE'}
    if not pe.get('resolved'): return {**base,'status':'UNCERTAIN','reason':'CURRENT_IDENTITY_NOT_RESOLVED'}
    healthy=len(set(w.get('healthy_providers') or [])); usable=int(w.get('search_usable_queries') or 0); searched=int(w.get('search_queries') or 0); checked=int(w.get('direct_checked') or 0)
    if searched==0 or healthy<2: return {**base,'status':'ERROR_RETRYABLE','reason':'SEARCH_COVERAGE_INSUFFICIENT'}
    if usable==0: return {**base,'status':'ERROR_RETRYABLE','reason':'SERP_PARSING_OR_ZERO_LINKS_UNTRUSTED'}
    ready=healthy>=2 and usable>=2 and checked>=5
    return {**base,'status':'MEDIUM','reason':'RESOLVED_SURVIVED_MULTI_ENGINE_ADVERSARIAL_CHALLENGE','strict_review_ready':ready}

v2.guesses=guesses
v2.search_queries=search_queries
v2.hrefs=hrefs
v2.identity=identity
v2.webcheck=webcheck
v2.classify=classify

if __name__=='__main__': v2.main()
