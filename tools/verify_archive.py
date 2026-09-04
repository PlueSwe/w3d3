#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_archive.py — bevisar att arkivet är komplett och korrekt.

Grundprincip: HTTP 200 är INTE bevis för att ett dokument är migrerat.
Varje fil kontrolleras mot disk och mot sitt faktiska innehåll.

Output (under SIRIS_ROOT/reports/):
    archive-verification.md
    archive-verification.csv

Användning:
    python tools/verify_archive.py
    python tools/verify_archive.py --no-rehash    # snabbare, svagare kontroll
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    BESLUT_JSON, CASE_DOCS_CSV, CASES_CSV, CATALOG_JSONL, DATA_JSON,
    DOCUMENTS_CSV, DOCUMENTS_JSONL, INDEX_DIR, PDF_DIR, REPORT_DIR, ROOT,
    ensure_dirs, fmt_bytes, load_json_file, normalize_dno, read_jsonl,
)

REPORT_MD = os.path.join(REPORT_DIR, "archive-verification.md")
REPORT_CSV = os.path.join(REPORT_DIR, "archive-verification.csv")
CASE_DOCS_JSONL = os.path.join(INDEX_DIR, "case_documents.jsonl")

SUCCESS = {"ok", "already_archived", "imported"}

# Ärendetyper som i praktiken aldrig ger ett publicerat beslut.
TYPES_WITHOUT_DECISIONS = {
    "uppgift", "begäran", "avtal", "annons", "makulerat",
    "överklagande", "återkallande",
}

CSV_FIELDS = [
    "document_id", "docid", "title", "document_type", "year",
    "primary_diarienummer", "diarienummer_list", "dnr_count",
    "local_path", "file_size", "sha256", "http_status", "download_status",
    "verification_status", "issues", "duplicate_of", "text_status",
    "county_name", "org_name", "school_name", "source_url",
]


def inspect_pdf(path: str, rehash: bool) -> dict:
    out = {"size": 0, "sha256": "", "is_pdf": False, "has_eof": False,
           "has_root": False, "has_pages": False, "issues": []}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        out["issues"].append(f"kan inte läsa filen: {exc}")
        return out
    out["size"] = size
    if size == 0:
        out["issues"].append("filen är tom (0 byte)")
        return out

    h = hashlib.sha256()
    head = b""
    tail = b""
    found_root = found_pages = False
    carry = b""
    try:
        with open(path, "rb") as f:
            first = True
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                if rehash:
                    h.update(chunk)
                if first:
                    head = chunk[:2048]
                    first = False
                win = carry + chunk
                if not found_root and b"/Root" in win:
                    found_root = True
                if not found_pages and (b"/Pages" in win or b"/Type/Page" in win
                                        or b"/Type /Page" in win):
                    found_pages = True
                carry = chunk[-32:]
                tail = chunk[-2048:]
    except OSError as exc:
        out["issues"].append(f"läsfel: {exc}")
        return out

    if rehash:
        out["sha256"] = h.hexdigest()
    out["has_root"] = found_root
    out["has_pages"] = found_pages
    out["is_pdf"] = head.lstrip()[:5] == b"%PDF-"
    out["has_eof"] = b"%%EOF" in tail

    if not out["is_pdf"]:
        out["issues"].append(f"inte en PDF – börjar med {head[:60]!r}")
        low = head.lower()
        if b"<html" in low or b"<!doctype" in low:
            out["issues"].append("innehållet ser ut som en HTML-sida")
    else:
        if not out["has_eof"]:
            out["issues"].append("saknar %%EOF – kan vara trunkerad")
        if not out["has_root"]:
            out["issues"].append("saknar /Root – ofullständig PDF-struktur")
        if not out["has_pages"]:
            out["issues"].append("hittar inga sidobjekt")
        if size < 1024:
            out["issues"].append(f"misstänkt liten PDF ({size} byte)")
    return out


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f} %" if whole else "–"


def read_csv_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Verifierar dokumentarkivet.")
    ap.add_argument("--no-rehash", action="store_true")
    args = ap.parse_args(argv)
    rehash = not args.no_rehash

    ensure_dirs()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("Läser index ...")
    documents = read_jsonl(DOCUMENTS_JSONL)
    if not documents:
        print(f"FEL: {DOCUMENTS_JSONL} saknas. Kör tools/build_index.py först.",
              file=sys.stderr)
        return 2
    links = read_jsonl(CASE_DOCS_JSONL)
    cases_rows = read_csv_rows(CASES_CSV)
    catalog_ids = {r["docid"] for r in read_jsonl(CATALOG_JSONL) if r.get("docid")}

    cases_raw = load_json_file(DATA_JSON)
    legacy = {}
    if os.path.exists(BESLUT_JSON):
        try:
            legacy = load_json_file(BESLUT_JSON)
        except Exception:
            legacy = {}

    print(f"  {len(documents)} dokument, {len(links)} kopplingar, "
          f"{len(cases_raw)} ärenden")
    print(f"Kontrollerar filer ({'med' if rehash else 'utan'} SHA-256-omräkning) ...")

    rows: list[dict] = []
    by_hash: dict[str, list[str]] = collections.defaultdict(list)
    n_ok = n_failed = n_missing = n_mismatch = n_notpdf = n_suspect = 0
    total_bytes = 0
    broken: list[dict] = []
    http_counts: collections.Counter = collections.Counter()

    for i, doc in enumerate(documents, 1):
        if i % 2000 == 0:
            print(f"  {i}/{len(documents)} ...")
        issues: list[str] = []
        row = {k: doc.get(k, "") for k in CSV_FIELDS if k in doc}
        row["duplicate_of"] = ""
        http_counts[str(doc.get("http_status", "")) or "–"] += 1

        status = doc.get("download_status", "")
        if status not in SUCCESS:
            n_failed += 1
            issues.append(f"nedladdning misslyckades ({status})")
            if str(doc.get("http_status")) in ("404", "410"):
                broken.append(doc)
            row["verification_status"] = "failed_download"
            row["issues"] = "; ".join(issues)
            rows.append(row)
            continue

        path = os.path.join(ROOT, str(doc.get("local_path", "")).replace("/", os.sep))
        if not os.path.exists(path):
            n_missing += 1
            row["verification_status"] = "missing_file"
            row["issues"] = "filen saknas på disk trots status i indexet"
            rows.append(row)
            continue

        info = inspect_pdf(path, rehash)
        total_bytes += info["size"]
        row["file_size"] = info["size"]

        if rehash and info["sha256"]:
            row["sha256"] = info["sha256"]
            exp = doc.get("sha256", "")
            if exp and exp != info["sha256"]:
                n_mismatch += 1
                issues.append(f"SHA-256 avviker (index {exp[:12]}…, "
                              f"fil {info['sha256'][:12]}…)")
            by_hash[info["sha256"]].append(doc["document_id"])
        elif doc.get("sha256"):
            by_hash[doc["sha256"]].append(doc["document_id"])

        issues.extend(info["issues"])
        if not info["is_pdf"]:
            n_notpdf += 1
            row["verification_status"] = "not_pdf"
        elif info["issues"]:
            n_suspect += 1
            row["verification_status"] = "suspect_pdf"
        else:
            n_ok += 1
            row["verification_status"] = "verified"
        row["issues"] = "; ".join(issues)
        rows.append(row)

    # ── Dubbletter ──
    dup_groups = {h: ids for h, ids in by_hash.items() if len(ids) > 1}
    dup_docs = sum(len(v) for v in dup_groups.values())
    dup_redundant = dup_docs - len(dup_groups)
    first_of = {}
    for h, ids in dup_groups.items():
        f0 = sorted(ids)[0]
        for d in ids:
            first_of[d] = f0
    for r in rows:
        d = r.get("document_id")
        if d in first_of and first_of[d] != d:
            r["duplicate_of"] = first_of[d]

    # ── Ärendetäckning ──
    verified_ids = {r["document_id"] for r in rows
                    if r.get("verification_status") == "verified"}
    cases_with_doc = {r["diarienummer"] for r in cases_rows
                      if r.get("has_document") == "ja"}
    cases_with_verified = {l["diarienummer"] for l in links
                           if l["document_id"] in verified_ids
                           and l.get("link_confidence") == "high"}
    cases_multi = [r for r in cases_rows
                   if int(r.get("document_count") or 0) > 1]
    docs_multi_dnr = [d for d in documents if int(d.get("dnr_count") or 0) > 1]
    docs_no_dnr = [d for d in documents
                   if d.get("download_status") in SUCCESS
                   and int(d.get("dnr_count") or 0) == 0]
    unmatched_links = [l for l in links if l.get("link_confidence") != "high"]
    unmatched_dnos = sorted({l["diarienummer"] for l in unmatched_links})

    eligible_no_doc = 0
    for c in cases_raw:
        dno = normalize_dno(c.get("dno", ""))
        if dno in cases_with_doc:
            continue
        typ = str(c.get("typ", "")).lower()
        if any(k in typ for k in TYPES_WITHOUT_DECISIONS):
            continue
        eligible_no_doc += 1

    # ── Disk vs index ──
    on_disk = set()
    for fn in os.listdir(PDF_DIR) if os.path.isdir(PDF_DIR) else []:
        if fn.endswith((".pdf", ".bin")):
            on_disk.add(f"pdf/{fn}")
    indexed = {str(d.get("local_path", "")) for d in documents}
    orphans = sorted(on_disk - indexed)
    parts = sorted(fn for fn in (os.listdir(PDF_DIR) if os.path.isdir(PDF_DIR) else [])
                   if fn.endswith(".part"))
    not_downloaded = sorted(catalog_ids - {d["docid"] for d in documents})

    # ── CSV ──
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x.get("verification_status", ""),
                                             str(x.get("docid", "")))):
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Skrev {REPORT_CSV} ({len(rows)} rader)")

    # ── Markdown ──
    md: list[str] = []
    a = md.append
    total = len(documents)

    a("# Arkivverifiering – beslutsdokument")
    a("")
    a(f"Genererad: {generated_at}")
    a(f"Arkivrot: `{ROOT}`")
    a(f"SHA-256 omräknad från disk: {'ja' if rehash else 'NEJ (--no-rehash)'}")
    a("")
    a("> Ett dokument räknas som **verifierat** endast om filen finns på disk, "
      "checksumman stämmer mot indexet, filen inleds med `%PDF-`, avslutas med "
      "`%%EOF` och har läsbar PDF-struktur. HTTP 200 räknas inte som bevis.")
    a("")

    a("## Nyckeltal")
    a("")
    a("| Mått | Antal | Andel |")
    a("|---|---:|---:|")
    a(f"| Ärenden totalt i diariet | {len(cases_raw)} | 100 % |")
    a(f"| Dokument i katalogen | {len(catalog_ids)} | – |")
    a(f"| Dokument i indexet | {total} | – |")
    a(f"| **Verifierade dokument** | **{n_ok}** | {pct(n_ok, total)} |")
    a(f"| Misstänkta PDF:er | {n_suspect} | {pct(n_suspect, total)} |")
    a(f"| Nedladdade men inte PDF | {n_notpdf} | {pct(n_notpdf, total)} |")
    a(f"| Misslyckade nedladdningar | {n_failed} | {pct(n_failed, total)} |")
    a(f"| Brutna länkar (HTTP 404/410) | {len(broken)} | {pct(len(broken), total)} |")
    a(f"| Filer som saknas på disk | {n_missing} | {pct(n_missing, total)} |")
    a(f"| Checksummeavvikelser | {n_mismatch} | {pct(n_mismatch, total)} |")
    a(f"| Dubbletter (identiskt innehåll) | {dup_redundant} i {len(dup_groups)} grupper | {pct(dup_redundant, total)} |")
    a(f"| Katalogposter utan nedladdning | {len(not_downloaded)} | {pct(len(not_downloaded), max(1, len(catalog_ids)))} |")
    a("")
    a("### Koppling ärende ↔ dokument")
    a("")
    a("| Mått | Antal | Andel |")
    a("|---|---:|---:|")
    a(f"| Kopplingar totalt | {len(links)} | – |")
    a(f"| Ärenden med minst ett dokument | {len(cases_with_doc)} | {pct(len(cases_with_doc), len(cases_raw))} |")
    a(f"| Ärenden med minst ett **verifierat** dokument | {len(cases_with_verified)} | {pct(len(cases_with_verified), len(cases_raw))} |")
    a(f"| Ärenden med **flera** dokument | {len(cases_multi)} | {pct(len(cases_multi), len(cases_raw))} |")
    a(f"| Dokument som nämner flera diarienummer | {len(docs_multi_dnr)} | {pct(len(docs_multi_dnr), total)} |")
    a(f"| Dokument utan diarienummer i texten | {len(docs_no_dnr)} | {pct(len(docs_no_dnr), total)} |")
    a(f"| Diarienummer utan matchande ärende | {len(unmatched_dnos)} | – |")
    a(f"| **Total lagringsmängd** | **{fmt_bytes(total_bytes)}** | – |")
    a("")

    if legacy:
        a("### Jämfört med tidigare koppling (`beslut.json`)")
        a("")
        a("| | Tidigare | Nu | Faktor |")
        a("|---|---:|---:|---:|")
        a(f"| Kända dokument | {len(legacy)} | {total} | "
          f"{total / max(1, len(legacy)):.1f}× |")
        a(f"| Ärenden med dokument | {len(legacy)} | {len(cases_with_doc)} | "
          f"{len(cases_with_doc) / max(1, len(legacy)):.1f}× |")
        a(f"| Ärenden med flera dokument | 0 (omöjligt) | {len(cases_multi)} | – |")
        a("")

    a("## Bedömning")
    a("")
    complete = (n_failed == 0 and n_missing == 0 and n_mismatch == 0
                and n_notpdf == 0 and len(not_downloaded) == 0)
    if complete and n_suspect == 0:
        a(f"**Arkivet är komplett mot katalogen.** Samtliga {n_ok} dokument som "
          f"Skolinspektionens API listar är nedladdade och verifierade som "
          f"giltiga PDF:er.")
    elif complete:
        a(f"**Samtliga {total} katalogförda dokument är nedladdade och "
          f"strukturellt giltiga**, men {n_suspect} har anmärkningar som bör "
          f"granskas.")
    else:
        a(f"**Arkivet är inte komplett.** {n_failed} nedladdningar misslyckades, "
          f"{n_missing} filer saknas, {n_mismatch} har checksummeavvikelse, "
          f"{n_notpdf} är inte PDF och {len(not_downloaded)} katalogposter är "
          f"inte hämtade. Kör `python tools/fetch_pdfs.py --retry-failed`.")
    a("")
    a("### Vad som fortfarande kan saknas")
    a("")
    a(f"Siffrorna mäter arkivet mot **Skolinspektionens publika katalog**. "
      f"Kvarstående osäkerheter:")
    a("")
    a(f"- **{len(cases_raw) - len(cases_with_doc)}** ärenden saknar dokument. "
      f"Av dessa har **{eligible_no_doc}** en ärendetyp som normalt ger ett "
      f"publicerat beslut. Myndigheten publicerar dock inte allt — individärenden "
      f"undantas uttryckligen.")
    a(f"- **{len(docs_no_dnr)}** nedladdade dokument saknar diarienummer i texten "
      f"och kan därför inte kopplas till ett ärende. Det gäller framför allt "
      f"Skolenkäten och statistikrapporter, som saknar diarienummer i källan.")
    a("- Skolenheter som lagts ner listas inte under `schools/current`. Deras "
      "dokument nås via historiska huvudmän, men täckningen kan inte bevisas "
      "utan en fullständig svepning av SIRIS docID-rymden "
      "(`tools/sweep_docids.py`).")
    a("")

    # Dokumenttyper
    a("## Dokumenttyper")
    a("")
    types = collections.Counter(d.get("document_type") or "okant" for d in documents)
    a("| Typ | Antal | Andel |")
    a("|---|---:|---:|")
    for t, n in types.most_common():
        a(f"| {t} | {n} | {pct(n, total)} |")
    a("")

    # Per år
    a("## Dokument per år")
    a("")
    per_year = collections.Counter(str(d.get("year") or "okänt") for d in documents)
    a("| År | Dokument |")
    a("|---|---:|")
    for y, n in sorted(per_year.items()):
        a(f"| {y} | {n} |")
    a("")

    a("## HTTP-statusfördelning")
    a("")
    a("| Status | Antal |")
    a("|---|---:|")
    for st, n in sorted(http_counts.items(), key=lambda x: (-x[1], x[0])):
        a(f"| {st} | {n} |")
    a("")

    def table(title: str, items: list[dict], cols: list[tuple[str, str]],
              limit: int = 50, empty: str = "Inga.") -> None:
        a(f"## {title} ({len(items)})")
        a("")
        if not items:
            a(empty)
            a("")
            return
        a("| " + " | ".join(c[0] for c in cols) + " |")
        a("|" + "|".join(["---"] * len(cols)) + "|")
        for it in items[:limit]:
            a("| " + " | ".join(
                str(it.get(k, "") or "").replace("|", "\\|")[:110]
                for _l, k in cols) + " |")
        if len(items) > limit:
            a("")
            a(f"*Visar {limit} av {len(items)}. Fullständig lista i "
              f"`reports/archive-verification.csv`.*")
        a("")

    table("Misslyckade nedladdningar",
          [r for r in rows if r.get("verification_status") == "failed_download"],
          [("Dokument", "document_id"), ("Titel", "title"),
           ("HTTP", "http_status"), ("Status", "download_status")],
          empty="Inga misslyckade nedladdningar.")

    table("Brutna länkar (HTTP 404/410)", broken,
          [("Dokument", "document_id"), ("Titel", "title"), ("URL", "source_url")],
          empty="Inga brutna länkar.")

    table("HTTP 200 men innehållet är inte PDF",
          [r for r in rows if r.get("verification_status") == "not_pdf"],
          [("Dokument", "document_id"), ("MIME", "mime_type"), ("Anmärkning", "issues")],
          empty="Inga. Samtliga hämtade filer är PDF-filer.")

    table("PDF:er med strukturanmärkning",
          [r for r in rows if r.get("verification_status") == "suspect_pdf"],
          [("Dokument", "document_id"), ("Storlek", "file_size"),
           ("Anmärkning", "issues")],
          empty="Inga. Samtliga PDF:er har giltig struktur.")

    table("Filer som saknas på disk",
          [r for r in rows if r.get("verification_status") == "missing_file"],
          [("Dokument", "document_id"), ("Förväntad sökväg", "local_path")],
          empty="Inga.")

    # Dubbletter
    a(f"## Dubbletter ({len(dup_groups)} grupper, {dup_redundant} redundanta)")
    a("")
    if not dup_groups:
        a("Inga dubbletter.")
        a("")
    else:
        a("Filer med identisk SHA-256. Samma dokument kan legitimt vara "
          "publicerat under flera noder i katalogen, eller gälla flera ärenden.")
        a("")
        a("| SHA-256 | Antal | Dokument |")
        a("|---|---:|---|")
        for h, ids in sorted(dup_groups.items(), key=lambda x: -len(x[1]))[:50]:
            a(f"| `{h[:16]}…` | {len(ids)} | {', '.join(sorted(ids)[:5])} |")
        if len(dup_groups) > 50:
            a("")
            a(f"*Visar 50 av {len(dup_groups)} grupper.*")
        a("")

    # Flera dokument per ärende
    a(f"## Ärenden med flera dokument ({len(cases_multi)})")
    a("")
    if cases_multi:
        a("Detta var **omöjligt att representera** i den tidigare datamodellen.")
        a("")
        a("| Diarienummer | Antal | Ärendetyp | Dokument |")
        a("|---|---:|---|---|")
        for r in sorted(cases_multi,
                        key=lambda x: -int(x.get("document_count") or 0))[:50]:
            a(f"| {r['diarienummer']} | {r['document_count']} | "
              f"{str(r.get('typ',''))[:30]} | {str(r.get('document_ids',''))[:90]} |")
        if len(cases_multi) > 50:
            a("")
            a(f"*Visar 50 av {len(cases_multi)}.*")
    else:
        a("Inga.")
    a("")

    # Osäkra kopplingar
    a(f"## Diarienummer utan matchande ärende ({len(unmatched_dnos)})")
    a("")
    a("Dokumentet nämner ett diarienummer som inte finns i diariet "
      "(`data.json`). Vanligast för äldre beslut som föregår diariets "
      "startdatum 2019-01-01. Dokumentet är arkiverat oavsett.")
    a("")
    if unmatched_dnos:
        a("| Diarienummer | Dokument |")
        a("|---|---|")
        by_dno: dict[str, list[str]] = collections.defaultdict(list)
        for l in unmatched_links:
            by_dno[l["diarienummer"]].append(l["document_id"])
        for dno in unmatched_dnos[:50]:
            a(f"| {dno} | {', '.join(sorted(by_dno[dno])[:4])} |")
        if len(unmatched_dnos) > 50:
            a("")
            a(f"*Visar 50 av {len(unmatched_dnos)}.*")
    else:
        a("Inga.")
    a("")

    # Dokument utan dnr
    a(f"## Dokument utan diarienummer i texten ({len(docs_no_dnr)})")
    a("")
    a("Dessa kan inte kopplas till ett ärende. Fördelning per dokumenttyp:")
    a("")
    nod = collections.Counter(d.get("document_type") or "okant" for d in docs_no_dnr)
    a("| Typ | Antal |")
    a("|---|---:|")
    for t, n in nod.most_common():
        a(f"| {t} | {n} |")
    a("")
    txt = collections.Counter(str(d.get("text_status") or "") for d in docs_no_dnr)
    a("Textextraktionens utfall för dessa:")
    a("")
    a("| Extraktionsstatus | Antal |")
    a("|---|---:|")
    for t, n in txt.most_common(10):
        a(f"| {t or '–'} | {n} |")
    a("")

    a("## Arkivintegritet")
    a("")
    a(f"- Filer på disk utan indexpost: **{len(orphans)}**")
    for p in orphans[:20]:
        a(f"  - `{p}`")
    a(f"- Ofullbordade `.part`-filer: **{len(parts)}**")
    for p in parts[:20]:
        a(f"  - `{p}`")
    a(f"- Katalogposter som inte hämtats: **{len(not_downloaded)}**")
    if not_downloaded[:20]:
        a(f"  - docID: {', '.join(str(x) for x in not_downloaded[:20])}")
    a("")

    a("## Så reproduceras rapporten")
    a("")
    a("```bash")
    a("python tools/catalog_crawl.py     # bygg katalogen från Skolinspektionens API")
    a("python tools/fetch_pdfs.py        # ladda ner alla dokument")
    a("python tools/build_index.py       # koppla ärende ↔ dokument")
    a("python tools/verify_archive.py    # denna rapport")
    a("```")
    a("")

    with open(REPORT_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md))
    print(f"Skrev {REPORT_MD}")

    print()
    print("=" * 64)
    print(f"  Dokument i index:        {total}")
    print(f"  Verifierade:             {n_ok}")
    print(f"  Misstänkta:              {n_suspect}")
    print(f"  Ej PDF:                  {n_notpdf}")
    print(f"  Misslyckade:             {n_failed}")
    print(f"  Saknade filer:           {n_missing}")
    print(f"  Checksummeavvikelser:    {n_mismatch}")
    print(f"  Dubbletter:              {dup_redundant} i {len(dup_groups)} grupper")
    print(f"  Ärenden med dokument:    {len(cases_with_doc)} av {len(cases_raw)}")
    print(f"  Ärenden med flera dok:   {len(cases_multi)}")
    print(f"  Total lagring:           {fmt_bytes(total_bytes)}")
    print("=" * 64)
    return 0 if complete else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
