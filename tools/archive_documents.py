#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive_documents.py — Etapp 2: säkra samtliga beslutsdokument lokalt.

Läser dagens datakällor (data.json + beslut.json), härleder samtliga kända
dokument-URL:er, laddar ner varje dokument och skriver ett fullständigt manifest.

Verktyget är avsiktligt fristående:
  - det läser befintliga filer men ändrar dem ALDRIG
  - all output hamnar under archive/, reports/ och logs/
  - körningen kan avbrytas och återupptas när som helst

Arkivstruktur:
    archive/<år>/<diarienummer-sanerat>/<document-id>.pdf

Manifest (append-only ledger, fungerar samtidigt som resume-state):
    archive/manifest.jsonl
    archive/manifest.csv     (regenereras ur .jsonl vid varje körnings slut)

Loggfil:
    logs/archive-<timestamp>.log

Användning:
    python tools/archive_documents.py                  # full körning / återuppta
    python tools/archive_documents.py --limit 25       # provkörning
    python tools/archive_documents.py --dry-run        # planera utan nedladdning
    python tools/archive_documents.py --retry-failed   # gör om misslyckade poster
    python tools/archive_documents.py --workers 3 --delay 0.7
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterator

import urllib.error
import urllib.request

# ──────────────────────────────────────────────────────────────────────────
#  Sökvägar
# ──────────────────────────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_JSON = os.path.join(ROOT, "data.json")
BESLUT_JSON = os.path.join(ROOT, "beslut.json")
SKOLOR_JSON = os.path.join(ROOT, "skolor.json")

ARCHIVE_DIR = os.path.join(ROOT, "archive")
MANIFEST_JSONL = os.path.join(ARCHIVE_DIR, "manifest.jsonl")
MANIFEST_CSV = os.path.join(ARCHIVE_DIR, "manifest.csv")
LOG_DIR = os.path.join(ROOT, "logs")

SIRIS_URL = "https://siris.skolverket.se/siris/ris.openfile?docID={docid}"

USER_AGENT = (
    "w3d3-archiver/1.0 (skolinsyn.se; arkivering av offentliga beslut; "
    "kontakt via skolinsyn.se)"
)

# Manifestets kolumner. Ordningen är CSV-ordningen.
MANIFEST_FIELDS = [
    # obligatoriska minimikolumner
    "case_id",
    "diarienummer",
    "case_date",
    "document_id",
    "document_type",
    "source_url",
    "local_path",
    "filename",
    "mime_type",
    "file_size",
    "sha256",
    "http_status",
    "downloaded_at",
    "download_status",
    "error_message",
    # tillgänglig metadata
    "kommun",
    "skola",
    "skolkod",
    "huvudman",
    "arendetyp",
    "beslutstyp",
    "beslutsdatum",
    # proveniens / spårbarhet
    "arendemening",
    "source_system",
    "link_source",
    "link_confidence",
    "attempts",
    "run_id",
]

# download_status-värden
ST_OK = "ok"                      # nedladdad och verifierad som PDF
ST_OK_NOT_PDF = "ok_not_pdf"      # nedladdad, men innehållet är inte PDF
ST_EXISTS = "already_archived"    # fanns redan lokalt, hoppades över
ST_HTTP_ERROR = "http_error"      # servern svarade med felkod
ST_NETWORK_ERROR = "network_error"
ST_EMPTY = "empty_response"
ST_SKIPPED = "skipped"

STATUS_FAILED = {ST_HTTP_ERROR, ST_NETWORK_ERROR, ST_EMPTY, ST_OK_NOT_PDF}

log = logging.getLogger("archiver")


# ──────────────────────────────────────────────────────────────────────────
#  Hjälpfunktioner
# ──────────────────────────────────────────────────────────────────────────


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_dno(raw: str) -> str:
    """
    Normaliserar ett diarienummer till kanonisk form 'SI YYYY:NNNN'.

    Hanterar de fel som finns i befintlig data: hårt mellanslag (U+00A0),
    saknat mellanslag efter 'SI', dubbla mellanslag, gemener.
    Detta är rättningen av den trasiga `.replace(' ', ' ')` i scan_siris.py.
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    m = re.match(r"^SI\s*(\d{4})\s*:\s*(\d+)$", s, re.IGNORECASE)
    if m:
        return f"SI {m.group(1)}:{m.group(2)}"
    return s


def sanitize_filename(name: str) -> str:
    """
    Sanerar en sträng för användning som fil-/katalognamn på alla plattformar.

    - kolon och andra Windows-otillåtna tecken ersätts med bindestreck
    - blanksteg blir understreck
    - reserverade Windows-namn (CON, PRN, ...) prefixas
    - resultatet begränsas till 100 tecken
    """
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = s.replace(" ", "_")
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", s)
    s = re.sub(r"[-_]{2,}", lambda m: m.group(0)[0], s)
    s = s.strip(". ")
    if not s:
        s = "unknown"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", s, re.IGNORECASE):
        s = "_" + s
    return s[:100]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_pdf(data: bytes) -> bool:
    """En PDF ska börja med %PDF- inom de första bytesen."""
    return data[:1024].lstrip()[:5] == b"%PDF-"


def year_of(date_str: str, dno: str) -> str:
    """Årtal för arkivstrukturen: primärt ärendets datum, annars ur dno."""
    if date_str and re.match(r"^\d{4}", date_str):
        return date_str[:4]
    m = re.search(r"(\d{4}):", dno or "")
    return m.group(1) if m else "unknown"


# ──────────────────────────────────────────────────────────────────────────
#  Datakällor
# ──────────────────────────────────────────────────────────────────────────


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_case_index() -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """
    Läser data.json och skolor.json.

    Returnerar (cases_by_dno, schools_by_code, warnings).
    """
    warnings: list[str] = []

    cases_raw = load_json(DATA_JSON)
    log.info("data.json: %d ärenden inlästa", len(cases_raw))

    cases: dict[str, dict] = {}
    collisions = 0
    for row in cases_raw:
        dno = normalize_dno(row.get("dno", ""))
        if not dno:
            continue
        if dno in cases:
            collisions += 1
            continue
        cases[dno] = row
    if collisions:
        warnings.append(
            f"{collisions} ärenden hade dubblerat diarienummer efter normalisering "
            f"och ignorerades"
        )
    log.info("data.json: %d unika diarienummer efter normalisering", len(cases))

    schools: dict[str, dict] = {}
    if os.path.exists(SKOLOR_JSON):
        try:
            for s in load_json(SKOLOR_JSON):
                code = str(s.get("code", "")).strip()
                if code:
                    schools[code] = s
            log.info("skolor.json: %d skolenheter inlästa", len(schools))
        except Exception as exc:  # berikning är valfri, får inte stoppa körningen
            warnings.append(f"skolor.json kunde inte läsas: {exc}")
    else:
        warnings.append("skolor.json saknas – skolnamn kommer inte att fyllas i")

    return cases, schools, warnings


def discover_documents(
    cases: dict[str, dict], schools: dict[str, dict]
) -> tuple[list[dict], list[str]]:
    """
    Härleder samtliga kända dokument ur beslut.json och kopplar dem till ärenden.

    Varje post i beslut.json är ett SIRIS-docID som skannats fram ur PDF:ens
    egen 'Dnr'-rad, vilket gör kopplingen dokument→ärende högt tillförlitlig.
    Poster vars diarienummer saknar motsvarighet i data.json arkiveras ändå —
    dokumentet är det värdefulla, ärendemetadata kan kompletteras senare.
    """
    warnings: list[str] = []
    beslut = load_json(BESLUT_JSON)
    log.info("beslut.json: %d dokumentkopplingar inlästa", len(beslut))

    docs: list[dict] = []
    seen_docids: dict[int, str] = {}
    unmatched = 0
    renormalized = 0

    for raw_dno, docid in beslut.items():
        dno = normalize_dno(raw_dno)
        if dno != raw_dno:
            renormalized += 1

        try:
            docid_int = int(docid)
        except (TypeError, ValueError):
            warnings.append(f"{raw_dno}: ogiltigt docID {docid!r}, hoppades över")
            continue

        # Samma docID kopplat till flera diarienummer = misstänkt, men båda behålls.
        if docid_int in seen_docids and seen_docids[docid_int] != dno:
            warnings.append(
                f"docID {docid_int} kopplat till både {seen_docids[docid_int]} "
                f"och {dno}"
            )
        seen_docids[docid_int] = dno

        case = cases.get(dno)
        if case is None:
            unmatched += 1

        docs.append(
            build_document_record(dno, docid_int, case, schools, raw_dno)
        )

    if renormalized:
        log.info("%d diarienummer normaliserades (t.ex. saknat mellanslag)", renormalized)
    if unmatched:
        warnings.append(
            f"{unmatched} dokument har diarienummer utan matchande ärende i data.json"
        )

    docs.sort(key=lambda d: (d["case_date"] or "", d["diarienummer"]))
    return docs, warnings


def build_document_record(
    dno: str,
    docid: int,
    case: dict | None,
    schools: dict[str, dict],
    raw_dno: str,
) -> dict:
    """Skapar en fullständig manifestpost (ännu ej nedladdad)."""
    case = case or {}
    skolkod = str(case.get("skolkod", "") or "")
    school = schools.get(skolkod, {})

    case_date = str(case.get("date", "") or "")
    year = year_of(case_date, dno)
    document_id = f"siris-{docid}"

    dno_dir = sanitize_filename(dno)
    filename = f"{document_id}.pdf"
    local_path = os.path.join("archive", year, dno_dir, filename).replace("\\", "/")

    return {
        "case_id": dno,           # dno är systemets naturliga ärende-ID idag
        "diarienummer": dno,
        "case_date": case_date,
        "document_id": document_id,
        "document_type": "beslut",   # SIRIS publicerar beslutshandlingar
        "source_url": SIRIS_URL.format(docid=docid),
        "local_path": local_path,
        "filename": filename,
        "mime_type": "",
        "file_size": "",
        "sha256": "",
        "http_status": "",
        "downloaded_at": "",
        "download_status": "",
        "error_message": "",
        "kommun": str(case.get("kommun", "") or ""),
        "skola": str(school.get("name", "") or ""),
        "skolkod": skolkod,
        "huvudman": str(school.get("org") or case.get("hauptman", "") or ""),
        "arendetyp": str(case.get("typ", "") or ""),
        "beslutstyp": "",          # finns inte i källdata – fylls när PDF-text indexeras
        "beslutsdatum": "",        # finns inte i källdata – fylls när PDF-text indexeras
        "arendemening": str(case.get("subject", "") or ""),
        "source_system": "SIRIS",
        "link_source": "beslut.json (scan_siris.py: Dnr ur PDF sida 1)",
        # Kopplingen kommer ur dokumentets eget innehåll när ärendet finns i
        # data.json; saknas ärendet kan kopplingen inte korsvalideras.
        "link_confidence": "high" if case else "unverified",
        "attempts": 0,
        "run_id": "",
        "_raw_dno": raw_dno,
    }


# ──────────────────────────────────────────────────────────────────────────
#  Manifest / resume-state
# ──────────────────────────────────────────────────────────────────────────


def read_manifest() -> dict[str, dict]:
    """
    Läser manifest.jsonl. Append-only ledger: senaste posten per document_id vinner.
    """
    state: dict[str, dict] = {}
    if not os.path.exists(MANIFEST_JSONL):
        return state
    bad = 0
    with open(MANIFEST_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if rec.get("document_id"):
                state[rec["document_id"]] = rec
    if bad:
        log.warning("manifest.jsonl: %d rader kunde inte tolkas och ignorerades", bad)
    log.info("manifest.jsonl: %d tidigare poster inlästa (resume)", len(state))
    return state


class ManifestWriter:
    """Trådsäker append-only-skrivare. Flushar varje rad så avbrott inte tappar data."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, rec: dict) -> None:
        clean = {k: rec.get(k, "") for k in MANIFEST_FIELDS}
        line = json.dumps(clean, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def write_manifest_csv(records: list[dict]) -> None:
    """Regenererar manifest.csv ur den aktuella statusbilden."""
    os.makedirs(os.path.dirname(MANIFEST_CSV), exist_ok=True)
    # utf-8-sig så att Excel öppnar svenska tecken korrekt
    with open(MANIFEST_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        w.writeheader()
        for rec in records:
            w.writerow({k: rec.get(k, "") for k in MANIFEST_FIELDS})
    log.info("manifest.csv skriven: %d rader", len(records))


# ──────────────────────────────────────────────────────────────────────────
#  Nedladdning
# ──────────────────────────────────────────────────────────────────────────


class RateLimiter:
    """Global takthållare: minst `delay` sekunder mellan varje utgående anrop."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                sleep_for = self._next - now
            else:
                sleep_for = 0.0
            self._next = max(now, self._next) + self.delay
        if sleep_for > 0:
            time.sleep(sleep_for)


def fetch(url: str, timeout: int) -> tuple[int, bytes, str, str]:
    """
    Hämtar en URL.

    Returnerar (http_status, body, content_type, error_message).
    http_status = 0 betyder att inget HTTP-svar nåddes (nätverksfel).
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "") or ""
            return resp.status, body, ctype.split(";")[0].strip(), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        ctype = (exc.headers.get("Content-Type", "") if exc.headers else "") or ""
        return exc.code, body, ctype.split(";")[0].strip(), f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return 0, b"", "", f"{type(exc).__name__}: {exc}"


def is_retryable(status: int) -> bool:
    """Nätverksfel och serverfel är övergående; 4xx är det inte."""
    return status == 0 or status == 429 or 500 <= status < 600


def download_one(
    rec: dict,
    limiter: RateLimiter,
    args: argparse.Namespace,
    known_hashes: dict[str, str],
    hash_lock: threading.Lock,
) -> dict:
    """
    Laddar ner ett dokument med retry/backoff och fyller i manifestposten.

    Skriver ALDRIG över en befintlig fil utan att först kontrollera den.
    """
    abs_path = os.path.join(ROOT, rec["local_path"].replace("/", os.sep))
    rec["run_id"] = args.run_id

    # ── Befintlig fil: verifiera i stället för att skriva över ──
    if os.path.exists(abs_path):
        size = os.path.getsize(abs_path)
        if size > 0:
            digest = sha256_file(abs_path)
            with open(abs_path, "rb") as f:
                head = f.read(1024)
            rec.update(
                {
                    "file_size": size,
                    "sha256": digest,
                    "mime_type": "application/pdf" if looks_like_pdf(head) else "unknown",
                    "http_status": rec.get("http_status") or "",
                    "downloaded_at": rec.get("downloaded_at")
                    or datetime.fromtimestamp(
                        os.path.getmtime(abs_path), timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "download_status": ST_EXISTS,
                    "error_message": "",
                }
            )
            register_hash(rec, known_hashes, hash_lock)
            log.debug("%s: fanns redan (%d B)", rec["document_id"], size)
            return rec
        # Nollstor fil från en avbruten körning – säkert att ersätta.
        log.warning("%s: nollstor fil hittades, laddas om", rec["document_id"])

    # ── Nedladdning med exponentiell backoff ──
    status, body, ctype, err = 0, b"", "", ""
    for attempt in range(1, args.max_retries + 1):
        rec["attempts"] = attempt
        limiter.wait()
        status, body, ctype, err = fetch(rec["source_url"], args.timeout)

        if status == 200 and body:
            break
        if not is_retryable(status):
            break
        if attempt < args.max_retries:
            backoff = min(args.backoff_base * (2 ** (attempt - 1)), args.backoff_max)
            log.warning(
                "%s: försök %d/%d misslyckades (status=%s %s) – väntar %.1fs",
                rec["document_id"], attempt, args.max_retries, status, err, backoff,
            )
            time.sleep(backoff)

    rec["http_status"] = status
    rec["downloaded_at"] = utcnow()
    rec["mime_type"] = ctype

    # ── Utvärdera resultatet ──
    if status != 200:
        rec["download_status"] = ST_NETWORK_ERROR if status == 0 else ST_HTTP_ERROR
        rec["error_message"] = err or f"oväntad status {status}"
        log.error("%s: MISSLYCKADES – %s", rec["document_id"], rec["error_message"])
        return rec

    if not body:
        rec["download_status"] = ST_EMPTY
        rec["error_message"] = "HTTP 200 men tomt svar"
        log.error("%s: tomt svar trots HTTP 200", rec["document_id"])
        return rec

    is_pdf = looks_like_pdf(body)

    # HTTP 200 räcker inte. En sida som svarar med HTML-felsida ska inte
    # räknas som ett arkiverat dokument — men innehållet sparas ändå för
    # granskning, med tydlig status.
    if not is_pdf:
        rec["download_status"] = ST_OK_NOT_PDF
        rec["error_message"] = (
            f"HTTP 200 men innehållet är inte PDF "
            f"(content-type={ctype or 'okänd'}, första bytes="
            f"{body[:16]!r})"
        )
        rec["filename"] = rec["document_id"] + ".bin"
        rec["local_path"] = rec["local_path"].rsplit("/", 1)[0] + "/" + rec["filename"]
        abs_path = os.path.join(ROOT, rec["local_path"].replace("/", os.sep))
        log.error("%s: HTTP 200 men inte PDF (%s)", rec["document_id"], ctype)
    else:
        rec["download_status"] = ST_OK
        rec["error_message"] = ""
        if not ctype:
            rec["mime_type"] = "application/pdf"

    # ── Atomisk skrivning ──
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    tmp_path = abs_path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, abs_path)

    rec["file_size"] = len(body)
    rec["sha256"] = hashlib.sha256(body).hexdigest()
    register_hash(rec, known_hashes, hash_lock)

    log.info(
        "%s -> %s (%s, %d B)",
        rec["diarienummer"], rec["local_path"], rec["download_status"], len(body),
    )
    return rec


def register_hash(rec: dict, known: dict[str, str], lock: threading.Lock) -> None:
    """Noterar dubbletter i error_message utan att ändra download_status."""
    digest = rec.get("sha256")
    if not digest:
        return
    with lock:
        first = known.get(digest)
        if first is None:
            known[digest] = rec["document_id"]
            return
    if first != rec["document_id"]:
        note = f"identiskt innehåll som {first}"
        rec["error_message"] = (
            f"{rec['error_message']}; {note}" if rec["error_message"] else note
        )


# ──────────────────────────────────────────────────────────────────────────
#  Orkestrering
# ──────────────────────────────────────────────────────────────────────────


def setup_logging(run_id: str, verbose: bool) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(LOG_DIR, f"archive-{run_id}.log")

    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(sh)

    return logfile


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Arkiverar samtliga kända beslutsdokument lokalt.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=0,
                   help="ladda bara ner N dokument (provkörning)")
    p.add_argument("--workers", type=int, default=4,
                   help="antal parallella nedladdningar (standard 4)")
    p.add_argument("--delay", type=float, default=0.4,
                   help="minsta sekunder mellan utgående anrop, globalt (standard 0.4)")
    p.add_argument("--timeout", type=int, default=60,
                   help="timeout per anrop i sekunder (standard 60)")
    p.add_argument("--max-retries", type=int, default=4,
                   help="antal försök per dokument (standard 4)")
    p.add_argument("--backoff-base", type=float, default=2.0,
                   help="första backoff-väntan i sekunder (standard 2)")
    p.add_argument("--backoff-max", type=float, default=60.0,
                   help="längsta backoff-väntan i sekunder (standard 60)")
    p.add_argument("--dry-run", action="store_true",
                   help="planera körningen och skriv plan, ladda inte ner något")
    p.add_argument("--retry-failed", action="store_true",
                   help="gör om poster som tidigare misslyckades")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    args.run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logfile = setup_logging(args.run_id, args.verbose)

    log.info("=" * 72)
    log.info("Arkiveringskörning %s startad", args.run_id)
    log.info("Rot: %s", ROOT)
    log.info("Logg: %s", logfile)
    log.info("Parametrar: workers=%d delay=%.2fs timeout=%ds retries=%d",
             args.workers, args.delay, args.timeout, args.max_retries)
    log.info("=" * 72)

    for path in (DATA_JSON, BESLUT_JSON):
        if not os.path.exists(path):
            log.error("Nödvändig källfil saknas: %s", path)
            return 2

    # ── Inventera ──
    cases, schools, warn_a = build_case_index()
    docs, warn_b = discover_documents(cases, schools)
    for w in warn_a + warn_b:
        log.warning("INVENTERING: %s", w)

    log.info("Identifierade %d dokument-URL:er för %d unika ärenden",
             len(docs), len({d["diarienummer"] for d in docs}))

    # ── Resume ──
    previous = read_manifest()
    known_hashes: dict[str, str] = {}
    for rec in previous.values():
        if rec.get("sha256"):
            known_hashes.setdefault(rec["sha256"], rec["document_id"])

    todo: list[dict] = []
    skipped_done = 0
    for rec in docs:
        prev = previous.get(rec["document_id"])
        if prev is None:
            todo.append(rec)
            continue
        st = prev.get("download_status")
        if st in (ST_OK, ST_EXISTS):
            abs_path = os.path.join(ROOT, str(prev.get("local_path", "")).replace("/", os.sep))
            if os.path.exists(abs_path) and os.path.getsize(abs_path) > 0:
                skipped_done += 1
                continue
            log.warning("%s: manifestet säger klar men filen saknas – laddas om",
                        rec["document_id"])
            todo.append(rec)
            continue
        if st in STATUS_FAILED and not args.retry_failed:
            skipped_done += 1
            continue
        todo.append(rec)

    if skipped_done:
        log.info("%d dokument hoppas över (redan klara i manifestet)", skipped_done)
    if args.limit and len(todo) > args.limit:
        log.info("--limit %d: begränsar körningen från %d till %d dokument",
                 args.limit, len(todo), args.limit)
        todo = todo[: args.limit]

    log.info("Att hämta i denna körning: %d dokument", len(todo))

    if args.dry_run:
        log.info("--dry-run: ingen nedladdning utförs.")
        for rec in todo[:20]:
            log.info("  PLAN %s  %s  ->  %s",
                     rec["diarienummer"], rec["source_url"], rec["local_path"])
        if len(todo) > 20:
            log.info("  ... och ytterligare %d dokument", len(todo) - 20)
        return 0

    if not todo:
        log.info("Inget att göra. Regenererar manifest.csv.")
        write_manifest_csv(list(previous.values()))
        return 0

    # ── Nedladdning ──
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    writer = ManifestWriter(MANIFEST_JSONL)
    limiter = RateLimiter(args.delay)
    hash_lock = threading.Lock()

    work: queue.Queue = queue.Queue()
    for rec in todo:
        work.put(rec)

    counters = {"done": 0, ST_OK: 0, ST_EXISTS: 0, "failed": 0}
    counter_lock = threading.Lock()
    stop = threading.Event()
    total = len(todo)
    started = time.time()

    def worker() -> None:
        while not stop.is_set():
            try:
                rec = work.get_nowait()
            except queue.Empty:
                return
            try:
                result = download_one(rec, limiter, args, known_hashes, hash_lock)
            except Exception as exc:  # en trasig post får inte stoppa körningen
                log.exception("%s: oväntat fel", rec.get("document_id"))
                result = dict(rec)
                result["download_status"] = ST_NETWORK_ERROR
                result["error_message"] = f"internt fel: {type(exc).__name__}: {exc}"
                result["downloaded_at"] = utcnow()
            writer.write(result)
            previous[result["document_id"]] = result

            with counter_lock:
                counters["done"] += 1
                st = result["download_status"]
                if st in counters:
                    counters[st] += 1
                elif st in STATUS_FAILED:
                    counters["failed"] += 1
                n = counters["done"]
            if n % 50 == 0 or n == total:
                elapsed = time.time() - started
                rate = n / elapsed if elapsed else 0
                eta = (total - n) / rate if rate else 0
                log.info("FRAMSTEG %d/%d (%.1f%%) – %.1f dok/s – ETA %.0f min – "
                         "ok=%d fanns=%d fel=%d",
                         n, total, 100 * n / total, rate, eta / 60,
                         counters[ST_OK], counters[ST_EXISTS], counters["failed"])
            work.task_done()

    threads = [threading.Thread(target=worker, daemon=True, name=f"dl-{i}")
               for i in range(max(1, args.workers))]
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log.warning("Avbrott begärt – avslutar pågående nedladdningar. "
                    "Körningen kan återupptas med samma kommando.")
        stop.set()
        for t in threads:
            t.join(timeout=args.timeout + 5)
    finally:
        writer.close()

    # ── Avslut ──
    write_manifest_csv(list(previous.values()))

    elapsed = time.time() - started
    log.info("=" * 72)
    log.info("Körning %s klar på %.1f min", args.run_id, elapsed / 60)
    log.info("Behandlade: %d av %d planerade", counters["done"], total)
    log.info("  nedladdade:      %d", counters[ST_OK])
    log.info("  fanns redan:     %d", counters[ST_EXISTS])
    log.info("  misslyckade:     %d", counters["failed"])
    log.info("Manifest: %s (%d poster totalt)", MANIFEST_JSONL, len(previous))
    log.info("Kör tools/verify_archive.py för fullständig verifiering.")
    log.info("=" * 72)

    return 0 if counters["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
