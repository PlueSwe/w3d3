#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_docids.py — fullständighetskontroll av SIRIS docID-rymden.

Katalogen från Skolinspektionens API (tools/catalog_crawl.py) är den primära
och auktoritativa källan. Den har dock en känd blind fläck: skolenheter som
lagts ner listas inte under `schools/current`, och myndighetens egen sida
hänvisar uttryckligen äldre beslut vidare till Skolverkets webbplats.

Det här verktyget sveper docID-rymden och hittar dokument som ingen katalognod
pekar på. Det är en KOMPLETTERING, inte en ersättning: kör katalogen först.

Funna dokument skrivs in i samma katalog- och nedladdningsfiler som övriga, med
`found_via_kind = "sweep"`, så att resten av kedjan (build_index, verify) inte
behöver veta varifrån ett dokument kom.

Svepningen är återupptagbar per docID-block och loggar varje prövat ID, så att
luckor är synliga i efterhand — till skillnad från den ursprungliga
scan_siris.py där misslyckade ID föll bort tyst.

Användning:
    python tools/sweep_docids.py --probe            # kartlägg rymdens gränser
    python tools/sweep_docids.py --from 600000 --to 670000
    python tools/sweep_docids.py --around-catalog   # svep katalogens intervall
    python tools/sweep_docids.py --status           # hur långt har vi kommit
"""

from __future__ import annotations

import argparse
import hashlib
import os
import queue
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_JSONL, INDEX_DIR, PDF_DIR, SIRIS_FILE,
    JsonlWriter, RateLimiter, classify_document, ensure_dirs, fmt_bytes,
    find_dnrs, http_get_retry, pdf_path, read_jsonl, setup_logging, utcnow,
)

SWEEP_JSONL = os.path.join(INDEX_DIR, "sweep.jsonl")
DOWNLOADS_JSONL = os.path.join(INDEX_DIR, "downloads.jsonl")

log = None

# Ett dokument räknas som Skolinspektionens om texten innehåller ett
# diarienummer på formen SI YYYY:NNNN, eller myndighetens namn.
_SI_MARKER = re.compile(r"Skolinspektionen", re.IGNORECASE)


def looks_like_pdf(b: bytes) -> bool:
    return b[:1024].lstrip()[:5] == b"%PDF-"


def quick_text(body: bytes, limit: int = 3 << 20) -> str:
    """
    Snabb texttitt utan att skriva till disk: avkodar Flate-strömmar och
    plockar textliteraler. Räcker för att se om ett dokument är
    Skolinspektionens och vilket diarienummer det bär.
    """
    import zlib
    out: list[str] = []
    data = body[:limit]
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end]
        dec = None
        for cand in (raw, raw.strip(b"\r\n")):
            try:
                dec = zlib.decompress(cand)
                break
            except zlib.error:
                continue
        if dec is None:
            continue
        for lit in re.findall(rb"\((?:\\.|[^\\()])*\)", dec):
            s = lit[1:-1].replace(b"\\(", b"(").replace(b"\\)", b")")
            out.append(s.decode("latin-1", "replace"))
        if sum(len(x) for x in out) > 400_000:
            break
    return "".join(out)


def probe(limiter, args) -> None:
    """Kartlägger var i docID-rymden det finns dokument."""
    log.info("Kartlägger docID-rymden (glesa stickprov) ...")
    lo, hi, step = args.probe_from, args.probe_to, args.probe_step
    hits = []
    for did in range(lo, hi + 1, step):
        status, body, ct, err, _ = http_get_retry(
            SIRIS_FILE.format(docid=did), limiter, log, tries=2,
            timeout=args.timeout)
        is_pdf = status == 200 and body and looks_like_pdf(body)
        if is_pdf:
            hits.append(did)
        log.info("  docID %-8d status=%-4s %-18s %s",
                 did, status, ct or "-", "PDF" if is_pdf else "")
    if hits:
        log.info("Träffar mellan %d och %d (steg %d): %d av %d",
                 lo, hi, step, len(hits), len(range(lo, hi + 1, step)))
        log.info("Lägsta träff: %d   Högsta träff: %d", min(hits), max(hits))
    else:
        log.info("Inga träffar i intervallet.")


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(
        description="Sveper SIRIS docID-rymden efter dokument katalogen missar.")
    ap.add_argument("--from", dest="lo", type=int, default=0)
    ap.add_argument("--to", dest="hi", type=int, default=0)
    ap.add_argument("--around-catalog", action="store_true",
                    help="svep katalogens min–max docID plus marginal")
    ap.add_argument("--margin", type=int, default=20000,
                    help="marginal utanför katalogens intervall (standard 20000)")
    ap.add_argument("--probe", action="store_true",
                    help="glesa stickprov för att hitta rymdens gränser")
    ap.add_argument("--probe-from", type=int, default=500000)
    ap.add_argument("--probe-to", type=int, default=700000)
    ap.add_argument("--probe-step", type=int, default=10000)
    ap.add_argument("--status", action="store_true",
                    help="visa hur mycket av rymden som svepts")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--keep-non-si", action="store_true",
                    help="behåll även dokument som inte är Skolinspektionens")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("sweep", args.verbose)

    catalog_ids = {r["docid"] for r in read_jsonl(CATALOG_JSONL) if r.get("docid")}
    swept: dict[int, dict] = {}
    for r in read_jsonl(SWEEP_JSONL):
        if r.get("docid") is not None:
            swept[r["docid"]] = r

    if args.status:
        print(f"Katalog:  {len(catalog_ids)} docID")
        if catalog_ids:
            print(f"          intervall {min(catalog_ids)}–{max(catalog_ids)}")
        print(f"Svepta:   {len(swept)} docID")
        if swept:
            found = [d for d, r in swept.items() if r.get("is_pdf")]
            si = [d for d, r in swept.items() if r.get("is_skolinspektionen")]
            newf = [d for d in si if d not in catalog_ids]
            print(f"          intervall {min(swept)}–{max(swept)}")
            print(f"          PDF: {len(found)}   Skolinspektionen: {len(si)}")
            print(f"          NYA utanför katalogen: {len(newf)}")
        return 0

    limiter = RateLimiter(args.delay)

    if args.probe:
        probe(limiter, args)
        return 0

    lo, hi = args.lo, args.hi
    if args.around_catalog:
        if not catalog_ids:
            log.error("Katalogen är tom. Kör tools/catalog_crawl.py först.")
            return 2
        lo = max(1, min(catalog_ids) - args.margin)
        hi = max(catalog_ids) + args.margin
        log.info("Katalogens intervall: %d–%d", min(catalog_ids), max(catalog_ids))
    if not lo or not hi or hi < lo:
        log.error("Ange --from och --to, eller --around-catalog, eller --probe.")
        return 2

    todo = [d for d in range(lo, hi + 1) if d not in swept]
    log.info("=" * 72)
    log.info("Svepning %d–%d – logg: %s", lo, hi, logfile)
    log.info("  redan svepta i intervallet: %d",
             sum(1 for d in range(lo, hi + 1) if d in swept))
    log.info("  att pröva:                  %d", len(todo))
    log.info("  beräknad tid:               %.1f h",
             len(todo) * args.delay / 3600)
    log.info("=" * 72)
    if not todo:
        log.info("Intervallet är redan helt svept.")
        return 0

    sweep_w = JsonlWriter(SWEEP_JSONL)
    cat_w = JsonlWriter(CATALOG_JSONL)
    dl_w = JsonlWriter(DOWNLOADS_JSONL)

    q: queue.Queue = queue.Queue()
    for d in todo:
        q.put(d)

    c = {"done": 0, "pdf": 0, "si": 0, "new": 0, "miss": 0, "err": 0, "bytes": 0}
    lock = threading.Lock()
    stop = threading.Event()
    total = len(todo)
    started = time.time()

    def worker():
        while not stop.is_set():
            try:
                did = q.get_nowait()
            except queue.Empty:
                return
            url = SIRIS_FILE.format(docid=did)
            status, body, ct, err, attempts = http_get_retry(
                url, limiter, log, tries=args.max_retries, timeout=args.timeout)

            rec = {"docid": did, "http_status": status, "content_type": ct,
                   "size": len(body) if body else 0, "attempts": attempts,
                   "error": err, "swept_at": utcnow(),
                   "is_pdf": False, "is_skolinspektionen": False,
                   "in_catalog": did in catalog_ids, "dnrs": []}

            if status == 200 and body and looks_like_pdf(body):
                rec["is_pdf"] = True
                text = quick_text(body)
                dnrs = find_dnrs(text)
                rec["dnrs"] = dnrs
                rec["is_skolinspektionen"] = bool(dnrs) or bool(_SI_MARKER.search(text))

                is_new = rec["is_skolinspektionen"] and did not in catalog_ids
                should_keep = (rec["is_skolinspektionen"] or args.keep_non_si)

                if should_keep:
                    dst = pdf_path(did)
                    if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
                        os.makedirs(PDF_DIR, exist_ok=True)
                        tmp = dst + ".part"
                        with open(tmp, "wb") as f:
                            f.write(body)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, dst)
                    dl_w.write({
                        "docid": did, "document_id": f"siris-{did}",
                        "local_path": f"pdf/siris-{did}.pdf",
                        "filename": f"siris-{did}.pdf",
                        "source_url": url, "http_status": status,
                        "mime_type": ct or "application/pdf",
                        "file_size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "download_status": "ok", "error_message": "",
                        "downloaded_at": utcnow(), "attempts": attempts,
                    })
                if is_new:
                    # Titel finns inte i sveptläge; klassificera på texten i stället.
                    cat_w.write({
                        "docid": did, "title": "", "raw_title": "",
                        "document_type": classify_document(text[:4000]),
                        "year": "", "gransknomr": "", "source_url": url,
                        "found_via_kind": "sweep", "found_via_code": "",
                        "found_via_name": "docID-svepning",
                        "found_at": utcnow(),
                    })

            sweep_w.write(rec)
            with lock:
                c["done"] += 1
                if rec["is_pdf"]:
                    c["pdf"] += 1
                    c["bytes"] += rec["size"]
                    if rec["is_skolinspektionen"]:
                        c["si"] += 1
                        if not rec["in_catalog"]:
                            c["new"] += 1
                elif status == 0:
                    c["err"] += 1
                else:
                    c["miss"] += 1
                n = c["done"]
            if n % 500 == 0 or n == total:
                el = time.time() - started
                rate = n / el if el else 0
                log.info("SVEP %d/%d (%.1f%%) – %.1f/s – ETA %.1f h – "
                         "pdf=%d si=%d NYA=%d tomma=%d fel=%d – %s",
                         n, total, 100 * n / total, rate,
                         ((total - n) / rate / 3600) if rate else 0,
                         c["pdf"], c["si"], c["new"], c["miss"], c["err"],
                         fmt_bytes(c["bytes"]))
            q.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, args.workers))]
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log.warning("Avbrott – svepningen kan återupptas med samma kommando.")
        stop.set()
        for t in threads:
            t.join(timeout=args.timeout + 5)
    finally:
        sweep_w.close()
        cat_w.close()
        dl_w.close()

    log.info("=" * 72)
    log.info("Svepning klar på %.1f h", (time.time() - started) / 3600)
    log.info("  prövade docID:        %d", c["done"])
    log.info("  PDF-svar:             %d", c["pdf"])
    log.info("  Skolinspektionens:    %d", c["si"])
    log.info("  NYA (ej i katalogen): %d", c["new"])
    log.info("  tomma/ej PDF:         %d", c["miss"])
    log.info("  nätverksfel:          %d", c["err"])
    log.info("  hämtat:               %s", fmt_bytes(c["bytes"]))
    if c["new"]:
        log.info("Kör build_index.py igen för att koppla de nya dokumenten.")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
