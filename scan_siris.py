#!/usr/bin/env python3
"""
Scan SIRIS docIDs to build dno->docID mapping for Skolinspektionen beslut.
Usage: python scan_siris.py [start] [end]
Default range: 630000-680000 (covers ~2023-2026 decisions)
Output: beslut.json  {dno: docID, ...}
"""

import sys, json, os, re, tempfile, subprocess, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

OUTPUT = os.path.join(os.path.dirname(__file__), 'beslut.json')
START = int(sys.argv[1]) if len(sys.argv) > 1 else 630000
END   = int(sys.argv[2]) if len(sys.argv) > 2 else 680000
WORKERS = 8
DELAY = 0.05  # seconds between requests per worker

def fetch_dno(docid):
    url = f'https://siris.skolverket.se/siris/ris.openfile?docID={docid}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (research)'})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read()
        if not data.startswith(b'%PDF'):
            return docid, None
        tmp = tempfile.mktemp(suffix='.pdf')
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            result = subprocess.run(
                ['pdftotext', '-l', '1', tmp, '-'],
                capture_output=True, timeout=10
            )
            text = result.stdout.decode('utf-8', 'replace')
        finally:
            try: os.unlink(tmp)
            except: pass
        m = re.search(r'Dnr\s+(SI\s*\d{4}:\d+)', text)
        if m:
            dno = m.group(1).replace(' ', ' ').strip()
            return docid, dno
        return docid, None
    except Exception:
        return docid, 'ERR'


def main():
    # Load existing results
    existing = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        print(f'Loaded {len(existing)} existing entries from beslut.json')

    # Invert to find already-scanned docIDs
    scanned_docids = set(existing.values())

    total = END - START
    done = 0
    hits = 0
    errors = 0
    save_interval = 500

    mapping = dict(existing)  # dno -> docID

    docids = [d for d in range(START, END) if d not in scanned_docids]
    print(f'Scanning {len(docids)} docIDs ({START}-{END}), {WORKERS} workers...')
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_dno, d): d for d in docids}
        for future in as_completed(futures):
            docid, dno = future.result()
            done += 1
            if dno and not dno.startswith('ERR'):
                mapping[dno] = docid
                hits += 1
                elapsed = time.time() - start_time
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(docids) - done) / rate if rate > 0 else 0
                print(f'  [{done}/{len(docids)}] {docid} -> {dno}  '
                      f'({hits} hits, {rate:.1f}/s, ETA {eta/60:.0f}min)', flush=True)
            elif dno == 'ERR':
                errors += 1

            if done % save_interval == 0:
                with open(OUTPUT, 'w', encoding='utf-8') as f:
                    json.dump(mapping, f, ensure_ascii=False)
                print(f'  Saved {len(mapping)} entries ({hits} new hits so far)', flush=True)

            time.sleep(DELAY)

    # Final save
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    print(f'\nDone! {hits} SI beslut found out of {len(docids)} scanned.')
    print(f'Errors: {errors}')
    print(f'Total in beslut.json: {len(mapping)}')


if __name__ == '__main__':
    main()
