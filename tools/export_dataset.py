#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_dataset.py — bygger ett laddningsklart dataset ur arkivet.

Producerar CSV-filer som motsvarar tabellerna i docs/target-data-model.md, samt
en load.sql som laddar dem via staging-tabeller. Målet är att ingen
transformation ska återstå på mottagarsidan: filerna laddas, FK:er löses upp,
klart.

Filerna använder NATURLIGA nycklar (document_key, diarienummer, kommunkod,
organisationsnummer, skolenhetskod) i stället för löpnummer. Det gör exporten
idempotent — samma export kan laddas två gånger utan att dubblera något — och
gör att den kan laddas i vilken ordning som helst.

Output (under SIRIS_ROOT/export/):

    municipalities.csv      kommuner
    organizations.csv       huvudmän, aktuella och historiska
    schools.csv             skolenheter
    documents.csv           handlingar
    document_versions.csv   filer, med storage_key och SHA-256
    cases.csv               ärenden ur diariet
    case_documents.csv      kopplingen ärende ↔ dokument
    survey_reports.csv      Skolenkäten
    import_runs.csv         proveniens
    document_texts.csv      extraherad text (med --with-text)
    load.sql                laddningsskript
    manifest.json           radantal, checksummor, tidsstämpel

Användning:
    python tools/export_dataset.py
    python tools/export_dataset.py --product beslut     # utan Skolenkäten
    python tools/export_dataset.py --with-text
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_DIR, CATALOG_JSONL, DATA_JSON, INDEX_DIR, ROOT, SKOLOR_JSON,
    classify_document, ensure_dirs, fmt_bytes, load_json_file, normalize_dno,
    read_jsonl, setup_logging,
)

EXPORT_DIR = os.path.join(ROOT, "export")
DOWNLOADS_JSONL = os.path.join(INDEX_DIR, "downloads.jsonl")
EXTRACT_JSONL = os.path.join(INDEX_DIR, "extracted.jsonl")
DIARIUM_CSV = os.path.join(INDEX_DIR, "diarium.csv")
COUNTIES_JSON = os.path.join(CATALOG_DIR, "counties.json")
ORGS_JSON = os.path.join(CATALOG_DIR, "organisations.json")
SCHOOLS_JSON = os.path.join(CATALOG_DIR, "schools.json")

# Lagringsprefix. Nyckeln är plattformsneutral och mappar rakt till en
# S3-nyckel; bucket och endpoint kommer från konfiguration, aldrig härifrån.
STORAGE_PREFIX = "siris"

SUCCESS = {"ok", "already_archived", "imported"}

PRODUCT_BY_TYPE = {
    "skolenkaten": "skolenkaten",
    "ombedomning_nationella_prov": "ombedomning",
}

log = None


# ──────────────────────────────────────────────────────────────────────────
#  Skolenkäten: härledning ur rapporttiteln
# ──────────────────────────────────────────────────────────────────────────

_RESPONDENT = [
    (r"elevenk[äa]ten|\belever\b", "elev"),
    (r"v[åa]rdnadshavare|f[öo]r[äa]ldra", "vardnadshavare"),
    (r"personalenk[äa]ten|pedagogisk personal", "pedagogisk_personal"),
]
_SCHOOL_FORM = [
    (r"anpassad grundskola|grunds[äa]rskola", "anpassad_grundskola"),
    (r"anpassad gymnasieskola|gymnasies[äa]rskola", "anpassad_gymnasieskola"),
    (r"f[öo]rskoleklass", "forskoleklass"),
    (r"f[öo]rskol", "forskola"),
    (r"grundskol", "grundskola"),
    (r"gymnasieskol", "gymnasieskola"),
    (r"obligatoriska s[äa]rskolan", "obligatoriska_sarskolan"),
    (r"vuxenutbildning|komvux", "vuxenutbildning"),
]


def parse_survey(title: str) -> dict:
    """
    Härleder Skolenkätens fält ur titeln, som är strukturerad:
    "Grundskolan, Elevenkäten, åk 5, Södertälje, Lina grundskola, VT18"
    """
    t = title or ""
    low = t.lower()
    out = {"report_level": "", "respondent_group": "", "school_form": "",
           "grade": "", "term": "", "year": ""}

    if re.search(r"huvudmannarapport", low):
        out["report_level"] = "huvudman"
    elif re.search(r"skolenhetsrapport|f[öo]rskolerapport", low):
        out["report_level"] = "skolenhet"

    for pat, val in _RESPONDENT:
        if re.search(pat, low):
            out["respondent_group"] = val
            break
    for pat, val in _SCHOOL_FORM:
        if re.search(pat, low):
            out["school_form"] = val
            break

    m = re.search(r"\b[åa]k\s*(\d+)", low)
    if m:
        out["grade"] = f"åk {m.group(1)}"

    m = re.search(r"\b(VT|HT)\s*(\d{2})\b", t, re.I)
    if m:
        term = f"{m.group(1).upper()}{m.group(2)}"
        out["term"] = term
        yy = int(m.group(2))
        out["year"] = str(2000 + yy if yy < 80 else 1900 + yy)
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Utskrift
# ──────────────────────────────────────────────────────────────────────────


class Table:
    """CSV-utskrift med UTF-8 utan BOM — det COPY förväntar sig."""

    def __init__(self, name: str, fields: list[str]):
        self.name = name
        self.fields = fields
        self.path = os.path.join(EXPORT_DIR, f"{name}.csv")
        os.makedirs(EXPORT_DIR, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=fields, extrasaction="ignore")
        self._w.writeheader()
        self.rows = 0

    def write(self, row: dict) -> None:
        self._w.writerow({k: ("" if row.get(k) is None else row.get(k))
                          for k in self.fields})
        self.rows += 1

    def close(self) -> dict:
        self._fh.close()
        h = hashlib.sha256()
        with open(self.path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        size = os.path.getsize(self.path)
        log.info("  %-22s %8d rader  %10s", self.name + ".csv", self.rows,
                 fmt_bytes(size))
        return {"table": self.name, "file": f"{self.name}.csv",
                "rows": self.rows, "bytes": size, "sha256": h.hexdigest()}


def read_csv_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ──────────────────────────────────────────────────────────────────────────
#  Export
# ──────────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(
        description="Bygger laddningsklart dataset ur arkivet.")
    ap.add_argument("--product", choices=["alla", "beslut"], default="alla",
                    help="'beslut' utesluter Skolenkäten och ombedömning")
    ap.add_argument("--with-text", action="store_true",
                    help="ta med extraherad dokumenttext (stor fil)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    os.makedirs(EXPORT_DIR, exist_ok=True)
    log, logfile = setup_logging("export", args.verbose)
    log.info("=" * 72)
    log.info("Export startad – logg: %s", logfile)
    log.info("Mål: %s", EXPORT_DIR)
    log.info("Produktomfång: %s", args.product)
    log.info("=" * 72)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest: list[dict] = []

    # ── Källor ────────────────────────────────────────────────────────────
    log.info("Läser källor ...")

    catalog: dict[int, dict] = {}
    for row in read_jsonl(CATALOG_JSONL):
        d = row.get("docid")
        if d is None:
            continue
        cur = catalog.setdefault(d, {})
        for k, v in row.items():
            if v and not cur.get(k):
                cur[k] = v
    log.info("  katalog:       %d dokument", len(catalog))

    downloads: dict[int, dict] = {}
    for r in read_jsonl(DOWNLOADS_JSONL):
        if r.get("docid") is not None:
            downloads[r["docid"]] = r
    log.info("  nedladdningar: %d", len(downloads))

    extracted = read_jsonl(EXTRACT_JSONL, key="document_id")
    log.info("  extraktioner:  %d", len(extracted))

    diarium = read_csv_rows(DIARIUM_CSV)
    log.info("  diarium:       %d ärenden", len(diarium))

    counties = load_json_file(COUNTIES_JSON) if os.path.exists(COUNTIES_JSON) else []
    orgs = load_json_file(ORGS_JSON) if os.path.exists(ORGS_JSON) else []
    schools_cat = load_json_file(SCHOOLS_JSON) if os.path.exists(SCHOOLS_JSON) else []
    log.info("  kommuner: %d  huvudmän: %d  skolenheter: %d",
             len(counties), len(orgs), len(schools_cat))

    skolor: dict[str, dict] = {}
    if os.path.exists(SKOLOR_JSON):
        try:
            for s in load_json_file(SKOLOR_JSON):
                if s.get("code"):
                    skolor[str(s["code"])] = s
        except Exception as exc:
            log.warning("skolor.json kunde inte läsas: %s", exc)

    muni_by_name = {}
    for c in counties:
        muni_by_name[c["namn"].strip().lower()] = c["kod"]

    def muni_code_from_name(name: str) -> str:
        """Diariet skriver 'Stockholms kommun'; katalogen skriver 'Stockholm'."""
        if not name:
            return ""
        n = re.sub(r"\s+kommun$", "", name.strip(), flags=re.I).strip().lower()
        if n in muni_by_name:
            return muni_by_name[n]
        # 'Stockholms' -> 'Stockholm'
        if n.endswith("s") and n[:-1] in muni_by_name:
            return muni_by_name[n[:-1]]
        return ""

    log.info("Skriver tabeller ...")

    # ── import_runs ───────────────────────────────────────────────────────
    t = Table("import_runs", ["run_key", "tool", "source_system", "started_at",
                              "finished_at", "status", "params", "stats", "notes"])
    t.write({"run_key": f"export-{generated_at}", "tool": "export_dataset",
             "source_system": "SIRIS", "started_at": generated_at,
             "finished_at": generated_at, "status": "completed",
             "params": json.dumps({"product": args.product}, ensure_ascii=False),
             "stats": "{}", "notes": "Export ur lokalt arkiv."})
    manifest.append(t.close())

    # ── municipalities ────────────────────────────────────────────────────
    t = Table("municipalities", ["code", "name"])
    for c in sorted(counties, key=lambda x: x["kod"]):
        t.write({"code": c["kod"], "name": c["namn"]})
    manifest.append(t.close())

    # ── organizations ─────────────────────────────────────────────────────
    t = Table("organizations", ["code", "name", "legal_form", "is_current",
                                "municipality_code"])
    # Bolagsform hämtas ur skolor.json där den finns.
    form_by_org = {}
    for s in skolor.values():
        if s.get("orgnr") and s.get("bolagsform"):
            form_by_org[str(s["orgnr"])] = s["bolagsform"]
    # En huvudman kan förekomma i både 'aktuella' och 'gamla'. Den aktuella
    # posten vinner, annars skulle en verksam huvudman markeras som upphörd.
    seen_org = set()
    for o in sorted(orgs, key=lambda x: (x["kod"], x.get("grupp") != "aktuella")):
        if o["kod"] in seen_org:
            continue
        seen_org.add(o["kod"])
        t.write({"code": o["kod"], "name": o["namn"],
                 "legal_form": form_by_org.get(o["kod"], ""),
                 "is_current": "true" if o.get("grupp") == "aktuella" else "false",
                 "municipality_code": ""})
    manifest.append(t.close())

    # ── schools ───────────────────────────────────────────────────────────
    t = Table("schools", ["code", "name", "organization_code", "municipality_code",
                          "school_forms", "is_current", "contact", "statistics"])
    for s in sorted(schools_cat, key=lambda x: x["kod"]):
        code = s["kod"]
        enr = skolor.get(code, {})
        forms = enr.get("skoltyper") or []
        contact = {k: enr.get(k) for k in ("telefon", "email", "web", "adress")
                   if enr.get(k)}
        stats = enr.get("stats") or {}
        t.write({
            "code": code,
            "name": s.get("namn", "") or enr.get("name", ""),
            "organization_code": enr.get("orgnr", "") or
                                 (s.get("parent_code", "")
                                  if s.get("parent_kind") == "org" else ""),
            "municipality_code": enr.get("areaCode", "") or
                                 (s.get("parent_code", "")
                                  if s.get("parent_kind") == "county" else ""),
            "school_forms": "{" + ",".join(forms) + "}" if forms else "{}",
            "is_current": "true",
            "contact": json.dumps(contact, ensure_ascii=False) if contact else "{}",
            "statistics": json.dumps(stats, ensure_ascii=False) if stats else "{}",
        })
    manifest.append(t.close())

    # ── documents + document_versions + texts + survey_reports ────────────
    doc_t = Table("documents", [
        "document_key", "source_system", "source_id", "product", "document_type",
        "title", "document_year", "document_date", "review_area", "source_url",
        "school_code", "organization_code", "municipality_code",
        "publication_status", "legacy_diarienummer"])
    ver_t = Table("document_versions", [
        "document_key", "version_no", "storage_key", "file_name", "mime_type",
        "file_kind", "file_size", "sha256", "http_status", "download_status",
        "error_message", "downloaded_at", "is_current"])
    survey_t = Table("survey_reports", [
        "document_key", "report_level", "respondent_group", "school_form",
        "grade", "term", "year", "school_code", "organization_code",
        "municipality_code"])
    text_t = Table("document_texts", [
        "document_key", "version_no", "extraction_method", "char_count",
        "content"]) if args.with_text else None

    dnr_links: list[tuple[str, str, str, str, int]] = []
    n_docs = n_skipped_product = n_no_file = 0

    for docid in sorted(catalog):
        cat = catalog[docid]
        dl = downloads.get(docid)
        title = cat.get("raw_title") or cat.get("title") or ""
        dtype = classify_document(title) if title else (cat.get("document_type") or "okant")
        product = PRODUCT_BY_TYPE.get(dtype, "beslut")

        if args.product == "beslut" and product != "beslut":
            n_skipped_product += 1
            continue
        if dl is None or dl.get("download_status") not in SUCCESS:
            n_no_file += 1
            continue

        key = dl.get("document_id") or f"siris-{docid}"
        file_name = dl.get("filename") or f"siris-{docid}.pdf"
        muni = cat.get("county_code", "")

        ex = extracted.get(key, {})
        labeled = ex.get("dnrs_labeled") or []
        legacy = ex.get("legacy_dnrs") or []
        legacy_own = next((d for d, k in legacy if k == "own"), "")

        doc_t.write({
            "document_key": key,
            "source_system": "SIRIS",
            "source_id": str(docid),
            "product": product,
            "document_type": dtype,
            "title": cat.get("title", ""),
            "document_year": cat.get("year", ""),
            "document_date": "",
            "review_area": cat.get("gransknomr", ""),
            "source_url": dl.get("source_url", ""),
            "school_code": cat.get("school_code", ""),
            "organization_code": cat.get("org_code", ""),
            "municipality_code": muni,
            "publication_status": "published",
            "legacy_diarienummer": legacy_own,
        })
        ver_t.write({
            "document_key": key,
            "version_no": 1,
            "storage_key": f"{STORAGE_PREFIX}/{file_name}",
            "file_name": file_name,
            "mime_type": dl.get("mime_type", ""),
            "file_kind": dl.get("file_kind", ""),
            "file_size": dl.get("file_size", ""),
            "sha256": dl.get("sha256", ""),
            "http_status": dl.get("http_status", ""),
            "download_status": dl.get("download_status", ""),
            "error_message": dl.get("error_message", ""),
            "downloaded_at": dl.get("downloaded_at", ""),
            "is_current": "true",
        })
        if product == "skolenkaten":
            sv = parse_survey(title)
            # Äldre rapporttitlar saknar orden "Skolenhetsrapport" och
            # "Huvudmannarapport". Katalogen är då en säkrare källa: en rapport
            # som hittades under en skolenhet avser den skolenheten.
            if not sv["report_level"]:
                sv["report_level"] = ("skolenhet" if cat.get("school_code")
                                      else "huvudman" if cat.get("org_code")
                                      else "")
            survey_t.write({
                "document_key": key, **sv,
                "school_code": cat.get("school_code", ""),
                "organization_code": cat.get("org_code", ""),
                "municipality_code": muni,
            })
        if text_t is not None and ex.get("text_chars"):
            tp = os.path.join(INDEX_DIR, "text", f"{key}.txt")
            content = ""
            if os.path.exists(tp):
                try:
                    with open(tp, encoding="utf-8") as f:
                        content = f.read()
                except OSError:
                    content = ""
            if content:
                text_t.write({
                    "document_key": key, "version_no": 1,
                    "extraction_method": ex.get("text_status", ""),
                    "char_count": ex.get("text_chars", ""),
                    "content": content,
                })

        for pos, (dno, kind) in enumerate(labeled):
            dno = normalize_dno(dno)
            if dno:
                dnr_links.append((dno, key, kind, "dnr_ur_pdf_text", pos))
        n_docs += 1

    manifest.append(doc_t.close())
    manifest.append(ver_t.close())
    manifest.append(survey_t.close())
    if text_t is not None:
        manifest.append(text_t.close())

    if n_skipped_product:
        log.info("  (%d dokument utanför produktomfånget)", n_skipped_product)
    if n_no_file:
        log.info("  (%d katalogposter utan hämtad fil)", n_no_file)

    # ── cases ─────────────────────────────────────────────────────────────
    # Diariet är primärkälla. data.json används för ärenden diariet ännu inte
    # täcker, så att exporten fungerar även innan diarieskörden är klar.
    case_t = Table("cases", [
        "diarienummer", "case_year", "case_no", "diary_series", "diary_caseref",
        "subject", "case_type", "status", "department", "municipality_code",
        "municipality_name", "registered_date", "closed_date", "source"])

    known: set[str] = set()
    for r in diarium:
        dno = normalize_dno(r.get("dno_si") or r.get("diarienummer") or "")
        if not dno or dno in known:
            continue
        known.add(dno)
        case_t.write({
            "diarienummer": dno,
            "case_year": r.get("year", ""),
            "case_no": r.get("case_no", ""),
            "diary_series": r.get("diaryref", ""),
            "diary_caseref": r.get("caseref", ""),
            "subject": r.get("arendemening", ""),
            "case_type": r.get("arendetyp", ""),
            "status": r.get("status", ""),
            "department": r.get("avdelning", ""),
            "municipality_code": muni_code_from_name(r.get("kommun", "")),
            "municipality_name": r.get("kommun", ""),
            "registered_date": r.get("reg_datum", ""),
            "closed_date": r.get("avsl_datum", ""),
            "source": "diarium",
        })

    n_from_json = 0
    if os.path.exists(DATA_JSON):
        for c in load_json_file(DATA_JSON):
            dno = normalize_dno(c.get("dno", ""))
            if not dno or dno in known:
                continue
            known.add(dno)
            n_from_json += 1
            m = re.match(r"^SI (\d{4}):(\d+)$", dno)
            case_t.write({
                "diarienummer": dno,
                "case_year": m.group(1) if m else "",
                "case_no": m.group(2) if m else "",
                "diary_series": "", "diary_caseref": "",
                "subject": c.get("subject", ""),
                "case_type": c.get("typ", ""),
                "status": "", "department": "",
                "municipality_code": muni_code_from_name(c.get("kommun", "")),
                "municipality_name": c.get("kommun", ""),
                "registered_date": c.get("date", ""),
                "closed_date": "",
                "source": "data_json",
            })
    if n_from_json:
        log.info("  (%d ärenden kompletterade ur data.json — diariet ännu ofullständigt)",
                 n_from_json)
    manifest.append(case_t.close())

    # ── case_documents ────────────────────────────────────────────────────
    link_t = Table("case_documents", [
        "diarienummer", "document_key", "link_type", "link_method",
        "link_confidence", "dnr_position"])
    n_orphan = 0
    for dno, key, kind, method, pos in dnr_links:
        in_diary = dno in known
        if kind == "own":
            conf = "high" if in_diary else "unmatched_case"
        else:
            conf = "reference" if in_diary else "reference_unmatched"
        if not in_diary:
            n_orphan += 1
            continue        # FK skulle inte gå att lösa upp
        link_t.write({
            "diarienummer": dno, "document_key": key,
            "link_type": "own_dnr" if kind == "own" else "mentioned",
            "link_method": method, "link_confidence": conf,
            "dnr_position": pos,
        })
    if n_orphan:
        log.info("  (%d kopplingar utelämnade: diarienumret saknas i ärendetabellen)",
                 n_orphan)
    manifest.append(link_t.close())

    # ── load.sql + manifest ───────────────────────────────────────────────
    write_load_sql(args)
    total_rows = sum(m["rows"] for m in manifest)
    with open(os.path.join(EXPORT_DIR, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump({
            "generated_at": generated_at,
            "product_scope": args.product,
            "includes_text": bool(args.with_text),
            "storage_prefix": STORAGE_PREFIX,
            "total_rows": total_rows,
            "tables": manifest,
        }, f, ensure_ascii=False, indent=2)

    log.info("=" * 72)
    log.info("Export klar: %d rader i %d tabeller", total_rows, len(manifest))
    log.info("  %s", EXPORT_DIR)
    log.info("Ladda med:  psql -f load.sql")
    log.info("=" * 72)
    return 0


def write_load_sql(args) -> None:
    """
    Laddningsskript: staging-tabeller, COPY, sedan upsert med FK-upplösning.

    Naturliga nycklar löses upp mot löpnummer här i stället för i exporten,
    vilket gör CSV-filerna oberoende av databasens tillstånd.
    """
    sql = r"""-- load.sql — laddar exporten in i måldatamodellen.
--
-- Kör migrationerna 0001–0004 först. Index (0005) och vyer (0006) läggs på
-- EFTER laddningen — att skapa index efter COPY är väsentligt snabbare.
--
--   psql "$DATABASE_URL" -f migrations/0001_core.sql
--   psql "$DATABASE_URL" -f migrations/0002_documents.sql
--   psql "$DATABASE_URL" -f migrations/0003_beslut.sql
--   psql "$DATABASE_URL" -f migrations/0004_skolenkaten.sql
--   psql "$DATABASE_URL" -v export_dir="$(pwd)/export" -f export/load.sql
--   psql "$DATABASE_URL" -f migrations/0005_indexes.sql
--   psql "$DATABASE_URL" -f migrations/0006_views.sql
--
-- Skriptet är idempotent: samma export kan laddas om utan att dubblera något.
-- \copy används i stället för COPY så att filerna läses av klienten och inte
-- kräver serverside-åtkomst.

\set ON_ERROR_STOP on
\timing on

BEGIN;

CREATE SCHEMA IF NOT EXISTS stg;

DROP TABLE IF EXISTS stg.municipalities, stg.organizations, stg.schools,
    stg.documents, stg.document_versions, stg.cases, stg.case_documents,
    stg.survey_reports, stg.import_runs, stg.document_texts CASCADE;

-- Staging: allt som text, ingen validering. Konvertering sker vid insert.
CREATE TABLE stg.import_runs      (run_key text, tool text, source_system text,
                                   started_at text, finished_at text, status text,
                                   params text, stats text, notes text);
CREATE TABLE stg.municipalities   (code text, name text);
CREATE TABLE stg.organizations    (code text, name text, legal_form text,
                                   is_current text, municipality_code text);
CREATE TABLE stg.schools          (code text, name text, organization_code text,
                                   municipality_code text, school_forms text,
                                   is_current text, contact text, statistics text);
CREATE TABLE stg.documents        (document_key text, source_system text, source_id text,
                                   product text, document_type text, title text,
                                   document_year text, document_date text,
                                   review_area text, source_url text,
                                   school_code text, organization_code text,
                                   municipality_code text, publication_status text,
                                   legacy_diarienummer text);
CREATE TABLE stg.document_versions(document_key text, version_no text, storage_key text,
                                   file_name text, mime_type text, file_kind text,
                                   file_size text, sha256 text, http_status text,
                                   download_status text, error_message text,
                                   downloaded_at text, is_current text);
CREATE TABLE stg.cases            (diarienummer text, case_year text, case_no text,
                                   diary_series text, diary_caseref text, subject text,
                                   case_type text, status text, department text,
                                   municipality_code text, municipality_name text,
                                   registered_date text, closed_date text, source text);
CREATE TABLE stg.case_documents   (diarienummer text, document_key text, link_type text,
                                   link_method text, link_confidence text,
                                   dnr_position text);
CREATE TABLE stg.survey_reports   (document_key text, report_level text,
                                   respondent_group text, school_form text, grade text,
                                   term text, year text, school_code text,
                                   organization_code text, municipality_code text);
CREATE TABLE stg.document_texts   (document_key text, version_no text,
                                   extraction_method text, char_count text, content text);

\copy stg.import_runs       FROM :'export_dir'/import_runs.csv       WITH (FORMAT csv, HEADER true)
\copy stg.municipalities    FROM :'export_dir'/municipalities.csv    WITH (FORMAT csv, HEADER true)
\copy stg.organizations     FROM :'export_dir'/organizations.csv     WITH (FORMAT csv, HEADER true)
\copy stg.schools           FROM :'export_dir'/schools.csv           WITH (FORMAT csv, HEADER true)
\copy stg.documents         FROM :'export_dir'/documents.csv         WITH (FORMAT csv, HEADER true)
\copy stg.document_versions FROM :'export_dir'/document_versions.csv WITH (FORMAT csv, HEADER true)
\copy stg.cases             FROM :'export_dir'/cases.csv             WITH (FORMAT csv, HEADER true)
\copy stg.case_documents    FROM :'export_dir'/case_documents.csv    WITH (FORMAT csv, HEADER true)
\copy stg.survey_reports    FROM :'export_dir'/survey_reports.csv    WITH (FORMAT csv, HEADER true)

-- ── Proveniens ────────────────────────────────────────────────────────────
INSERT INTO core.import_runs (tool, source_system, started_at, finished_at,
                              status, params, stats, notes)
SELECT tool, source_system, started_at::timestamptz, finished_at::timestamptz,
       status, params::jsonb, stats::jsonb, notes
FROM stg.import_runs;

-- ── Geografi och organisationer ───────────────────────────────────────────
INSERT INTO core.municipalities (code, name)
SELECT code, name FROM stg.municipalities
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO core.organizations (code, name, legal_form, is_current, municipality_code)
SELECT s.code, s.name, nullif(s.legal_form,''), s.is_current::boolean,
       nullif(s.municipality_code,'')
FROM stg.organizations s
ON CONFLICT (code) DO UPDATE
   SET name = EXCLUDED.name,
       legal_form = EXCLUDED.legal_form,
       is_current = EXCLUDED.is_current,
       updated_at = now();

INSERT INTO core.schools (code, name, organization_id, municipality_code,
                          school_forms, is_current, contact, statistics)
SELECT s.code, s.name, o.id,
       -- Kommunkod släpps om den inte finns i kommuntabellen, hellre än att
       -- fälla hela laddningen på en okänd kod.
       (SELECT m.code FROM core.municipalities m WHERE m.code = s.municipality_code),
       COALESCE(s.school_forms, '{}')::text[],
       s.is_current::boolean,
       COALESCE(nullif(s.contact,''), '{}')::jsonb,
       COALESCE(nullif(s.statistics,''), '{}')::jsonb
FROM stg.schools s
LEFT JOIN core.organizations o ON o.code = nullif(s.organization_code,'')
ON CONFLICT (code) DO UPDATE
   SET name = EXCLUDED.name,
       organization_id = EXCLUDED.organization_id,
       municipality_code = EXCLUDED.municipality_code,
       school_forms = EXCLUDED.school_forms,
       contact = EXCLUDED.contact,
       statistics = EXCLUDED.statistics,
       updated_at = now();

-- ── Dokument ──────────────────────────────────────────────────────────────
INSERT INTO core.documents (document_key, source_system, source_id, product,
    document_type, title, document_year, document_date, review_area, source_url,
    school_id, organization_id, municipality_code, publication_status)
SELECT d.document_key, d.source_system, d.source_id, d.product,
       nullif(d.document_type,''), nullif(d.title,''),
       nullif(d.document_year,'')::smallint,
       nullif(d.document_date,'')::date,
       nullif(d.review_area,''), nullif(d.source_url,''),
       sc.id, o.id,
       (SELECT m.code FROM core.municipalities m WHERE m.code = d.municipality_code),
       d.publication_status
FROM stg.documents d
LEFT JOIN core.schools       sc ON sc.code = nullif(d.school_code,'')
LEFT JOIN core.organizations o  ON o.code  = nullif(d.organization_code,'')
ON CONFLICT (document_key) DO UPDATE
   SET product = EXCLUDED.product,
       document_type = EXCLUDED.document_type,
       title = EXCLUDED.title,
       document_year = EXCLUDED.document_year,
       review_area = EXCLUDED.review_area,
       school_id = EXCLUDED.school_id,
       organization_id = EXCLUDED.organization_id,
       municipality_code = EXCLUDED.municipality_code,
       updated_at = now();

INSERT INTO core.document_versions (document_id, version_no, storage_key, file_name,
    mime_type, file_kind, file_size, sha256, http_status, download_status,
    error_message, downloaded_at, is_current)
SELECT d.id, v.version_no::int, v.storage_key, v.file_name,
       nullif(v.mime_type,''), nullif(v.file_kind,''),
       nullif(v.file_size,'')::bigint, nullif(v.sha256,''),
       nullif(v.http_status,'')::int, nullif(v.download_status,''),
       nullif(v.error_message,''), nullif(v.downloaded_at,'')::timestamptz,
       v.is_current::boolean
FROM stg.document_versions v
JOIN core.documents d ON d.document_key = v.document_key
ON CONFLICT (storage_key) DO NOTHING;

UPDATE core.documents d
   SET current_version_id = v.id
  FROM core.document_versions v
 WHERE v.document_id = d.id AND v.is_current
   AND d.current_version_id IS DISTINCT FROM v.id;

INSERT INTO core.publication_events (document_id, document_version_id, event_type,
                                     actor, note)
SELECT d.id, d.current_version_id, 'imported', 'system',
       'Importerad från SIRIS-arkivet'
FROM core.documents d
WHERE NOT EXISTS (SELECT 1 FROM core.publication_events e
                   WHERE e.document_id = d.id AND e.event_type = 'imported');

-- ── Ärenden ───────────────────────────────────────────────────────────────
INSERT INTO beslut.cases (diarienummer, case_year, case_no, diary_series,
    diary_caseref, subject, case_type, status, department, municipality_code,
    municipality_name, registered_date, closed_date, source)
SELECT c.diarienummer, nullif(c.case_year,'')::smallint, nullif(c.case_no,'')::int,
       nullif(c.diary_series,''), nullif(c.diary_caseref,'')::int,
       nullif(c.subject,''), nullif(c.case_type,''), nullif(c.status,''),
       nullif(c.department,''),
       (SELECT m.code FROM core.municipalities m WHERE m.code = c.municipality_code),
       nullif(c.municipality_name,''),
       nullif(c.registered_date,'')::date, nullif(c.closed_date,'')::date,
       c.source
FROM stg.cases c
ON CONFLICT (diarienummer) DO UPDATE
   SET subject = COALESCE(EXCLUDED.subject, beslut.cases.subject),
       case_type = COALESCE(EXCLUDED.case_type, beslut.cases.case_type),
       status = COALESCE(EXCLUDED.status, beslut.cases.status),
       department = COALESCE(EXCLUDED.department, beslut.cases.department),
       municipality_code = COALESCE(EXCLUDED.municipality_code,
                                    beslut.cases.municipality_code),
       municipality_name = COALESCE(EXCLUDED.municipality_name,
                                    beslut.cases.municipality_name),
       closed_date = COALESCE(EXCLUDED.closed_date, beslut.cases.closed_date),
       -- Diariet är primärkälla och får skriva över en rad som kom från data.json.
       source = CASE WHEN EXCLUDED.source = 'diarium' THEN 'diarium'
                     ELSE beslut.cases.source END,
       updated_at = now();

-- ── Koppling ──────────────────────────────────────────────────────────────
INSERT INTO beslut.case_documents (case_id, document_id, link_type, link_method,
                                   link_confidence, dnr_position)
SELECT c.id, d.id, l.link_type, l.link_method, l.link_confidence,
       nullif(l.dnr_position,'')::int
FROM stg.case_documents l
JOIN beslut.cases   c ON c.diarienummer = l.diarienummer
JOIN core.documents d ON d.document_key = l.document_key
ON CONFLICT (case_id, document_id, link_type) DO NOTHING;

-- ── Skolenkäten ───────────────────────────────────────────────────────────
INSERT INTO skolenkaten.survey_reports (document_id, report_level, respondent_group,
    school_form, grade, term, year, school_id, organization_id, municipality_code)
SELECT d.id, nullif(s.report_level,''), nullif(s.respondent_group,''),
       nullif(s.school_form,''), nullif(s.grade,''), nullif(s.term,''),
       nullif(s.year,'')::smallint, sc.id, o.id,
       (SELECT m.code FROM core.municipalities m WHERE m.code = s.municipality_code)
FROM stg.survey_reports s
JOIN core.documents d ON d.document_key = s.document_key
LEFT JOIN core.schools       sc ON sc.code = nullif(s.school_code,'')
LEFT JOIN core.organizations o  ON o.code  = nullif(s.organization_code,'')
ON CONFLICT (document_id) DO NOTHING;

DROP SCHEMA stg CASCADE;

COMMIT;

ANALYZE;
"""
    if args.with_text:
        sql = sql.replace(
            "\\copy stg.survey_reports    FROM",
            "\\copy stg.document_texts    FROM :'export_dir'/document_texts.csv "
            "WITH (FORMAT csv, HEADER true)\n"
            "\\copy stg.survey_reports    FROM")
        sql = sql.replace(
            "DROP SCHEMA stg CASCADE;",
            """INSERT INTO core.document_texts (document_version_id, extraction_method,
                                char_count, content)
SELECT v.id, nullif(t.extraction_method,''), nullif(t.char_count,'')::int, t.content
FROM stg.document_texts t
JOIN core.documents d ON d.document_key = t.document_key
JOIN core.document_versions v ON v.document_id = d.id
                             AND v.version_no = t.version_no::int
ON CONFLICT (document_version_id) DO NOTHING;

DROP SCHEMA stg CASCADE;""")

    with open(os.path.join(EXPORT_DIR, "load.sql"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(sql)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
