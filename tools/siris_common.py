#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
siris_common.py — gemensam infrastruktur för SIRIS-verktygen.

Sökvägar styrs av environment variables så att arkivroten kan flyttas utan
kodändring (samma princip som senare gäller för databas och objektlagring):

    SIRIS_ROOT      arkivets rot            (default: D:\\siris)
    SIRIS_SOURCE    repo med data.json m.m. (default: den här filens repo)

Layout under SIRIS_ROOT:

    catalog/    råsvar och normaliserad katalog från Skolinspektionens API
    pdf/        SAMTLIGA dokument, platt: siris-<docID>.pdf
    index/      kopplingstabeller (CSV/JSONL) — ärende ↔ dokument
    reports/    verifieringsrapporter
    logs/       körningsloggar
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ──────────────────────────────────────────────────────────────────────────
#  Sökvägar
# ──────────────────────────────────────────────────────────────────────────

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = os.environ.get("SIRIS_ROOT") or ("D:\\siris" if os.name == "nt" else "/srv/siris")
SOURCE = os.environ.get("SIRIS_SOURCE") or REPO

CATALOG_DIR = os.path.join(ROOT, "catalog")
PDF_DIR = os.path.join(ROOT, "pdf")
INDEX_DIR = os.path.join(ROOT, "index")
REPORT_DIR = os.path.join(ROOT, "reports")
LOG_DIR = os.path.join(ROOT, "logs")

DATA_JSON = os.path.join(SOURCE, "data.json")
BESLUT_JSON = os.path.join(SOURCE, "beslut.json")
SKOLOR_JSON = os.path.join(SOURCE, "skolor.json")

# Katalogfiler
CATALOG_JSONL = os.path.join(CATALOG_DIR, "catalog.jsonl")      # en rad per (docID, upptäcktsnod)
DOCUMENTS_JSONL = os.path.join(INDEX_DIR, "documents.jsonl")    # en rad per unikt docID
DOCUMENTS_CSV = os.path.join(INDEX_DIR, "documents.csv")
CASE_DOCS_CSV = os.path.join(INDEX_DIR, "case_documents.csv")   # kopplingstabellen
CASE_DOCS_JSONL = os.path.join(INDEX_DIR, "case_documents.jsonl")
CASES_CSV = os.path.join(INDEX_DIR, "cases.csv")

SI_API = "https://www.skolinspektionen.se/api/siris"
SIRIS_FILE = "https://siris.skolverket.se/siris/ris.openfile?docID={docid}"

USER_AGENT = (
    "skolinsyn-archiver/2.0 (+https://w3d3.skolinsyn.se; "
    "arkivering av offentliga myndighetsbeslut)"
)


def ensure_dirs() -> None:
    for d in (ROOT, CATALOG_DIR, PDF_DIR, INDEX_DIR, REPORT_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def pdf_path(docid: int | str) -> str:
    """Platt sökväg för ett dokument. Alla PDF:er ligger på samma ställe."""
    return os.path.join(PDF_DIR, f"siris-{docid}.pdf")


def document_id(docid: int | str) -> str:
    return f"siris-{docid}"


# ──────────────────────────────────────────────────────────────────────────
#  Loggning
# ──────────────────────────────────────────────────────────────────────────


def setup_logging(name: str, verbose: bool = False) -> tuple[logging.Logger, str]:
    ensure_dirs()
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    logfile = os.path.join(LOG_DIR, f"{name}-{run_id}.log")

    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(sh)

    log.run_id = run_id  # type: ignore[attr-defined]
    return log, logfile


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────────────────
#  Nätverk
# ──────────────────────────────────────────────────────────────────────────


class RateLimiter:
    """Global takthållare över alla trådar."""

    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.delay
        if sleep_for:
            time.sleep(sleep_for)


def http_get(url: str, timeout: int = 60, accept: str | None = None
             ) -> tuple[int, bytes, str, str]:
    """Returnerar (status, body, content_type, error). status 0 = inget svar."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.status, r.read(),
                    (r.headers.get("Content-Type") or "").split(";")[0].strip(), "")
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        ct = (e.headers.get("Content-Type") if e.headers else "") or ""
        return e.code, body, ct.split(";")[0].strip(), f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return 0, b"", "", f"{type(e).__name__}: {e}"


def retryable(status: int) -> bool:
    return status == 0 or status == 429 or 500 <= status < 600


def http_get_retry(url: str, limiter: RateLimiter, log: logging.Logger,
                   tries: int = 4, timeout: int = 60, base: float = 2.0,
                   cap: float = 60.0, accept: str | None = None
                   ) -> tuple[int, bytes, str, str, int]:
    """http_get med exponentiell backoff. Returnerar även antal försök."""
    status, body, ct, err = 0, b"", "", ""
    for attempt in range(1, tries + 1):
        limiter.wait()
        status, body, ct, err = http_get(url, timeout, accept)
        if status == 200 and body:
            return status, body, ct, "", attempt
        if not retryable(status):
            return status, body, ct, err, attempt
        if attempt < tries:
            wait = min(base * (2 ** (attempt - 1)), cap)
            log.warning("  försök %d/%d misslyckades (%s %s) – väntar %.0fs",
                        attempt, tries, status, err, wait)
            time.sleep(wait)
    return status, body, ct, err, tries


def load_api_json(body: bytes):
    """
    Skolinspektionens API returnerar JSON-kodad JSON (en sträng som i sin tur
    innehåller JSON). Hanterar båda formerna.
    """
    text = body.decode("utf-8-sig", "replace")
    data = json.loads(text)
    if isinstance(data, str):
        data = json.loads(data)
    return data


# ──────────────────────────────────────────────────────────────────────────
#  Normalisering
# ──────────────────────────────────────────────────────────────────────────

_DNR_RE = re.compile(r"SI\s*(\d{4})\s*[:\-]\s*(\d{1,6})", re.IGNORECASE)

# Äldre diarienummerserier. Skolinspektionen införde 'SI ÅÅÅÅ:NNNN' först
# omkring 2016–2018. Dessförinnan användes en serieprefixad form:
#
#   53-2006:2989    Skolverkets utbildningsinspektion (2003–2008)
#   401-2014:2380   Skolinspektionens tillsyn (2009–2015)
#   402-2013:2272   annan ärendeserie
#
# Tankstreck (–) förekommer i stället för bindestreck. Dessa nummer kan inte
# kopplas mot data.json, som börjar 2019-01-01 — men de är dokumentets
# faktiska diarienummer och sparas som metadata, så att kopplingen kan göras
# i efterhand om ett äldre diarium blir tillgängligt.
_LEGACY_DNR_RE = re.compile(
    r"\b(\d{2,4})\s*[-‐-―]\s*(\d{4})\s*:\s*(\d{1,6})\b"
)

# Skolverkets utbildningsinspektion 2003–2008 skriver diarienumret helt utan
# serieprefix: "Dnr 2003:1810".
#
# Den formen får ALDRIG matchas fritt. Svenska författningsnummer har exakt
# samma form — "i enlighet med förordning 2009:672" förekommer i besluten — och
# en fri matchning skulle koppla beslut till ärenden som inte finns. Etiketten
# 'Dnr' omedelbart före är därför obligatorisk här, till skillnad från i de
# andra serierna där formen i sig är entydig.
_BARE_DNR_RE = re.compile(
    r"(?:dnr|diarienummer|diarienr|d\.?nr)\s*[:.]?\s*(\d{4})\s*:\s*(\d{1,6})\b",
    re.IGNORECASE,
)


def normalize_dno(raw: str) -> str:
    """
    Kanonisk form 'SI YYYY:NNNN'.

    Hanterar hårt mellanslag (U+00A0), smalt mellanslag, saknat mellanslag och
    bindestreck i stället för kolon. Detta rättar felet i scan_siris.py rad 41.
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    s = re.sub(r"\s+", " ", s).strip()
    m = _DNR_RE.match(s)
    if m:
        return f"SI {m.group(1)}:{int(m.group(2))}"
    return s


# Etiketter som markerar att diarienumret är dokumentets EGET, inte en referens
# till ett annat ärende. Beslut inleds typiskt med "Beslut  2022-03-17  Dnr SI 2019:7839".
_DNR_LABEL_RE = re.compile(
    r"(?:dnr|diarienummer|diarienr|d\.?nr|ärendenummer|arendenummer)\s*[:.]?\s*$",
    re.IGNORECASE,
)


def find_dnrs(text: str) -> list[str]:
    """Alla diarienummer som förekommer i en text, i den ordning de dyker upp."""
    return [d for d, _kind in find_dnrs_labeled(text)]


def find_legacy_dnrs(text: str, header_window: int = 1500
                     ) -> list[tuple[str, str]]:
    """
    Diarienummer i de äldre serierna, med samma own/mentioned-distinktion som
    find_dnrs_labeled.

    Två former hanteras:
      NNN-ÅÅÅÅ:NNNN   serieprefixad, entydig, matchas var som helst i texten
      ÅÅÅÅ:NNNN       naken, matchas ENDAST direkt efter etiketten 'Dnr'

    Den nakna formen sammanfaller med svenska författningsnummer
    ("förordning 2009:672"), och matchas därför aldrig utan etikett.
    Kanonisk form: som den skrevs, med prefix när sådant finns.
    """
    t = text or ""
    hits: list[tuple[str, int, bool]] = []
    order: dict[str, int] = {}
    for m in _LEGACY_DNR_RE.finditer(t):
        dno = f"{m.group(1)}-{m.group(2)}:{int(m.group(3))}"
        before = t[max(0, m.start() - 40):m.start()]
        labelled = bool(_DNR_LABEL_RE.search(before))
        if dno in order:
            i = order[dno]
            if labelled and not hits[i][2]:
                hits[i] = (dno, hits[i][1], True)
            continue
        order[dno] = len(hits)
        hits.append((dno, m.start(), labelled))

    # Naken form, endast direkt efter 'Dnr'. Etiketten är per definition
    # närvarande, så dessa räknas alltid som etiketterade.
    for m in _BARE_DNR_RE.finditer(t):
        dno = f"{m.group(1)}:{int(m.group(2))}"
        # Hoppa över om numret redan fångats som del av en prefixad form.
        if any(h[0].endswith(dno) for h in hits):
            continue
        if dno in order:
            continue
        order[dno] = len(hits)
        hits.append((dno, m.start(), True))

    hits.sort(key=lambda h: h[1])
    if not hits:
        return []
    own = next((i for i, (_d, p, l) in enumerate(hits)
                if l and p < header_window), None)
    if own is None:
        own = next((i for i, (_d, _p, l) in enumerate(hits) if l), None)
    if own is None:
        own = 0
    return [(d, "own" if i == own else "mentioned")
            for i, (d, _p, _l) in enumerate(hits)]


def find_dnrs_labeled(text: str, header_window: int = 1500
                      ) -> list[tuple[str, str]]:
    """
    Diarienummer med uppgift om HUR de förekommer.

    Returnerar [(diarienummer, kind)] där kind är:
      "own"       — dokumentets eget ärende
      "mentioned" — dokumentet hänvisar till ett annat ärende

    Skillnaden är avgörande för kopplingens precision. Ett beslut skriver ofta
    ut andra ärendens diarienummer i löptext, t.ex. "vi fattar idag beslut i
    ärendet med dnr SI 2019:7841". Utan den här distinktionen kopplas beslutet
    till varje ärende det råkar nämna.

    Diskrimineringen bygger på två signaler som båda följer av hur besluten är
    satta:

      1. Etikett — numret föregås av 'Dnr'/'Diarienummer'.
      2. Position — det egna numret står i sidhuvudet på sida 1, tillsammans
         med beslutsdatum och sidnumrering. Hänvisningar står i brödtext,
         längre ned.

    Det egna numret är därför den första etiketterade förekomsten inom
    sidhuvudsfönstret. Saknas en sådan används första etiketterade förekomsten
    var som helst; saknas även den betraktas den första förekomsten som egen,
    eftersom ett dokument utan tydligt eget diarienummer annars inte skulle gå
    att koppla alls.
    """
    t = text or ""
    hits: list[tuple[str, int, bool]] = []   # (dnr, position, labelled)
    order: dict[str, int] = {}
    for m in _DNR_RE.finditer(t):
        dno = f"SI {m.group(1)}:{int(m.group(2))}"
        before = t[max(0, m.start() - 40):m.start()]
        labelled = bool(_DNR_LABEL_RE.search(before))
        if dno in order:
            # Behåll den tidigaste/starkaste förekomsten per nummer.
            i = order[dno]
            if labelled and not hits[i][2]:
                hits[i] = (dno, hits[i][1], True)
            continue
        order[dno] = len(hits)
        hits.append((dno, m.start(), labelled))

    if not hits:
        return []

    own_idx = next((i for i, (_d, pos, lab) in enumerate(hits)
                    if lab and pos < header_window), None)
    if own_idx is None:
        own_idx = next((i for i, (_d, _p, lab) in enumerate(hits) if lab), None)
    if own_idx is None:
        own_idx = 0

    return [(d, "own" if i == own_idx else "mentioned")
            for i, (d, _p, _l) in enumerate(hits)]


def docid_from_url(url: str) -> int | None:
    m = re.search(r"docID=(\d+)", url or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


# ──────────────────────────────────────────────────────────────────────────
#  Dokumenttyp ur katalogens titel
# ──────────────────────────────────────────────────────────────────────────

# Ordningen är signifikant: mer specifika mönster först.
_DOCTYPE_RULES: list[tuple[str, str]] = [
    # Skolenkäten. Namngivningen har ändrats över åren: äldre poster heter
    # "Elevenkäten", "Vårdnadshavare", "Personalenkäten" eller
    # "Förskolerapport" utan att ordet Skolenkäten nämns.
    (r"skolenk[äa]ten", "skolenkaten"),
    (r"elevenk[äa]ten", "skolenkaten"),
    (r"personalenk[äa]ten", "skolenkaten"),
    (r"f[öo]r[äa]ldraenk[äa]ten", "skolenkaten"),
    (r"v[åa]rdnadshavare", "skolenkaten"),
    (r"pedagogisk personal", "skolenkaten"),
    (r"f[öo]r[äa]ldraelevbrev", "skolenkaten"),
    (r"huvudmannarapport", "skolenkaten"),
    (r"skolenhetsrapport", "skolenkaten"),
    (r"f[öo]rskolerapport", "skolenkaten"),
    (r"ombed[öo]mning", "ombedomning_nationella_prov"),
    # Utbildningsinspektion: tillsynens form 2003–2009, före Skolinspektionen.
    (r"utb\.?\s*insp\.?|utbildningsinspektion", "utbildningsinspektion"),
    (r"uppf[öo]ljning", "uppfoljningsbeslut"),
    (r"oanm[äa]ld granskning", "granskningsbeslut"),
    (r"granskningsbeslut", "granskningsbeslut"),
    (r"kvalitetsgranskning", "kvalitetsgranskning"),
    (r"huvudmannatillsyn", "tillsynsbeslut"),
    (r"\bregelbunden tillsyn\b", "tillsynsbeslut"),
    (r"\btematisk tillsyn\b", "tillsynsbeslut"),
    (r"\bplanerad tillsyn\b", "tillsynsbeslut"),
    (r"\briktad tillsyn\b", "tillsynsbeslut"),
    (r"\btillsyn\b", "tillsynsbeslut"),
    (r"f[öo]rel[äa]ggande", "forelaggande"),
    (r"skolbeslut|kommunbeslut|huvudmannabeslut", "beslut"),
    (r"\brapport\b", "rapport"),
    (r"\bbeslut\b", "beslut"),
]

# Nedladdningsprioritet. Skolinspektionens beslut hämtas före enkätresultat, så
# att en avbruten körning ändå har säkrat det som Beslutstjänsten bygger på.
DOCTYPE_PRIORITY: dict[str, int] = {
    "tillsynsbeslut": 0,
    "uppfoljningsbeslut": 0,
    "granskningsbeslut": 0,
    "kvalitetsgranskning": 0,
    "forelaggande": 0,
    "beslut": 1,
    "utbildningsinspektion": 1,
    "rapport": 2,
    "okant": 3,
    "ombedomning_nationella_prov": 4,
    "skolenkaten": 5,
}


def doctype_priority(doctype: str) -> int:
    return DOCTYPE_PRIORITY.get(doctype or "okant", 3)


def classify_document(title: str) -> str:
    t = (title or "").lower()
    for pattern, label in _DOCTYPE_RULES:
        if re.search(pattern, t):
            return label
    return "okant"


def clean_title(text: str) -> str:
    """Tar bort '(pdf, 324 kB)'-suffixet ur katalogens titel."""
    return re.sub(r"\s*\((pdf|PDF)[^)]*\)\s*$", "", (text or "").strip()).strip()


def size_hint(text: str) -> str:
    m = re.search(r"\((?:pdf|PDF),\s*([\d\s.,]+\s*[kKmMgG]?B)\)", text or "")
    return m.group(1).strip() if m else ""


# ──────────────────────────────────────────────────────────────────────────
#  Filtypsigenkänning
# ──────────────────────────────────────────────────────────────────────────

# SIRIS publicerar inte bara PDF. Beslut från 2003–2010 ligger ofta som Word,
# och content-type kan vara application/octet-stream. Filtypen måste därför
# avgöras på innehållet, inte på headern — och ett Word-beslut är ett giltigt
# dokument, inte en misslyckad nedladdning.
def sniff_filetype(body: bytes, content_type: str = "") -> tuple[str, str, bool]:
    """
    Returnerar (kortnamn, filändelse, är_dokument).

    är_dokument = False betyder att svaret inte är en handling alls, t.ex. en
    HTML-felsida som levererats med HTTP 200.
    """
    head = body[:2048]
    stripped = head.lstrip()

    if stripped[:5] == b"%PDF-":
        return "pdf", ".pdf", True
    if head[:8] == bytes.fromhex("d0cf11e0a1b11ae1"):
        # OLE2-container: Word 97–2003 (.doc), men även .xls/.ppt.
        ct = (content_type or "").lower()
        if "excel" in ct or "ms-excel" in ct:
            return "xls", ".xls", True
        if "powerpoint" in ct:
            return "ppt", ".ppt", True
        return "doc", ".doc", True
    if head[:4] == bytes.fromhex("504b0304"):
        # OOXML eller vanlig ZIP. Innehållet avgör vilket.
        probe = body[:64_000]
        if b"word/" in probe:
            return "docx", ".docx", True
        if b"xl/" in probe:
            return "xlsx", ".xlsx", True
        if b"ppt/" in probe:
            return "pptx", ".pptx", True
        return "zip", ".zip", True
    if stripped[:5] == bytes.fromhex("7b5c727466"):   # RTF-signatur
        return "rtf", ".rtf", True
    low = stripped[:512].lower()
    if low[:5] == b"<html" or low[:9] == b"<!doctype" or b"<html" in low[:200]:
        return "html", ".html", False
    if low[:5] == b"<?xml":
        return "xml", ".xml", False
    return "okant", ".bin", False


MIME_BY_KIND = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "rtf": "application/rtf",
    "zip": "application/zip",
    "html": "text/html",
    "xml": "application/xml",
    "okant": "application/octet-stream",
}


def fmt_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ──────────────────────────────────────────────────────────────────────────
#  JSONL
# ──────────────────────────────────────────────────────────────────────────


class JsonlWriter:
    """Trådsäker append-only-skrivare som flushar per rad."""

    def __init__(self, path: str, append: bool = True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(path, "a" if append else "w", encoding="utf-8", newline="\n")

    def write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                os.fsync(self._fh.fileno())
            except Exception:
                pass
            self._fh.close()


def read_jsonl(path: str, key: str | None = None):
    """
    Läser JSONL. Med `key` returneras dict där senaste posten per nyckel vinner,
    annars en lista.
    """
    if not os.path.exists(path):
        return {} if key else []
    out_d: dict = {}
    out_l: list = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if key:
                k = rec.get(key)
                if k is not None:
                    out_d[k] = rec
            else:
                out_l.append(rec)
    return out_d if key else out_l


def load_json_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
