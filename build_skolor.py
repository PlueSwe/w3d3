#!/usr/bin/env python3
"""
Build skolor.json: school list with type, statistics and kontaktinfo
from Skolverket API. ~6700 schools, runs in ~5-10 min with parallel requests.
Output: skolor.json  [{code, name, typ, areaCode, skoltyper, elever_larare,
                       behoriga, telefon, email, web, adress, org}]
"""
import json, sys, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')
OUTPUT = 'skolor.json'
HDR = 'Accept: application/vnd.skolverket.plannededucations.api.v4.hal+json'
BASE = 'https://api.skolverket.se/planned-educations/v4'

def curl(url):
    r = subprocess.run(['curl','-s','-H', HDR, url], capture_output=True, timeout=15)
    try: return json.loads(r.stdout)
    except: return None

def fetch_school(code):
    d = curl(f'{BASE}/school-units/{code}')
    if not d or not d.get('body'): return None
    u = d['body']
    ci = u.get('contactInfo') or {}
    addr = next((a for a in (ci.get('addresses') or []) if a.get('type') == 'VISITING_ADDRESS'), {})
    skoltyper = [t['code'] for t in (u.get('typeOfSchooling') or [])]

    # Statistics
    stats_d = curl(f'{BASE}/school-units/{code}/statistics')
    elever_larare = None
    behoriga = None
    if stats_d and stats_d.get('body') and stats_d['body'].get('_links'):
        links = {k: v['href'] for k, v in stats_d['body']['_links'].items() if k != 'self'}
        # Pick first available type
        for typ_key, href in links.items():
            sd = curl(href)
            if sd and sd.get('body'):
                sb = sd['body']
                def latest(arr):
                    if not arr: return None
                    return next((x['value'] for x in arr if x.get('valueType') == 'EXISTS'), None)
                elever_larare = latest(sb.get('studentsPerTeacherQuota'))
                behoriga = latest(sb.get('certifiedTeachersQuota'))
                break

    return {
        'code': code,
        'name': u.get('name',''),
        'typ': u.get('principalOrganizerType',''),
        'areaCode': u.get('geographicalAreaCode',''),
        'skoltyper': skoltyper,
        'elever_larare': elever_larare,
        'behoriga': behoriga,
        'telefon': ci.get('telephone',''),
        'email': ci.get('email',''),
        'web': ci.get('web',''),
        'adress': f"{addr.get('street','')} {addr.get('zipCode','')} {addr.get('city','')}".strip(),
        'org': u.get('corporationName',''),
        'orgnr': u.get('organisationRegistryNumber',''),
        'bolagsform': u.get('companyForm',''),
    }

# Load existing results
existing = {}
try:
    existing = {s['code']: s for s in json.load(open(OUTPUT, encoding='utf-8'))}
    print(f'Resuming: {len(existing)} already done')
except: pass

# Load school codes
schools = json.load(open('schools_raw.json', encoding='utf-8'))
codes = [s['code'] for s in schools if s['code'] not in existing]
print(f'Fetching {len(codes)} schools (skipping {len(existing)} cached)...')

results = dict(existing)
done = 0
errors = 0
start = time.time()

with ThreadPoolExecutor(max_workers=12) as pool:
    futures = {pool.submit(fetch_school, code): code for code in codes}
    for future in as_completed(futures):
        code = futures[future]
        done += 1
        try:
            rec = future.result()
            if rec:
                results[code] = rec
            else:
                errors += 1
        except Exception as e:
            errors += 1

        if done % 200 == 0:
            elapsed = time.time() - start
            rate = done / elapsed
            eta = (len(codes) - done) / rate if rate else 0
            print(f'  {done}/{len(codes)} ({len(results)} ok, {errors} err) ETA {eta/60:.1f}min', flush=True)
            json.dump(list(results.values()), open(OUTPUT,'w',encoding='utf-8'), ensure_ascii=False)

json.dump(list(results.values()), open(OUTPUT,'w',encoding='utf-8'), ensure_ascii=False)
print(f'\nDone! {len(results)} schools saved, {errors} errors.')
