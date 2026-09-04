#!/usr/bin/env python3
"""Add totalNumberOfPupils to skolor.json from Skolverket statistics API."""
import json, sys, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')
HDR = 'Accept: application/vnd.skolverket.plannededucations.api.v4.hal+json'
BASE = 'https://api.skolverket.se/planned-educations/v4'

def curl(url):
    r = subprocess.run(['curl','-s','-H', HDR, url], capture_output=True, timeout=15)
    try: return json.loads(r.stdout)
    except: return None

def fetch_pupils(school):
    code = school['code']
    sd = curl(f'{BASE}/school-units/{code}/statistics')
    if not sd or not sd.get('body') or not sd['body'].get('_links'):
        return code, None
    links = {k: v['href'] for k, v in sd['body']['_links'].items() if k != 'self'}
    for typ_key, href in links.items():
        d = curl(href)
        if d and d.get('body'):
            arr = d['body'].get('totalNumberOfPupils', [])
            val = next((x['value'] for x in arr if x.get('valueType') == 'EXISTS'), None)
            if val:
                return code, val
    return code, None

schools = json.load(open('skolor.json', encoding='utf-8'))
print(f'Fetching pupil counts for {len(schools)} schools...')

results = {s['code']: s for s in schools}
done = 0
found = 0
start = time.time()

with ThreadPoolExecutor(max_workers=16) as pool:
    futures = {pool.submit(fetch_pupils, s): s['code'] for s in schools}
    for future in as_completed(futures):
        code = futures[future]
        done += 1
        try:
            c, val = future.result()
            if val:
                results[c]['elever_totalt'] = val
                found += 1
        except: pass
        if done % 500 == 0:
            elapsed = time.time() - start
            eta = (len(schools) - done) / (done/elapsed) if done else 0
            print(f'  {done}/{len(schools)} ({found} with data) ETA {eta/60:.1f}min', flush=True)
            json.dump(list(results.values()), open('skolor.json','w',encoding='utf-8'), ensure_ascii=False)

json.dump(list(results.values()), open('skolor.json','w',encoding='utf-8'), ensure_ascii=False)
print(f'\nDone! {found}/{len(schools)} schools have pupil count.')
