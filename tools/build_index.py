#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_index.py — kopplar varje PDF till rätt ärende och bygger indexet.

Kopplingen ärende↔dokument härleds ur dokumentets EGET innehåll: varje PDF
textextraheras och alla diarienummer på formen `SI YYYY:NNNN` plockas ut.
Det är den enda källan som faktiskt bevisar sambandet — katalogen från
Skolinspektionens API innehåller inga diarienummer, och en gissning utifrån
skolnamn eller datum vore inte spårbar.

Ett dokument kan ge upphov till flera kopplingar (samlingsbeslut som räknar upp
flera ärenden), och ett ärende kan ha flera dokument (beslut + uppföljning).
Kopplingstabellen är därför många-till-många.

Output (under SIRIS_ROOT/index/):

    documents.jsonl / documents.csv   en rad per dokument, med metadata + sha256
    case_documents.csv / .jsonl       KOPPLINGSTABELLEN ärende ↔ dokument
    cases.csv                         ärenden från data.json, med dokumenträkning
    text/<document_id>.txt            extraherad text (med --keep-text)

Textextraktion kräver `pdftotext` (poppler/xpdf) i PATH. Saknas den används en
inbyggd reservtolk som klarar okomprimerad och Flate-kodad text, vilket räcker
för att hitta diarienumret i de flesta beslut.

Användning:
    python tools/build_index.py
    python tools/build_index.py --workers 8 --keep-text
    python tools/build_index.py --force        # extrahera om allt
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from office_text import extract_office  # noqa: E402

from siris_common import (  # noqa: E402
    BESLUT_JSON, CASE_DOCS_CSV, CASE_DOCS_JSONL, CASES_CSV, CATALOG_JSONL,
    DATA_JSON, DOCUMENTS_CSV, DOCUMENTS_JSONL, INDEX_DIR, PDF_DIR, ROOT,
    SIRIS_FILE, SKOLOR_JSON,
    JsonlWriter, ensure_dirs, find_dnrs_labeled, find_legacy_dnrs, fmt_bytes,
    load_json_file,
    normalize_dno, read_jsonl, setup_logging, utcnow,
)

DOWNLOADS_JSONL = os.path.join(INDEX_DIR, "downloads.jsonl")
EXTRACT_JSONL = os.path.join(INDEX_DIR, "extracted.jsonl")
TEXT_DIR = os.path.join(INDEX_DIR, "text")

log = None
HAS_PDFTOTEXT = shutil.which("pdftotext") is not None

DOC_FIELDS = [
    "document_id", "docid", "title", "document_type", "year", "gransknomr",
    "source_url", "local_path", "filename", "mime_type", "file_size", "sha256",
    "http_status", "download_status", "downloaded_at", "error_message",
    "mentioned_diarienummer",
    "county_code", "county_name", "org_code", "org_name", "org_group",
    "school_code", "school_name",
    "dnr_count", "diarienummer_list", "primary_diarienummer",
    "legacy_diarienummer", "legacy_diarienummer_list",
    "text_chars", "text_status", "in_beslut_json",
]

LINK_FIELDS = [
    "diarienummer", "document_id", "docid", "local_path", "filename",
    "document_type", "title", "year", "link_method", "link_type",
    "link_confidence", "is_primary", "dnr_position",
    "case_date", "case_typ", "case_subject", "case_kommun", "case_hauptman",
    "case_skolkod", "school_name", "org_name", "county_name",
    "file_size", "sha256", "source_url",
]

CASE_FIELDS = [
    "diarienummer", "date", "typ", "subject", "kommun", "hauptman", "skolkod",
    "document_count", "document_ids", "has_document",
]


# ──────────────────────────────────────────────────────────────────────────
#  Textextraktion
# ──────────────────────────────────────────────────────────────────────────


def extract_with_pdftotext(path: str, pages: int, timeout: int) -> tuple[str, str]:
    cmd = ["pdftotext", "-q"]
    if pages:
        cmd += ["-l", str(pages)]
    cmd += [path, "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        text = r.stdout.decode("utf-8", "replace")
        if text.strip():
            return text, "pdftotext"
        return text, "pdftotext_empty"
    except subprocess.TimeoutExpired:
        return "", "pdftotext_timeout"
    except Exception as exc:
        return "", f"pdftotext_error: {type(exc).__name__}"


_TJ_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")


def extract_fallback(path: str, max_bytes: int = 6 << 20) -> tuple[str, str]:
    """
    Reservtolk utan externa beroenden.

    Avkodar Flate-komprimerade strömmar och plockar ut textliteraler ur
    PDF:ens innehållsströmmar. Tillräckligt för att hitta 'Dnr SI YYYY:NNNN',
    inte tillräckligt för fulltextindexering.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
    except OSError as exc:
        return "", f"read_error: {exc}"

    chunks: list[str] = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end]
        for candidate in (raw, raw.strip(b"\r\n")):
            try:
                out = zlib.decompress(candidate)
                break
            except zlib.error:
                out = None
        if out is None:
            continue
        for lit in _TJ_RE.findall(out):
            s = lit[1:-1]
            s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            try:
                chunks.append(s.decode("latin-1", "replace"))
            except Exception:
                pass
    text = "".join(chunks)
    return text, "fallback" if text.strip() else "fallback_empty"


def extract_text(path: str, pages: int, timeout: int) -> tuple[str, str]:
    low = path.lower()
    if low.endswith(".docx"):
        return extract_docx(path)
    if low.endswith(".doc"):
        return extract_doc(path)
    if not low.endswith(".pdf"):
        return "", "unsupported_format"
    if HAS_PDFTOTEXT:
        text, status = extract_with_pdftotext(path, pages, timeout)
        if text.strip():
            return text, status
        # Skannade beslut utan textlager ger tom output. Reservtolken hjälper
        # inte där heller, men kan rädda fall där pdftotext kraschar.
        text2, status2 = extract_fallback(path)
        if text2.strip():
            return text2, status2
        return text, status
    return extract_fallback(path)


# ──────────────────────────────────────────────────────────────────────────
#  Steg 1: extrahera diarienummer ur varje PDF
# ──────────────────────────────────────────────────────────────────────────


def run_extraction(args, downloads: dict[int, dict]) -> dict[str, dict]:
    prev: dict[str, dict] = read_jsonl(EXTRACT_JSONL, key="document_id")
    if prev and not args.force:
        log.info("Resume: %d dokument redan textextraherade", len(prev))

    targets = []
    for did, rec in downloads.items():
        doc_id = rec.get("document_id") or f"siris-{did}"
        path = os.path.join(ROOT, str(rec.get("local_path", "")).replace("/", os.sep))
        if not path or not os.path.exists(path):
            continue
        if not path.lower().endswith((".pdf", ".doc", ".docx")):
            continue
        if not args.force and doc_id in prev:
            continue
        targets.append((doc_id, did, path))

    log.info("Textextraktion: %d dokument att bearbeta (%s)",
             len(targets), "pdftotext" if HAS_PDFTOTEXT else "inbyggd reservtolk")
    if not targets:
        return prev

    if args.keep_text:
        os.makedirs(TEXT_DIR, exist_ok=True)

    writer = JsonlWriter(EXTRACT_JSONL)
    q: queue.Queue = queue.Queue()
    for t in targets:
        q.put(t)

    counters = {"done": 0, "with_dnr": 0, "no_text": 0, "no_dnr": 0}
    clock = threading.Lock()
    total = len(targets)
    started = time.time()

    def worker():
        while True:
            try:
                doc_id, did, path = q.get_nowait()
            except queue.Empty:
                return
            try:
                text, status = extract_text(path, args.pages, args.pdf_timeout)
                labeled = find_dnrs_labeled(text)
                legacy = find_legacy_dnrs(text)
                rec = {
                    "document_id": doc_id, "docid": did,
                    "text_status": status, "text_chars": len(text),
                    "dnrs": [d for d, _k in labeled],
                    "dnrs_labeled": [[d, k] for d, k in labeled],
                    "legacy_dnrs": [[d, k] for d, k in legacy],
                    "extracted_at": utcnow(),
                }
                if args.keep_text and text.strip():
                    try:
                        with open(os.path.join(TEXT_DIR, f"{doc_id}.txt"),
                                  "w", encoding="utf-8") as f:
                            f.write(text)
                    except OSError as exc:
                        log.warning("%s: kunde inte spara text: %s", doc_id, exc)
            except Exception as exc:
                log.exception("%s: extraktionsfel", doc_id)
                rec = {"document_id": doc_id, "docid": did,
                       "text_status": f"error: {type(exc).__name__}",
                       "text_chars": 0, "dnrs": [], "extracted_at": utcnow()}
            writer.write(rec)
            prev[doc_id] = rec
            with clock:
                counters["done"] += 1
                # Räkna alla diarienummerserier, inte bara SI-formen. Äldre
                # dokument bär 'NNN-ÅÅÅÅ:NNNN' eller 'Dnr ÅÅÅÅ:NNNN'.
                if rec["dnrs"] or rec.get("legacy_dnrs"):
                    counters["with_dnr"] += 1
                elif rec["text_chars"] == 0:
                    counters["no_text"] += 1
                else:
                    counters["no_dnr"] += 1
                n = counters["done"]
            if n % 500 == 0 or n == total:
                el = time.time() - started
                rate = n / el if el else 0
                log.info("EXTRAKTION %d/%d (%.1f%%) – %.1f dok/s – ETA %.0f min – "
                         "med dnr=%d utan dnr=%d utan text=%d",
                         n, total, 100 * n / total, rate,
                         ((total - n) / rate / 60) if rate else 0,
                         counters["with_dnr"], counters["no_dnr"],
                         counters["no_text"])
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
        log.warning("Avbrott – extraktionen kan återupptas.")
        raise
    finally:
        writer.close()

    log.info("Extraktion klar: %d med diarienummer, %d utan, %d utan textlager",
             counters["with_dnr"], counters["no_dnr"], counters["no_text"])
    return prev


# ──────────────────────────────────────────────────────────────────────────
#  Steg 2: bygg index
# ──────────────────────────────────────────────────────────────────────────


def write_csv(path: str, fields: list[str], rows) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
            n += 1
    return n


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(
        description="Kopplar PDF:er till ärenden och bygger indexet.")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pages", type=int, default=3,
                    help="antal sidor att textextrahera per PDF (0 = hela, standard 3)")
    ap.add_argument("--pdf-timeout", type=int, default=30)
    ap.add_argument("--keep-text", action="store_true",
                    help="spara extraherad text under index/text/ (underlag för framtida RAG)")
    ap.add_argument("--force", action="store_true", help="extrahera om allt")
    ap.add_argument("--skip-extract", action="store_true",
                    help="bygg bara index av redan extraherad data")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("index", args.verbose)
    log.info("=" * 72)
    log.info("Indexbygge startat – logg: %s", logfile)
    log.info("=" * 72)

    # ── Källor ──
    downloads: dict[int, dict] = {}
    for rec in read_jsonl(DOWNLOADS_JSONL):
        if rec.get("docid") is not None:
            downloads[rec["docid"]] = rec
    log.info("Nedladdningar: %d poster", len(downloads))
    if not downloads:
        log.error("Inga nedladdningar hittades. Kör tools/fetch_pdfs.py först.")
        return 2

    catalog: dict[int, dict] = {}
    for row in read_jsonl(CATALOG_JSONL):
        did = row.get("docid")
        if did is None:
            continue
        cur = catalog.setdefault(did, {})
        for k, v in row.items():
            if v and not cur.get(k):
                cur[k] = v
    log.info("Katalog: %d unika dokument", len(catalog))

    # Diariet är primärkälla för ärenden: det täcker 2008 och framåt, medan
    # data.json börjar 2019. Utan det skulle varje koppling till ett äldre
    # ärende felaktigt rapporteras som omatchad.
    cases: dict[str, dict] = {}
    n_diarium = 0
    diarium_csv = os.path.join(INDEX_DIR, "diarium.csv")
    if os.path.exists(diarium_csv):
        with open(diarium_csv, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                dno = normalize_dno(r.get("dno_si") or r.get("diarienummer") or "")
                if not dno or dno in cases:
                    continue
                cases[dno] = {
                    "dno": dno,
                    "date": r.get("reg_datum", ""),
                    "subject": r.get("arendemening", ""),
                    "typ": r.get("arendetyp", ""),
                    "kommun": r.get("kommun", ""),
                    "hauptman": "",
                    "skolkod": "",
                    "_source": "diarium",
                }
                n_diarium += 1

    cases_raw = load_json_file(DATA_JSON)
    n_json = 0
    for c in cases_raw:
        dno = normalize_dno(c.get("dno", ""))
        if dno and dno not in cases:
            c = dict(c)
            c["_source"] = "data_json"
            cases[dno] = c
            n_json += 1
    log.info("Ärenden: %d (diarium %d, data.json %d)",
             len(cases), n_diarium, n_json)

    schools: dict[str, dict] = {}
    if os.path.exists(SKOLOR_JSON):
        try:
            for s in load_json_file(SKOLOR_JSON):
                if s.get("code"):
                    schools[str(s["code"])] = s
        except Exception as exc:
            log.warning("skolor.json kunde inte läsas: %s", exc)

    legacy_beslut: dict[str, int] = {}
    if os.path.exists(BESLUT_JSON):
        try:
            for k, v in load_json_file(BESLUT_JSON).items():
                try:
                    legacy_beslut[normalize_dno(k)] = int(v)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            log.warning("beslut.json kunde inte läsas: %s", exc)
    legacy_docids = set(legacy_beslut.values())
    log.info("beslut.json (gamla kopplingen): %d poster", len(legacy_beslut))

    # ── Extraktion ──
    if args.skip_extract:
        extracted = read_jsonl(EXTRACT_JSONL, key="document_id")
        log.info("--skip-extract: använder %d befintliga extraktioner", len(extracted))
    else:
        extracted = run_extraction(args, downloads)

    # ── Bygg dokumentindex ──
    log.info("Bygger dokumentindex ...")
    documents: list[dict] = []
    for did in sorted(downloads):
        dl = downloads[did]
        cat = catalog.get(did, {})
        ex = extracted.get(dl.get("document_id") or f"siris-{did}", {})
        labeled_raw = ex.get("dnrs_labeled")
        if labeled_raw is None:
            # Äldre extraktion utan etikettinformation: första numret antas eget.
            labeled_raw = [[d, "own" if i == 0 else "mentioned"]
                           for i, d in enumerate(ex.get("dnrs") or [])]
        labeled = [(normalize_dno(d), k) for d, k in labeled_raw]
        labeled = [(d, k) for d, k in labeled if d]
        dnrs = [d for d, _k in labeled]
        own_dnrs = [d for d, k in labeled if k == "own"]

        documents.append({
            "document_id": dl.get("document_id") or f"siris-{did}",
            "docid": did,
            "title": cat.get("title", ""),
            "document_type": cat.get("document_type", ""),
            "year": cat.get("year", ""),
            "gransknomr": cat.get("gransknomr", ""),
            "source_url": dl.get("source_url") or SIRIS_FILE.format(docid=did),
            "local_path": dl.get("local_path", ""),
            "filename": dl.get("filename", ""),
            "mime_type": dl.get("mime_type", ""),
            "file_size": dl.get("file_size", ""),
            "sha256": dl.get("sha256", ""),
            "http_status": dl.get("http_status", ""),
            "download_status": dl.get("download_status", ""),
            "downloaded_at": dl.get("downloaded_at", ""),
            "error_message": dl.get("error_message", ""),
            "county_code": cat.get("county_code", ""),
            "county_name": cat.get("county_name", ""),
            "org_code": cat.get("org_code", ""),
            "org_name": cat.get("org_name", ""),
            "org_group": cat.get("org_group", ""),
            "school_code": cat.get("school_code", ""),
            "school_name": cat.get("school_name", ""),
            "dnr_count": len(dnrs),
            "diarienummer_list": "; ".join(dnrs),
            "primary_diarienummer": own_dnrs[0] if own_dnrs else "",
            "legacy_diarienummer": legacy_own[0] if legacy_own else "",
            "legacy_diarienummer_list": "; ".join(d for d, _k in legacy_list),
            "mentioned_diarienummer": "; ".join(d for d, k in labeled
                                                if k == "mentioned"),
            "text_chars": ex.get("text_chars", ""),
            "text_status": ex.get("text_status", ""),
            "in_beslut_json": "ja" if did in legacy_docids else "nej",
            "_labeled": labeled,
        })

    # ── Kopplingstabell ──
    log.info("Bygger kopplingstabell ärende ↔ dokument ...")
    links: list[dict] = []
    docs_by_case: dict[str, list[str]] = {}

    for doc in documents:
        if doc["download_status"] not in ("ok", "already_archived", "imported"):
            continue
        for pos, (dno, kind) in enumerate(doc["_labeled"]):
            case = cases.get(dno)
            school = schools.get(str(case.get("skolkod", ""))) if case else None
            if kind == "own":
                conf = "high" if case else "unmatched_case"
            else:
                # Dokumentet hänvisar till ärendet men tillhör det inte.
                conf = "reference" if case else "reference_unmatched"
            links.append({
                "diarienummer": dno,
                "document_id": doc["document_id"],
                "docid": doc["docid"],
                "local_path": doc["local_path"],
                "filename": doc["filename"],
                "document_type": doc["document_type"],
                "title": doc["title"],
                "year": doc["year"],
                "link_method": "dnr_ur_pdf_text",
                "link_type": "own_dnr" if kind == "own" else "mentioned",
                # 'high' = numret står som dokumentets eget OCH ärendet finns i
                # diariet. 'reference' = dokumentet hänvisar bara till ärendet.
                "link_confidence": conf,
                "is_primary": "ja" if kind == "own" else "nej",
                "dnr_position": pos,
                "case_date": (case or {}).get("date", ""),
                "case_typ": (case or {}).get("typ", ""),
                "case_subject": (case or {}).get("subject", ""),
                "case_kommun": (case or {}).get("kommun", ""),
                "case_hauptman": (case or {}).get("hauptman", ""),
                "case_skolkod": (case or {}).get("skolkod", ""),
                "school_name": doc["school_name"] or (school or {}).get("name", ""),
                "org_name": doc["org_name"] or (school or {}).get("org", ""),
                "county_name": doc["county_name"],
                "file_size": doc["file_size"],
                "sha256": doc["sha256"],
                "source_url": doc["source_url"],
            })
            if kind == "own":
                docs_by_case.setdefault(dno, []).append(doc["document_id"])

    # Egna kopplingar först per ärende, sedan hänvisningar.
    links.sort(key=lambda r: (r["diarienummer"], r["link_type"] != "own_dnr",
                              r["document_id"]))

    # ── Ärendeindex ──
    case_rows = []
    for dno in sorted(cases):
        c = cases[dno]
        ids = sorted(set(docs_by_case.get(dno, [])))
        case_rows.append({
            "diarienummer": dno,
            "date": c.get("date", ""),
            "typ": c.get("typ", ""),
            "subject": c.get("subject", ""),
            "kommun": c.get("kommun", ""),
            "hauptman": c.get("hauptman", ""),
            "skolkod": c.get("skolkod", ""),
            "document_count": len(ids),
            "document_ids": "; ".join(ids),
            "has_document": "ja" if ids else "nej",
        })

    # ── Skriv ──
    jw = JsonlWriter(DOCUMENTS_JSONL, append=False)
    for d in documents:
        jw.write({k: v for k, v in d.items() if not k.startswith("_")})
    jw.close()
    n_docs = write_csv(DOCUMENTS_CSV, DOC_FIELDS, documents)

    jw = JsonlWriter(CASE_DOCS_JSONL, append=False)
    for l in links:
        jw.write(l)
    jw.close()
    n_links = write_csv(CASE_DOCS_CSV, LINK_FIELDS, links)

    n_cases = write_csv(CASES_CSV, CASE_FIELDS, case_rows)

    # ── Sammanfattning ──
    ok_docs = [d for d in documents
               if d["download_status"] in ("ok", "already_archived", "imported")]
    with_dnr = [d for d in ok_docs if d["primary_diarienummer"]]
    own_links = [l for l in links if l["link_type"] == "own_dnr"]
    ref_links = [l for l in links if l["link_type"] != "own_dnr"]
    multi_dnr = [d for d in ok_docs if d["dnr_count"] > 1]
    cases_with = sum(1 for r in case_rows if r["has_document"] == "ja")
    cases_multi = sum(1 for r in case_rows if r["document_count"] > 1)
    unmatched = {l["diarienummer"] for l in own_links
                 if l["link_confidence"] != "high"}
    total_bytes = sum(d["file_size"] for d in ok_docs
                      if isinstance(d["file_size"], int))

    log.info("=" * 72)
    log.info("Index byggt")
    log.info("  dokument totalt:              %d", len(documents))
    log.info("  varav nedladdade:            %d", len(ok_docs))
    log.info("  med diarienummer i texten:   %d (%.1f %%)",
             len(with_dnr), 100 * len(with_dnr) / max(1, len(ok_docs)))
    log.info("  med FLERA diarienummer:      %d", len(multi_dnr))
    log.info("  kopplingar totalt:           %d", n_links)
    log.info("    varav ägande (own_dnr):    %d", len(own_links))
    log.info("    varav hänvisningar:        %d", len(ref_links))
    log.info("  ärenden totalt:              %d", len(case_rows))
    log.info("  ärenden med dokument:        %d (%.1f %%)",
             cases_with, 100 * cases_with / max(1, len(case_rows)))
    log.info("  ärenden med FLERA dokument:  %d", cases_multi)
    log.info("  diarienummer utan ärende:    %d", len(unmatched))
    log.info("  total lagring:               %s", fmt_bytes(total_bytes))
    log.info("")
    log.info("  Jämförelse med gamla beslut.json: %d kopplingar → %d nu (%.1fx)",
             len(legacy_beslut), cases_with,
             cases_with / max(1, len(legacy_beslut)))
    log.info("")
    log.info("Filer:")
    log.info("  %s  (%d rader)", DOCUMENTS_CSV, n_docs)
    log.info("  %s  (%d rader)  ← KOPPLINGSTABELLEN", CASE_DOCS_CSV, n_links)
    log.info("  %s  (%d rader)", CASES_CSV, n_cases)
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
