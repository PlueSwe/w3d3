#!/usr/bin/env python3
"""
Match data.json cases to Skolverket school unit codes.
Strategy: inverted word index + areaCode disambiguation via matched-case heuristic.
"""
import json, re, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

DATA    = 'data.json'
SCHOOLS = 'schools_raw.json'

data    = json.load(open(DATA,    encoding='utf-8'))
schools = json.load(open(SCHOOLS, encoding='utf-8'))

STOPWORDS = {
    'av','i','för','med','och','eller','på','om','från','till','inom','efter',
    'samt','vid','af','den','det','de','en','ett','är','som','år','under',
    'riktad','tematisk','planerad','regelbunden','anmälan','tillsyn','ansökan',
    'uppföljning','granskning','beslut','dnr','angående','avseende','ärende',
    'skolan','skola','gymnasiet','gymnasieskolan','grundskolan','förskolan',
    'skolinspektionens','skolinspektionen','huvud','huvudman','inkl','hm',
    'kommunens','kommunala','enskild','fristående','kommunal','fsk',
    'skolenhetsnivå','gymnasieskola','grundskola','förskola','komvux',
    'vuxenutbildning','läsåret','läsår','anpassad','nationellt','godkänd',
    'idrottsutbildningar','riksrekryterande','utbildning','utbildningar',
}

def norm(s):
    s = s.lower()
    s = re.sub(r'[,\.\-–—()/]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def significant_words(s, min_len=4):
    return [w for w in norm(s).split() if w not in STOPWORDS and len(w) >= min_len]

# Build: norm_name -> list of school dicts
by_exact_norm = defaultdict(list)
for sc in schools:
    by_exact_norm[norm(sc['name'])].append(sc)

# Build inverted index: word -> set of school names
word_to_schools = defaultdict(list)
for sc in schools:
    for w in significant_words(sc['name']):
        word_to_schools[w].append(sc)

# Build areaCode -> list of schools
by_area = defaultdict(list)
for sc in schools:
    by_area[sc['areaCode']].append(sc)

# Build commune name -> areaCode mapping from already-matched cases
# (cases with skolkod where we know both kommun and the matched code)
existing_matches = [(r['kommun'].lower(), r['skolkod']) for r in data
                    if r.get('skolkod') and r.get('kommun')]
skolkod_to_area = {sc['code']: sc['areaCode'] for sc in schools}
kommun_to_area = defaultdict(set)
for kname, code in existing_matches:
    area = skolkod_to_area.get(code)
    if area:
        kommun_to_area[kname].add(area)

# Also hardcode some common commune names
KOMMUNKOD = {
    'stockholm':'0180','göteborg':'1480','malmö':'1280','uppsala':'0380',
    'linköping':'0580','västerås':'1980','örebro':'1880','helsingborg':'1283',
    'norrköping':'0581','jönköping':'0680','umeå':'2480','lund':'1281',
    'borlänge':'2081','falun':'2080','solna':'0184','sundbyberg':'0183',
    'nacka':'0182','huddinge':'0141','botkyrka':'0127','haninge':'0136',
    'täby':'0160','järfälla':'0123','danderyd':'0162','sollentuna':'0163',
    'lidingö':'0186','ekerö':'0125','värmdö':'0120','tyresö':'0138',
    'östersund':'2380','sundsvall':'2281','gävle':'2180','kalmar':'0880',
    'karlstad':'1780','växjö':'0780','halmstad':'1380','borås':'1480',
    'eskilstuna':'0484','södertälje':'0181','kristianstad':'1290',
    'trollhättan':'1488','skellefteå':'2480','luleå':'2580',
}
for name, code in KOMMUNKOD.items():
    if code not in kommun_to_area[name]:
        kommun_to_area[name].add(code)

COMMUNE_PAT = re.compile(r'i\s+([\wÅÄÖåäö]+(?:\s+[\wÅÄÖåäö]+)?)\s+(?:kommuns?|stad)\b', re.IGNORECASE)

def find_schools_for_subject(subj_raw, kommunnamn):
    subj = norm(subj_raw)
    subj_words = set(significant_words(subj_raw))
    if not subj_words:
        return []

    # 1. Exact substring match (longest wins)
    exact_hits = [(len(n), n, scs)
                  for n, scs in by_exact_norm.items()
                  if n in subj]
    if exact_hits:
        best = max(exact_hits, key=lambda x: x[0])
        return best[2]

    # 2. Word intersection: candidates must share all significant words with school name
    if not subj_words:
        return []
    candidates = defaultdict(int)
    for w in subj_words:
        for sc in word_to_schools.get(w, []):
            candidates[sc['code']] += 1

    sc_by_code = {sc['code']: sc for sc in schools}
    results = []
    for code, count in candidates.items():
        sc = sc_by_code[code]
        sc_words = set(significant_words(sc['name']))
        if not sc_words:
            continue
        # All school name words must appear in subject
        if sc_words.issubset(subj_words):
            results.append(sc)

    return results

matched_new = 0
total_with_code = sum(1 for r in data if r.get('skolkod'))

for i, rec in enumerate(data):
    if i % 10000 == 0:
        print(f'  {i}/{len(data)}, matched so far: {total_with_code + matched_new}', flush=True)

    if rec.get('skolkod'):
        continue

    subj = rec.get('subject', '')
    if not subj:
        continue

    hits = find_schools_for_subject(subj, rec.get('kommun',''))

    if not hits:
        continue

    if len(hits) == 1:
        rec['skolkod'] = hits[0]['code']
        matched_new += 1
        continue

    # Disambiguate by municipality
    kname = rec.get('kommun','').lower()
    area_codes = kommun_to_area.get(kname, set())
    # Also try from subject text
    m = COMMUNE_PAT.search(subj)
    if m:
        area_codes |= kommun_to_area.get(m.group(1).lower(), set())

    if area_codes:
        filtered = [sc for sc in hits if sc['areaCode'] in area_codes]
        if filtered:
            rec['skolkod'] = filtered[0]['code']
            matched_new += 1
            continue

    # Fall back: pick first
    rec['skolkod'] = hits[0]['code']
    matched_new += 1

total = sum(1 for r in data if r.get('skolkod'))
print(f'\nMatched: {total}/{len(data)} ({100*total/len(data):.1f}%)')
json.dump(data, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False)
print('Saved.')
