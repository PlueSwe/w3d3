#!/usr/bin/env python3
"""
Add per-type statistics to skolor.json.
Replaces flat elever_larare/behoriga with stats dict keyed by skoltyp.
"""
import json, sys, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')
HDR = 'Accept: application/vnd.skolverket.plannededucations.api.v4.hal+json'
BASE = 'https://api.skolverket.se/planned-educations/v4'

def curl(url):
    r = subprocess.run(['curl','-s','-H', HDR, url], capture_output=True, timeout=15)
    try: return json.loads(r.stdout)
    except: return None

def latest(arr):
    if not arr: return None
    v = next((x['value'] for x in arr if x.get('valueType') == 'EXISTS'), None)
    return v

def fetch_stats(school):
    code = school['code']
    sd = curl(f'{BASE}/school-units/{code}/statistics')
    if not sd or not sd.get('body') or not sd['body'].get('_links'):
        return code, {}
    links = {k: v['href'] for k, v in sd['body']['_links'].items() if k != 'self'}
    result = {}
    for typ_key, href in links.items():
        # typ_key is like "gr-statistics", strip "-statistics"
        typ = typ_key.replace('-statistics', '')
        d = curl(href)
        if d and d.get('body'):
            b = d['body']
            result[typ] = {
                'elever_larare': latest(b.get('studentsPerTeacherQuota')),
                'behoriga':      latest(b.get('certifiedTeachersQuota')),
                'elever_totalt': latest(b.get('totalNumberOfPupils')),
            }
    return code, result

schools = json.load(open('skolor.json', encoding='utf-8'))
print(f'Fetching per-type stats for {len(schools)} schools...')

by_code = {s['code']: s for s in schools}
done = 0
start = time.time()

with ThreadPoolExecutor(max_workers=16) as pool:
    futures = {pool.submit(fetch_stats, s): s['code'] for s in schools}
    for future in as_completed(futures):
        code = futures[future]
        done += 1
        try:
            c, stats = future.result()
            by_code[c]['stats'] = stats
        except: pass
        if done % 500 == 0:
            elapsed = time.time() - start
            eta = (len(schools) - done) / (done/elapsed) if done else 0
            print(f'  {done}/{len(schools)} ETA {eta/60:.1f}min', flush=True)
            json.dump(list(by_code.values()), open('skolor.json','w',encoding='utf-8'), ensure_ascii=False)

json.dump(list(by_code.values()), open('skolor.json','w',encoding='utf-8'), ensure_ascii=False)
print(f'\nDone! {len(schools)} schools updated.')
