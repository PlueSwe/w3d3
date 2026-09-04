#!/usr/bin/env python3
"""
Fix skolkod matches where the matched school is in the wrong municipality.
Uses only hardcoded municipality->areaCode mapping (not learned from data).
"""
import json, re, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

data       = json.load(open('data.json',       encoding='utf-8'))
schools    = json.load(open('schools_raw.json', encoding='utf-8'))
sc_by_code = {s['code']: s for s in schools}

# areaCode lookup — Swedish municipalities (kommuner) -> areaCode
# areaCode in Skolverket matches Swedish municipality code (kommunkod)
KOMMUNKOD = {}
# Build comprehensive map from schools_raw using names of municipalities embedded
# in cases. Instead, load a hardcoded list from the most common ones + extract
# from data.json's `geographicalAreaCode` by looking at unambiguous single-school matches.

# Hardcoded core
_kk = {
    'ale':'1440','alingsås':'1489','alvesta':'0764','aneby':'0604',
    'arboga':'1984','arjeplog':'2506','arvidsjaur':'2505','arvika':'1784',
    'askersund':'1880','avesta':'2084','bengtsfors':'1460','berg':'2303',
    'bjurholm':'2411','bjuv':'1260','boden':'2582','bollebygd':'1443',
    'bollnäs':'2183','borgholm':'0840','borlänge':'2081','borås':'1480',
    'botkyrka':'0127','boxholm':'0560','bromölla':'1272','bräcke':'2305',
    'burlöv':'1261','båstad':'1278','dals-ed':'1406','danderyd':'0162',
    'degerfors':'1862','dorotea':'2425','eksjö':'0681','emmaboda':'0763',
    'enköping':'0381','eskilstuna':'0484','eslöv':'1285','essunga':'1490',
    'fagersta':'1982','falkenberg':'1382','falköping':'1499','falun':'2080',
    'filipstad':'1782','finspång':'0562','flen':'0482','forshaga':'1763',
    'färgelanda':'1407','gagnef':'2082','gällivare':'2523','gävle':'2180',
    'gnesta':'0481','gnosjö':'0617','gotland':'0980','grums':'1761',
    'grästorp':'1444','gullspång':'1493','gällivare':'2523','göteborg':'1480',
    'götene':'1498','habo':'0560','hagfors':'1783','hallsberg':'1861',
    'hallstahammar':'1985','halmstad':'1380','hammarö':'1764','haninge':'0136',
    'haparanda':'2583','heby':'0331','hedemora':'2083','helsingborg':'1283',
    'herrljunga':'1496','hjo':'1497','hofors':'2184','huddinge':'0141',
    'hudiksvall':'2184','hultsfred':'0821','hylte':'1315','håbo':'0305',
    'hällefors':'1863','härjedalen':'2361','härnösand':'2280','härryda':'1452',
    'hässleholm':'1290','höganäs':'1263','hörby':'1287','höör':'1288',
    'jokkmokk':'2521','järfälla':'0123','jönköping':'0680',
    'kalix':'2584','kalmar':'0880','karlsborg':'1446','karlshamn':'1083',
    'karlskoga':'1883','karlskrona':'1082','karlstad':'1780','katrineholm':'0483',
    'kil':'1757','kinda':'0562','kiruna':'2581','klippan':'1276',
    'knivsta':'0330','kramfors':'2282','kristianstad':'1290','kristinehamn':'1781',
    'krokom':'2309','kumla':'1882','kungsbacka':'1384','kungsör':'1987',
    'kungälv':'1442','kävlinge':'1264','köping':'1983',
    'laholm':'1381','landskrona':'1282','lerum':'1441','leksand':'2029',
    'lidingö':'0186','lidköping':'1494','lilla edet':'1445',
    'lindesberg':'1864','linköping':'0580','ljungby':'0781','ljusnarsberg':'1885',
    'ljusdal':'2161','ludvika':'2085','luleå':'2580','lund':'1281',
    'lycksele':'2462','lysekil':'1484','malmö':'1280','malung-sälen':'2023',
    'malå':'2507','mark':'1256','mariestad':'1493','markaryd':'0767',
    'mellerud':'1461','mjölby':'0581','mora':'2062','motala':'0583',
    'mullsjö':'0642','munkedal':'1430','munkfors':'1762','mölndal':'1481',
    'mönsterås':'0861','mörbylånga':'0840','nacka':'0182','nora':'1884',
    'norberg':'1986','nordanstig':'2132','nordmaling':'2401','norrköping':'0581',
    'norrtälje':'0188','norsjö':'2508','nybro':'0862','nykvarn':'0140',
    'nyköping':'0480','nynäshamn':'0139','nässjö':'0682','ockelbo':'2105',
    'olofström':'1084','orsa':'2061','orust':'1421','osby':'1273',
    'oskarshamn':'0882','ovanåker':'2121','oxelösund':'0481',
    'partille':'1482','perstorp':'1275','piteå':'2581',
    'ragunda':'2303','robertsfors':'2409','ronneby':'1081',
    'rättvik':'2031','sala':'1981','salem':'0128','sandviken':'2181',
    'sigtuna':'0191','simrishamn':'1291','sjöbo':'1264',
    'skara':'1495','skellefteå':'2480','skinnskatteberg':'1984',
    'skurup':'1265','skövde':'1496','smedjebacken':'2086',
    'sollefteå':'2283','sollentuna':'0163','solna':'0184',
    'sorsele':'2463','sotenäs':'1427','staffanstorp':'1230',
    'stenungsund':'1444','stockholm':'0180','storfors':'1760',
    'storuman':'2421','strängnäs':'0428','strömstad':'1486',
    'strömsund':'2313','sundbyberg':'0183','sundsvall':'2281',
    'sunne':'1766','surahammar':'1988','svalöv':'1233',
    'svedala':'1267','svenljunga':'1256','säffle':'1785',
    'säter':'2082','sävsjö':'0684','söderhamn':'2182',
    'söderköping':'0582','södertälje':'0181','sölvesborg':'1085',
    'tanum':'1435','tibro':'1492','tidaholm':'1491',
    'tierp':'0360','timrå':'2262','tingsryd':'0763',
    'tjörn':'1419','tomelilla':'1293','torsby':'1767',
    'torsås':'0864','tranemo':'1452','tranås':'0687',
    'trelleborg':'1285','trollhättan':'1488','trosa':'0486',
    'tyresö':'0138','täby':'0160','töreboda':'1493',
    'uddevalla':'1485','ulricehamn':'1491','umeå':'2480',
    'upplands väsby':'0114','upplands-bro':'0139','uppvidinge':'0760',
    'uppsala':'0380',
    'vadstena':'0563','vaggeryd':'0643','vallentuna':'0115',
    'vansbro':'2021','vara':'1497','varberg':'1383',
    'vaxholm':'0187','vellinge':'1233','vetlanda':'0683',
    'vilhelmina':'2422','vimmerby':'0884','vindeln':'2404',
    'vingåker':'0428','vänersborg':'1487','vännäs':'2403',
    'värmdö':'0120','värnamo':'0685','västervik':'0883',
    'västerås':'1980','växjö':'0780','ydre':'0561',
    'ystad':'1293','älmhult':'0765','älvdalen':'2029',
    'älvkarleby':'0381','älvsbyn':'2560','ängelholm':'1292',
    'åmål':'1462','åre':'2321','åsele':'2460','åstorp':'1277',
    'åtvidaberg':'0561','öckerö':'1421','ödeshög':'0563',
    'örebro':'1880','örkelljunga':'1277','örnsköldsvik':'2284',
    'östersund':'2380','österåker':'0117','östhammar':'0382',
    'östra göinge':'1293','överkalix':'2585','överkalelix':'2585',
    'överluleå':'2580',
}
for k, v in _kk.items():
    KOMMUNKOD[k] = v

def norm(s):
    s = s.lower()
    s = re.sub(r'[,\.\-–—()/]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

STOPWORDS = {
    'av','i','för','med','och','eller','på','om','från','till','inom','efter',
    'samt','vid','den','det','de','en','ett','är','som','år','under',
    'skolan','skola','gymnasiet','gymnasieskolan','grundskolan','förskolan',
    'skolinspektionen','huvud','huvudman','kommunens','kommunal','fsk',
    'gymnasieskola','grundskola','förskola','komvux','vuxenutbildning',
    'tematisk','planerad','regelbunden','anmälan','tillsyn','ansökan',
    'uppföljning','granskning','beslut','angående','avseende','ärende',
    'inkl','hm','läsåret','läsår','anpassad','nationellt','godkänd',
}

def sig_words(s, min_len=4):
    return [w for w in norm(s).split() if w not in STOPWORDS and len(w) >= min_len]

# Inverted index
word_to_schools = defaultdict(list)
for sc in schools:
    for w in sig_words(sc['name']):
        word_to_schools[w].append(sc)

COMMUNE_PAT = re.compile(r'i\s+([\wÅÄÖåäö]+(?:\s+[\wÅÄÖåäö]+)?)\s+(?:kommuns?|stad)\b', re.IGNORECASE)

def get_area_codes(rec):
    areas = set()
    kommun = rec.get('kommun', '').lower()
    if kommun in KOMMUNKOD:
        areas.add(KOMMUNKOD[kommun])
    subj = rec.get('subject', '')
    m = COMMUNE_PAT.search(subj)
    if m:
        nm = m.group(1).lower()
        if nm in KOMMUNKOD:
            areas.add(KOMMUNKOD[nm])
    return areas

def find_in_area(subj, area_codes):
    subj_words = set(sig_words(subj))
    if not subj_words:
        return None
    candidates = defaultdict(int)
    for w in subj_words:
        for sc in word_to_schools.get(w, []):
            candidates[sc['code']] += 1
    matches = []
    for code, _ in candidates.items():
        sc = sc_by_code[code]
        sc_words = set(sig_words(sc['name']))
        if sc_words and sc_words.issubset(subj_words):
            matches.append(sc)
    in_area = [sc for sc in matches if sc['areaCode'] in area_codes]
    return in_area[0]['code'] if in_area else None

fixed = 0
cleared = 0

for rec in data:
    code = rec.get('skolkod')
    if not code or not rec.get('kommun'):
        continue
    sc = sc_by_code.get(code)
    if not sc:
        continue

    area_codes = get_area_codes(rec)
    if not area_codes:
        continue  # don't know the right area, leave as-is

    if sc['areaCode'] in area_codes:
        continue  # already correct

    # Wrong area — try to fix
    subj = rec.get('subject', '')
    best = find_in_area(subj, area_codes)
    if best and best != code:
        rec['skolkod'] = best
        fixed += 1
    else:
        del rec['skolkod']
        cleared += 1

print(f'Fixed: {fixed}, Cleared: {cleared}')
json.dump(data, open('data.json', 'w', encoding='utf-8'), ensure_ascii=False)
print('Saved.')
