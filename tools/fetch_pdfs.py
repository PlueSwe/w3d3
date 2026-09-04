#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_pdfs.py — laddar ner samtliga dokument i katalogen till ett platt PDF-lager.

Alla dokument hamnar på SAMMA ställe:

    <SIRIS_ROOT>/pdf/siris-<docID>.pdf

Platt struktur är ett medvetet val. docID är dokumentets identitet i källan och
är globalt unikt, vilket ger tre fördelar: samma dokument kan inte hamna i två
kataloger, ett dokument som är kopplat till flera ärenden lagras bara en gång,
och sökvägen kan mappas rakt av till en S3-nyckel i Etapp 5. Kopplingen mellan
ärende och fil ligger i index/, inte i katalogstrukturen.

Nedladdningsstatus per dokument skrivs till:

    <SIRIS_ROOT>/index/downloads.jsonl

Användning:
    python tools/fetch_pdfs.py                    # ladda ner allt som saknas
    python tools/fetch_pdfs.py --limit 50         # provkörning
    python tools/fetch_pdfs.py --retry-failed     # gör om misslyckade
    python tools/fetch_pdfs.py --import-from <dir>   # ta in redan hämtade PDF:er
"""

from __future__ import annotations

import argparse
import hashlib
import os
import queue
import re
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_JSONL, INDEX_DIR, PDF_DIR, SIRIS_FILE,
    MIME_BY_KIND,
    JsonlWriter, RateLimiter, classify_document, doctype_priority, document_id,
    ensure_dirs, fmt_bytes, http_get_retry, pdf_path, read_jsonl, setup_logging,
    sniff_filetype, utcnow,
)

DOWNLOADS_JSONL = os.path.join(INDEX_DIR, "downloads.jsonl")

ST_OK = "ok"
ST_EXISTS = "already_archived"
ST_IMPORTED = "imported"
ST_NOT_PDF = "ok_not_pdf"
ST_HTTP = "http_error"
ST_NET = "network_error"
ST_EMPTY = "empty_response"
SUCCESS = {ST_OK, ST_EXISTS, ST_IMPORTED}

log = None


def looks_like_pdf(data: bytes) -> bool:
    return data[:1024].lstrip()[:5] == b"%PDF-"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_docids_from_catalog() -> dict[int, dict]:
    """Slår ihop katalograder till en post per unikt docID."""
    docs: dict[int, dict] = {}
    for row in read_jsonl(CATALOG_JSONL):
        did = row.get("docid")
        if did is None:
            continue
        cur = docs.get(did)
        if cur is None:
            docs[did] = dict(row)
        else:
            # Behåll den rikaste metadatan; titeln är densamma per docID.
            for k, v in row.items():
                if v and not cur.get(k):
                    cur[k] = v
    # Klassificera om ur titeln i stället för att lita på det värde som skrevs
    # vid crawl-tillfället, så att en förbättrad klassificerare får genomslag
    # utan att katalogen behöver hämtas om.
    for rec in docs.values():
        title = rec.get("raw_title") or rec.get("title") or ""
        if title:
            rec["document_type"] = classify_document(title)
    return docs


def import_existing(src_dir: str, docs: dict[int, dict], writer: JsonlWriter,
                    state: dict) -> int:
    """
    Tar in PDF:er som redan hämtats i en tidigare körning (t.ex. det första
    arkivet under archive/) i stället för att ladda ner dem igen.
    Filer kopieras, originalet lämnas orört.
    """
    n = 0
    for dirpath, _dirs, files in os.walk(src_dir):
        for fn in files:
            m = re.match(r"^siris-(\d+)\.pdf$", fn, re.IGNORECASE)
            if not m:
                continue
            did = int(m.group(1))
            src = os.path.join(dirpath, fn)
            dst = pdf_path(did)
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                continue
            try:
                if os.path.getsize(src) == 0:
                    continue
                os.makedirs(PDF_DIR, exist_ok=True)
                tmp = dst + ".part"
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            except OSError as exc:
                log.warning("kunde inte kopiera %s: %s", src, exc)
                continue

            with open(dst, "rb") as f:
                head = f.read(65536)
            kind, _ext, _isdoc = sniff_filetype(head)
            rec = dict(docs.get(did, {"docid": did}))
            rec.update({
                "document_id": document_id(did),
                "docid": did,
                "local_path": os.path.relpath(dst, os.path.dirname(PDF_DIR)).replace("\\", "/"),
                "filename": os.path.basename(dst),
                "file_size": os.path.getsize(dst),
                "sha256": sha256_file(dst),
                "file_kind": kind,
                "mime_type": MIME_BY_KIND.get(kind, "application/octet-stream"),
                "http_status": "",
                "download_status": ST_IMPORTED,
                "error_message": f"importerad från {src_dir}",
                "downloaded_at": utcnow(),
                "attempts": 0,
            })
            writer.write(rec)
            state[did] = rec
            n += 1
            if n % 250 == 0:
                log.info("  importerat %d filer ...", n)
    return n


def download_one(did: int, meta: dict, limiter: RateLimiter, args) -> dict:
    dst = pdf_path(did)
    rec = dict(meta)
    rec.update({
        "document_id": document_id(did),
        "docid": did,
        "local_path": f"pdf/siris-{did}.pdf",
        "filename": f"siris-{did}.pdf",
        "source_url": SIRIS_FILE.format(docid=did),
        "downloaded_at": utcnow(),
    })

    # Befintlig fil skrivs aldrig över utan kontroll.
    if os.path.exists(dst):
        size = os.path.getsize(dst)
        if size > 0:
            with open(dst, "rb") as f:
                head = f.read(65536)
            kind, _ext, _isdoc = sniff_filetype(head)
            rec.update({
                "file_size": size,
                "sha256": sha256_file(dst),
                "file_kind": kind,
                "mime_type": MIME_BY_KIND.get(kind, "application/octet-stream"),
                "http_status": "",
                "download_status": ST_EXISTS,
                "error_message": "",
                "attempts": 0,
            })
            return rec

    status, body, ctype, err, attempts = http_get_retry(
        rec["source_url"], limiter, log, tries=args.max_retries,
        timeout=args.timeout, base=args.backoff_base, cap=args.backoff_max,
    )
    rec["http_status"] = status
    rec["attempts"] = attempts
    rec["mime_type"] = ctype

    if status != 200:
        rec["download_status"] = ST_NET if status == 0 else ST_HTTP
        rec["error_message"] = err or f"status {status}"
        rec["file_size"] = ""
        rec["sha256"] = ""
        return rec
    if not body:
        rec["download_status"] = ST_EMPTY
        rec["error_message"] = "HTTP 200 men tomt svar"
        rec["file_size"] = 0
        rec["sha256"] = ""
        return rec

    # Filtypen avgörs på innehållet, inte på headern. SIRIS publicerar äldre
    # beslut som Word — de är giltiga handlingar, inte misslyckade hämtningar.
    kind, ext, is_document = sniff_filetype(body, ctype)
    rec["file_kind"] = kind
    rec["mime_type"] = MIME_BY_KIND.get(kind, ctype or "application/octet-stream")

    if not is_document:
        dst = os.path.join(PDF_DIR, f"siris-{did}{ext}")
        rec["local_path"] = f"pdf/siris-{did}{ext}"
        rec["filename"] = f"siris-{did}{ext}"
        rec["download_status"] = ST_NOT_PDF
        rec["error_message"] = (f"HTTP 200 men svaret är ingen handling "
                                f"(igenkänt som {kind}, "
                                f"content-type={ctype or 'okänd'})")
    else:
        dst = os.path.join(PDF_DIR, f"siris-{did}{ext}")
        rec["local_path"] = f"pdf/siris-{did}{ext}"
        rec["filename"] = f"siris-{did}{ext}"
        rec["download_status"] = ST_OK
        rec["error_message"] = ("" if kind == "pdf"
                                else f"handling i {kind}-format, inte PDF")

    os.makedirs(PDF_DIR, exist_ok=True)
    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)

    rec["file_size"] = len(body)
    rec["sha256"] = hashlib.sha256(body).hexdigest()
    return rec


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(description="Laddar ner katalogens dokument till ett platt PDF-lager.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--max-retries", type=int, default=4)
    ap.add_argument("--backoff-base", type=float, default=2.0)
    ap.add_argument("--backoff-max", type=float, default=60.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--import-from", default="",
                    help="katalog med redan hämtade siris-<id>.pdf att kopiera in")
    ap.add_argument("--import-only", action="store_true",
                    help="importera bara, ladda inte ner något")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("fetch", args.verbose)
    log.info("=" * 72)
    log.info("PDF-hämtning startad – logg: %s", logfile)
    log.info("PDF-lager: %s", PDF_DIR)
    log.info("=" * 72)

    docs = unique_docids_from_catalog()
    if not docs:
        log.error("Katalogen är tom. Kör tools/catalog_crawl.py först.")
        return 2
    log.info("Katalog: %d unika dokument", len(docs))

    state: dict[int, dict] = {}
    for rec in read_jsonl(DOWNLOADS_JSONL):
        if rec.get("docid") is not None:
            state[rec["docid"]] = rec
    if state:
        log.info("Resume: %d tidigare nedladdningsposter", len(state))

    writer = JsonlWriter(DOWNLOADS_JSONL)

    if args.import_from:
        src = os.path.abspath(args.import_from)
        if not os.path.isdir(src):
            log.error("--import-from: katalogen finns inte: %s", src)
            return 2
        log.info("Importerar befintliga PDF:er från %s ...", src)
        n = import_existing(src, docs, writer, state)
        log.info("  %d filer importerade", n)
        if args.import_only:
            writer.close()
            log.info("--import-only: ingen nedladdning utförs.")
            return 0

    todo: list[int] = []
    # Beslut före enkätresultat: en avbruten körning ska ändå ha säkrat det
    # som Beslutstjänsten bygger på.
    order = sorted(docs, key=lambda d: (doctype_priority(docs[d].get("document_type")),
                                        d))
    for did in order:
        prev = state.get(did)
        if prev is not None:
            st = prev.get("download_status")
            if st in SUCCESS:
                p = pdf_path(did)
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    continue
                log.warning("%s: manifest säger klar men filen saknas – laddas om",
                            document_id(did))
            elif not args.retry_failed:
                continue
        todo.append(did)

    if args.limit and len(todo) > args.limit:
        log.info("--limit %d (av %d)", args.limit, len(todo))
        todo = todo[: args.limit]

    log.info("Att hämta: %d dokument", len(todo))
    prio = {}
    for did in todo:
        prio[docs.get(did, {}).get("document_type") or "okant"] =             prio.get(docs.get(did, {}).get("document_type") or "okant", 0) + 1
    for t, n in sorted(prio.items(), key=lambda x: doctype_priority(x[0])):
        log.info("    %-30s %7d", t, n)
    if not todo:
        writer.close()
        log.info("Inget att göra.")
        return 0

    limiter = RateLimiter(args.delay)
    q: queue.Queue = queue.Queue()
    for did in todo:
        q.put(did)

    counters = {"done": 0, "ok": 0, "exists": 0, "failed": 0, "bytes": 0}
    clock = threading.Lock()
    stop = threading.Event()
    total = len(todo)
    started = time.time()

    def worker():
        while not stop.is_set():
            try:
                did = q.get_nowait()
            except queue.Empty:
                return
            try:
                rec = download_one(did, docs.get(did, {"docid": did}), limiter, args)
            except Exception as exc:
                log.exception("%s: oväntat fel", document_id(did))
                rec = {"docid": did, "document_id": document_id(did),
                       "download_status": ST_NET, "downloaded_at": utcnow(),
                       "error_message": f"internt fel: {type(exc).__name__}: {exc}"}
            writer.write(rec)
            with clock:
                counters["done"] += 1
                st = rec.get("download_status")
                if st == ST_OK:
                    counters["ok"] += 1
                elif st in (ST_EXISTS, ST_IMPORTED):
                    counters["exists"] += 1
                else:
                    counters["failed"] += 1
                if isinstance(rec.get("file_size"), int):
                    counters["bytes"] += rec["file_size"]
                n = counters["done"]
            if n % 200 == 0 or n == total:
                el = time.time() - started
                rate = n / el if el else 0
                log.info("FRAMSTEG %d/%d (%.1f%%) – %.1f dok/s – ETA %.0f min – "
                         "ny=%d fanns=%d fel=%d – %s",
                         n, total, 100 * n / total, rate,
                         ((total - n) / rate / 60) if rate else 0,
                         counters["ok"], counters["exists"], counters["failed"],
                         fmt_bytes(counters["bytes"]))
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
        log.warning("Avbrott – kör om samma kommando för att återuppta.")
        stop.set()
        for t in threads:
            t.join(timeout=args.timeout + 5)
    finally:
        writer.close()

    log.info("=" * 72)
    log.info("Klar på %.1f min", (time.time() - started) / 60)
    log.info("  nya:         %d", counters["ok"])
    log.info("  fanns redan: %d", counters["exists"])
    log.info("  fel:         %d", counters["failed"])
    log.info("  hämtat:      %s", fmt_bytes(counters["bytes"]))
    log.info("Nästa steg: python tools/build_index.py")
    log.info("=" * 72)
    return 0 if counters["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
