#!/usr/bin/env python3
import json,re,unicodedata
from pathlib import Path
from urllib.parse import urlparse
from collections import Counter,defaultdict

START_RANK=8168
SKIP_IDS=set(json.loads(r'''["cea4d73b-424d-4727-b682-f19a89527406", "3fb6653e-86f0-4eba-b7fc-95a6e3047753", "ea4098c1-8285-47c9-9729-961e4c013577", "cf0fe102-4d0d-4e82-ab0f-a7f37c849bfb", "9669c431-7c08-4426-87bf-de32be7d8757", "4fdcc76d-1e4e-407e-9c24-f6ad7f413884", "f3ab3c5e-dd2e-495a-a599-b166c19d54af", "daa27d7e-ca00-45ee-a556-a27ef0b96a95", "8641c327-33e9-4f2a-8c9a-518dcfd45d31", "849e22dc-d525-49c3-ab9e-dafb6cb487b2", "4a60d230-0047-45b0-a92a-cc5bc5002a54", "6c31cbe7-9d82-4721-9b11-5ea79cccd5ed", "49858144-4ac0-41ee-9071-8ee3ceb6f268", "a1dcbd5d-1b24-46da-a657-75ae14917d45", "bae403ea-8888-49c4-ba32-552f0a2f1188", "23cdf072-9112-402d-819a-43ca638b1dc2", "2aee205b-60a3-4fd8-867a-1a990a0d1c61", "b98b7861-bede-4e8f-b937-8f8a4adf30a3", "01f7f2a2-c230-4cca-b024-fb155a0f4270", "58eb294b-2f66-4af0-bc6c-10c3161dfa6e", "a60e3caa-d900-40bc-a991-492538f7be01", "9a230dec-22b0-4876-b66f-723d9ca82629", "89bdeca1-8cbd-40b9-973e-fd040331ad23", "c695330c-24b3-46af-82c9-92b4d7711a05", "5f10b672-edce-4399-9867-63fe447ae656", "74f53e2e-b337-4388-996a-52caa3cd1198", "446bcc97-5a68-4f1a-ac76-4b551fd01a97", "bd4182e9-c2bb-4d33-ae73-b7a3c113acdc", "8e11aa3f-087f-4668-8cde-10503ea420aa", "5e039a1d-4e43-4236-af78-80e8f41c9fb3", "ef662124-7a44-4c73-ba58-1ab10edea0ce", "85b36bb9-fc20-42ca-9d0d-efec1e304fce", "dab0564d-700a-49d7-8851-2fea9c12a816", "533537a4-34ea-4d75-8341-96b427bdee8a", "0748e9d5-db13-40a6-9bd7-e993c85d3f80", "deeb04c0-beb5-4df1-93b3-43968bb57d64", "2b15e40b-725d-44e3-b10c-8b8c8f29d2e1", "cf777579-bd0c-423a-8fbb-7e7dfb89697d", "1c47dbe0-0c19-409c-b2e1-5de8e95487e6", "58e40813-0a33-48fe-a3f5-b30701d6fb88", "26674e9e-c7f9-4737-a602-3fd6c06c405f", "9d313e64-91f5-48ad-bd2e-a6c3520dbc82", "fa38da3c-178c-4707-9cd4-1c619a792348", "6342a953-c114-47d3-bc50-5c2a12867293", "becf2a84-6acd-44fc-8de6-34f5ab69196a", "40af02ba-5043-429b-a7bb-bc85eb016ee0", "67763cf8-75ad-4ec3-a5f4-b9d925b8832b", "20fda54a-e416-497b-b9d3-7d9fa3495123", "59677460-473e-4228-8f89-b038eda8168a", "e323330e-6746-4a20-8552-65c985d83eb5", "893ff0d2-526b-435b-890f-0447c95c34e0", "8028cf1c-a517-49e4-9c47-8e32c8012597", "21ff4aa5-ae0e-4519-9cac-9b25361c614d", "8e87b069-1b16-4be8-931d-04ba884632c1", "a34ad1fb-ce3c-4d82-b625-8513de8d592c", "6e6f4349-b219-496f-88c5-ae6d3f20039e", "49aeeca1-b1f3-4a1b-8540-163238c3cb75", "dfb56d89-b229-4bb4-ac62-298053d9e24a", "ec193027-f0d1-478a-b3d1-76677cb6a6f0", "3530a399-9795-4ec5-bdf4-dcb46ae95d23", "e5a0ae14-26c7-4dcd-a104-1182ebfb9438", "f210e41f-18be-42f6-acd6-2fdbd94a730b", "9047b190-cd2f-4168-83ec-4dc21f4e742f", "0c52269b-9024-4d90-bafd-fd7b869b9f2e", "52ed719a-66b6-4b98-8bd4-905bf67df36a", "23c01255-3757-49ac-aba8-5f0aceb87571", "cca7aa51-c1ee-433f-ab02-a5ef53adb642", "373f583d-e2c2-490c-96a6-9a8aa1a4d61e", "1ed10161-d3f7-4c9b-84f2-97aeddf4a3f1", "df630884-386f-47b2-9a33-87b767aa81e9", "5ef746f8-ece9-4c0e-b430-1bd5b76ee091", "50d42c00-693e-424d-80fd-616534b73de1", "e66d27b6-6877-4526-84fa-57eb4c6b4f16", "7cdd4a4c-f841-4f29-b2f3-9cf906f5a0ac", "5f65946f-8e52-41bb-acad-d5d8bd6ba62e", "c5e27541-2839-4d96-a614-f0cfcd1d1f11", "b230ea9c-7503-4110-8f2f-c3e48600e7e0", "c99414c6-b8a7-42d2-a8cc-29ef787d3b87", "de871a17-4be9-4f70-add4-2b368b018410", "001e3fc0-2193-4772-a9a4-56333aa4d56c", "f55b3b1f-9728-4373-b6cc-a8fafd8fac13", "76158697-9ac1-4e7a-8119-53a1bc93749b", "039f9edc-cb71-4c70-9149-d96963c0109e", "f08a46ac-c87e-42e3-a81e-9fd11bc1efbb", "56ce96a6-9943-4fc3-a2c0-60ee5ee47067", "62a599cb-6307-4a42-99c1-804f20960742", "d5d301a0-ca7f-44e9-b8d5-42f8ba589b90", "f1b70204-9413-468d-b90e-8ea3c59ae1b7", "fe77003f-734e-4c34-ba87-6acc8d33688b", "c6599bdf-bfbd-44e3-bb41-31b10d4f6cdb", "b9bcf713-1955-4179-988f-7e2abaa7a78a", "874c621b-f4e3-4461-ab36-e3fc220f7dac", "9ad34e66-cf9e-4f67-b0fa-d93cfd12e206", "9a5770c9-588d-407f-aeaf-9d19b2178323", "f98c605c-7053-41ba-a83c-cc768a87f8e4", "49fee7b8-388d-493a-ac26-da4fa0753cee", "163f293d-3691-45d1-bf0a-d9a4e0059984", "60d35549-dd52-460a-8421-ee36b1cbdea5", "52aafb50-89f8-4392-9658-b6bb93a22069", "e41f17fe-db48-4fea-9938-1faa18461c30", "e7798c81-73f9-4428-9838-a06eecd4f3ac", "d046e020-087f-46ca-a6a6-8a375bfd05f6", "7d0f4aa2-0f01-4ace-936e-f76c836ea1e9"]'''))
REJECT_IDS={
    '52aafb50-89f8-4392-9658-b6bb93a22069', # mobile-home park brand
    'e7798c81-73f9-4428-9838-a06eecd4f3ac', # eviction-help residential PM
    '60d35549-dd52-460a-8421-ee36b1cbdea5', # bad Outlook/safelinks identity
    'e41f17fe-db48-4fea-9938-1faa18461c30', # IS Property vs Oculus identity mismatch
    'd046e020-087f-46ca-a6a6-8a375bfd05f6', # Robertson vs Pinnacle weak mismatch
}
EXPLICIT_CATS={'hotel','resort','lodging','holiday_rental_home','cabin','bed_and_breakfast','cottage','beach_resort','rv_park','ski_resort'}
EXPLICIT_TERMS=('vacation','holiday','villa','villas','cabin','chalet','cottage','cottages','resort','hotel','residence','residences','suite','suites','retreat','lodging','bnb','bed and breakfast','beach','rental home')
MARKETPLACE={'novasol.it','novasol.hr','novasol.com','booking.com','airbnb.com','vrbo.com','tripadvisor.com','expedia.com'}
MULTI=('co.uk','org.uk','me.uk','ltd.uk','plc.uk','net.uk','com.au','net.au','org.au','com.br','com.mx','co.nz','net.nz','org.nz','co.za','com.pt','com.es','com.tr','co.jp','com.sg','com.hk','com.my')

def clean(v): return str(v or '').replace('\t',' ').replace('\r',' ').replace('\n',' ').strip()
def norm_name(v):
    s=unicodedata.normalize('NFKD',clean(v)).encode('ascii','ignore').decode().lower()
    return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',s)).strip()
def norm_phone(v): return re.sub(r'\D','',clean(v))
def root_host(h):
    h=clean(h).lower().strip('.')
    if h.startswith('www.'):h=h[4:]
    for s in MULTI:
        if h.endswith('.'+s): return '.'.join(h.split('.')[-3:])
    p=h.split('.'); return '.'.join(p[-2:]) if len(p)>=2 else h

def main():
    records=[]
    per=defaultdict(Counter)
    for n in range(61,71):
        p=Path(f'gpt/sheet_sync_queue/bootstrap-20260815T011144Z-{n:03d}.jsonl')
        for line in p.read_text(encoding='utf-8').splitlines():
            if line.strip():
                r=json.loads(line);r['_chunk']=f'{n:03d}';records.append(r)
    if len(records)!=500: raise SystemExit(f'expected 500 records, got {len(records)}')
    rows=[];rank=START_RANK
    for r in records:
        cid=clean(r.get('overture_id')); ch=r['_chunk']
        if cid in SKIP_IDS:
            per[ch]['reject' if cid in REJECT_IDS else 'dup']+=1
            continue
        name=clean(r.get('name'));cat=clean(r.get('category')).lower();brand=clean(r.get('brand'))
        text=(name+' '+brand+' '+cat).lower()
        explicit=cat in EXPLICIT_CATS or any(t in text for t in EXPLICIT_TERMS) or int(r.get('operator_score') or 0)>=85
        if explicit:
            priority='A';score=88 if (cat in {'hotel','resort'} or 'suite' in text or 'residence' in text or 'villa' in text) else 91
            conf='High' if r.get('live_status')=='HIGH' else 'Medium';estatus='Verified';note='';per[ch]['A']+=1
        else:
            priority='B';score=70;conf='Medium';estatus='Recall-first';note='Recall-first: plausible adjacent hospitality/STR buyer retained.';per[ch]['amb']+=1
        city=clean(r.get('city'));state=clean(r.get('state'));street=clean(r.get('street'))
        market=city or clean(r.get('region')).split('::')[0];country=clean(r.get('country'))
        address=', '.join(x for x in (street,city,state) if x)
        website=clean(r.get('website'));domain=root_host(r.get('domain'))
        official=website;listing=''
        if domain in MARKETPLACE or (domain in {'northmyrtlebeachtravel.com','oceanlakesproperties.com'} and norm_name(name) not in norm_name(domain)):
            official='';listing=website
        phone=clean(r.get('public_phone')); email=clean(r.get('public_email')); insta=clean(r.get('instagram'))
        lead=[rank,priority,score,market,country,name,'',clean(r.get('category')),address,phone,1,'','','High','','','','overture:'+cid,'2026-08-15','New',note,official,email,'',insta,'','',listing,'','','','Email' if email else '',conf,estatus,clean(r.get('source_url')),'']
        ndom=domain if official else '';ph=norm_phone(phone);nn=norm_name(name)
        dkey=('domain:'+ndom) if ndom else (('phone:'+ph) if ph else f'name:{nn}|{norm_name(market)}|{norm_name(country)}')
        ded=[dkey,ph,ndom,nn,market,country,name,rank]
        rows.append(lead+ded);per[ch]['new']+=1;rank+=1
    if len(rows)!=397: raise SystemExit(f'expected 397 payload rows, got {len(rows)}')
    out=Path('gpt/sync_payload_061_070.tsv')
    out.write_text('\n'.join('\t'.join(clean(v) for v in row) for row in rows)+'\n',encoding='utf-8')
    summary={'inspected':500,'duplicates':sum(x['dup'] for x in per.values()),'clear_rejects':sum(x['reject'] for x in per.values()),'new_rows':len(rows),'priority_a':sum(x['A'] for x in per.values()),'ambiguous_retained':sum(x['amb'] for x in per.values()),'start_rank':START_RANK,'end_rank':rank-1,'chunks':{k:dict(v) for k,v in sorted(per.items())}}
    Path('gpt/sync_summary_061_070.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__': main()
