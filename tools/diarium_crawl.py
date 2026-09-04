#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diarium_crawl.py — hämtar Skolinspektionens diarium i sin helhet.

Dagens `data.json` innehåller fyra fält (dno, datum, ärendemening, ärendetyp)
för ärenden från 2019-01-01 och framåt. Diariets egen webbportal har mer:

  * ärenden tillbaka till 2008-10-13, då Skolinspektionen bildades
  * status ("Ad acta", "Pågående" …)
  * handläggande avdelning
  * KOMMUN som registrerat fält — inte gissat ur ärendemeningens text
  * avslutsdatum

Portalen har en detaljsida per ärende:

    case.aspx?diaryref=<serie>&caseref=<löpnummer>

`caseref` är en enda löpande sekvens över båda diarieserierna:

    serie 2 ("Skolinspektionen")            caseref 1 … 110948   (2008-10 … 2018-12)
    serie 6 ("Skolinspektionen 2019-01-01") caseref 110949 …     (2019-01 …)

Det gör diariet direkt uppräkningsbart, utan att paginera sökresultat.
Serien är gles — tomma caseref förekommer och loggas som sådana, så att
luckor är synliga i efterhand.

Output (under SIRIS_ROOT/index/):

    diarium.jsonl   en rad per prövat caseref (även tomma)
    diarium.csv     en rad per funnet ärende

Användning:
    python tools/diarium_crawl.py --probe          # hitta seriens övre gräns
    python tools/diarium_crawl.py                  # hämta allt
    python tools/diarium_crawl.py --from 1 --to 120000
    python tools/diarium_crawl.py --status
"""

from __future__ import annotations

import argparse
import csv
import html as html_mod
import os
import queue
import re
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_DIR, INDEX_DIR, USER_AGENT, JsonlWriter, RateLimiter, ensure_dirs,
    http_get_retry, read_jsonl, setup_logging, utcnow,
)

PORTAL = "https://externsearchport.skolinspektionen.se"
CASE_URL = PORTAL + "/case.aspx?diaryref={diaryref}&caseref={caseref}"

DIARIUM_JSONL = os.path.join(INDEX_DIR, "diarium.jsonl")
DIARIUM_CSV = os.path.join(INDEX_DIR, "diarium.csv")

# Gränsen mellan diarieserierna. Ärenden t.o.m. detta caseref ligger i serie 2,
# därefter i serie 6. Gränsen är empiriskt fastställd: serie 2 slutar
# 2018-12-31 på caseref 110948, serie 6 börjar 2019-01-01 på 110949.
SERIES_SPLIT = 110948

# En tom detaljsida är ca 2,3 kB; en med innehåll ca 5 kB.
EMPTY_PAGE_MAX = 3000

FIELDS = [
    "caseref", "diaryref", "diarienummer", "dno_si", "year", "case_no",
    "arendemening", "status", "avdelning", "kommun", "arendetyp",
    "reg_datum", "avsl_datum", "restricted", "url", "fetched_at",
]

DIARIUM_INDEX = os.path.join(CATALOG_DIR, "diarium_index.jsonl")

log = None

_SPAN_RE = re.compile(r'<span[^>]*id="(lblCase\w+)"[^>]*>(.*?)</span>', re.S | re.I)
_CELL_RE = re.compile(r'<td[^>]*class="(tdGuide|tdText)"[^>]*>(.*?)</td>', re.S | re.I)

# Etikett i portalen -> fältnamn i vår modell
_LABEL_MAP = {
    "avdelning": "avdelning",
    "kommun": "kommun",
    "ärendetyp": "arendetyp",
    "arendetyp": "arendetyp",
    "reg.datum": "reg_datum",
    "regdatum": "reg_datum",
    "avsl.datum": "avsl_datum",
    "avsldatum": "avsl_datum",
}


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_case(body: str, caseref: int, diaryref: int) -> dict | None:
    """
    Plockar ut ärendets fält ur detaljsidan.

    Sidan är uppbyggd av etikett/värde-par i <td class="tdGuide"> respektive
    <td class="tdText">. Status saknar etikett och står som ensam tdText före
    första tdGuide — den hanteras separat.
    """
    spans = {k: clean(v) for k, v in _SPAN_RE.findall(body)}
    dno = spans.get("lblCaseDno", "")
    if not dno:
        return None

    rec = {f: "" for f in FIELDS}
    rec["caseref"] = caseref
    rec["diaryref"] = diaryref
    rec["diarienummer"] = dno
    rec["arendemening"] = spans.get("lblCaseSubject", "")
    rec["url"] = CASE_URL.format(diaryref=diaryref, caseref=caseref)
    rec["fetched_at"] = utcnow()

    m = re.match(r"^(\d{4}):(\d+)$", dno)
    if m:
        rec["year"] = m.group(1)
        rec["case_no"] = m.group(2)
        # Nyckel på samma form som diarienumren i data.json och i besluten.
        rec["dno_si"] = f"SI {m.group(1)}:{int(m.group(2))}"

    cells = _CELL_RE.findall(body)
    pending_label = None
    seen_label = False
    for kind, raw in cells:
        val = clean(raw)
        if kind == "tdGuide":
            seen_label = True
            pending_label = re.sub(r"[\s:]+$", "", val).lower()
            continue
        # tdText
        if not val:
            pending_label = None
            continue
        if pending_label is None:
            # Ensam tdText före första etiketten = ärendets status.
            if not seen_label and not rec["status"]:
                rec["status"] = val
            continue
        field = _LABEL_MAP.get(pending_label.replace(" ", ""))
        if field and not rec[field]:
            rec[field] = val
        pending_label = None

    # Rubrikcellerna "Ärende (År:nr)"/"Ärendemening" är också tdGuide och kan
    # ha konsumerat statusvärdet. Fånga status separat om den saknas.
    if not rec["status"]:
        m2 = re.search(r'width="30%">\s*<table[^>]*>\s*<tr>\s*'
                       r'<td class="tdText">(.*?)</td>', body, re.S | re.I)
        if m2:
            rec["status"] = clean(m2.group(1))
    return rec


def diaryref_for(caseref: int) -> int:
    return 2 if caseref <= SERIES_SPLIT else 6


def fetch_case(caseref: int, limiter, args, diaryref: int | None = None) -> dict:
    if diaryref is None:
        diaryref = diaryref_for(caseref)
    url = CASE_URL.format(diaryref=diaryref, caseref=caseref)
    status, body, _ct, err, attempts = http_get_retry(
        url, limiter, log, tries=args.max_retries, timeout=args.timeout)

    base = {f: "" for f in FIELDS}
    base.update({"caseref": caseref, "diaryref": diaryref, "url": url,
                 "fetched_at": utcnow()})

    if status != 200 or not body:
        base["status"] = ""
        base["_state"] = "error"
        base["_http"] = status
        base["_error"] = err or f"status {status}"
        base["_attempts"] = attempts
        return base

    text = body.decode("utf-8", "replace")
    if len(text) < EMPTY_PAGE_MAX:
        base["_state"] = "empty"
        base["_http"] = status
        base["_error"] = ""
        base["_attempts"] = attempts
        return base

    rec = parse_case(text, caseref, diaryref)
    if rec is None:
        base["_state"] = "unparsed"
        base["_http"] = status
        base["_error"] = "sidan har innehåll men inget diarienummer"
        base["_attempts"] = attempts
        return base

    rec["_state"] = "ok"
    rec["_http"] = status
    rec["_error"] = ""
    rec["_attempts"] = attempts
    return rec


def find_upper_bound(limiter, args) -> int:
    """
    Fastställer högsta caseref genom att fråga portalens sökfunktion efter de
    senast registrerade ärendena.

    Att stega uppåt tills ett tomt caseref dyker upp fungerar inte: rymden är
    gles, och en stegning stannar då vid första luckan i stället för vid
    seriens slut. Sökningen svarar med de faktiska ärendena och deras caseref,
    vilket är entydigt.
    """
    import datetime
    import urllib.parse

    log.info("Fastställer övre gräns via portalens sökning ...")
    search_url = PORTAL + "/search.aspx?view=ac"

    status, body, _ct, err, _a = http_get_retry(
        search_url, limiter, log, tries=args.max_retries, timeout=args.timeout)
    if status != 200 or not body:
        log.error("Kunde inte hämta sökformuläret: %s", err)
        return 0
    page = body.decode("utf-8", "replace")

    def field(name: str) -> str:
        m = re.search(r'id="' + name + r'"\s+value="([^"]*)"', page)
        return m.group(1) if m else ""

    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=30)).isoformat()
    form = {
        "__VIEWSTATE": field("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": field("__EVENTVALIDATION"),
        "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
        "ddlDiaries": "", "txtSubject": "", "txtDno": "",
        "ddlCase_Type": "", "ddlDepartment": "", "ddlStatus": "",
        "txtFromDateReg": frm, "txtToDateReg": today.isoformat(),
        "txtFromDateEnd": "", "txtToDateEnd": "",
        "btnAdvancedSearch": "Sök",
    }
    data = urllib.parse.urlencode(form, encoding="utf-8").encode()
    req = urllib.request.Request(search_url, data=data, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": search_url,
    })
    limiter.wait()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout + 60) as r:
            result = r.read().decode("utf-8", "replace")
    except Exception as exc:
        log.error("Sökningen misslyckades: %s", exc)
        return 0

    refs = [int(m) for m in re.findall(r"caseref=(\d+)", result)]
    if not refs:
        log.error("Sökningen gav inga caseref (senaste 30 dagarna).")
        return 0
    top = max(refs)
    # Marginal för ärenden registrerade efter att sökningen gjordes.
    hi = top + 2000
    log.info("Högsta caseref i portalen: %d (senaste 30 dagarna, %d träffar)",
             top, len(set(refs)))
    log.info("Övre gräns satt till %d", hi)
    return hi


def probe(limiter, args) -> None:
    """Glesa stickprov för att hitta seriens övre gräns."""
    log.info("Kartlägger caseref-rymden ...")
    lo, hi = 1, args.probe_to
    last_hit = 0
    for cr in range(args.probe_from, hi + 1, args.probe_step):
        rec = fetch_case(cr, limiter, args)
        state = rec.get("_state")
        if state == "ok":
            last_hit = cr
            log.info("  caseref=%-7d %s  %s  %s", cr, rec["diarienummer"],
                     rec["reg_datum"], rec["arendemening"][:60])
        else:
            log.info("  caseref=%-7d %s", cr, state)
    log.info("Högsta träff i stickprovet: %d", last_hit)


def write_csv(rows: list[dict]) -> int:
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(DIARIUM_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(rows)


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(
        description="Hämtar Skolinspektionens diarium via detaljsidorna.")
    ap.add_argument("--from", dest="lo", type=int, default=1)
    ap.add_argument("--to", dest="hi", type=int, default=0,
                    help="högsta caseref (0 = auto, letar upp gränsen)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--max-retries", type=int, default=4)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-from", type=int, default=1000)
    ap.add_argument("--probe-to", type=int, default=240000)
    ap.add_argument("--probe-step", type=int, default=10000)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry-errors", action="store_true",
                    help="gör om caseref som gav nätverksfel")
    ap.add_argument("--from-index", action="store_true",
                    help="hämta bara de ärenden som diarium_enumerate.py hittat "
                         "(rekommenderat: rätt diaryref, inga tomma ID)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("diarium", args.verbose)

    done: dict[int, dict] = {}
    for r in read_jsonl(DIARIUM_JSONL):
        if r.get("caseref") is not None:
            done[r["caseref"]] = r

    if args.status:
        ok = [r for r in done.values() if r.get("_state") == "ok"]
        empty = [r for r in done.values() if r.get("_state") == "empty"]
        errs = [r for r in done.values() if r.get("_state") == "error"]
        print(f"Prövade caseref : {len(done)}")
        print(f"  ärenden       : {len(ok)}")
        print(f"  tomma         : {len(empty)}")
        print(f"  fel           : {len(errs)}")
        if ok:
            yrs = sorted({r.get("year") for r in ok if r.get("year")})
            print(f"  år            : {yrs[0]}–{yrs[-1]}" if yrs else "")
            refs = [r["caseref"] for r in ok if isinstance(r.get("caseref"), int)]
            if refs:
                print(f"  caseref-spann : {min(refs)}–{max(refs)}")
            print(f"  sekretessbelagda: "
                  f"{sum(1 for r in ok if r.get('restricted') == 'true')}")
        return 0

    limiter = RateLimiter(args.delay)

    if args.probe:
        probe(limiter, args)
        return 0

    # diaryref per caseref, från uppräkningen. Att läsa den ur portalens egna
    # länkar är säkrare än att härleda den ur ett gränsvärde.
    ref_map: dict[int, int] = {}

    if args.from_index:
        index_rows = read_jsonl(DIARIUM_INDEX)
        if not index_rows:
            log.error("%s saknas. Kör tools/diarium_enumerate.py först.",
                      DIARIUM_INDEX)
            return 2

        restricted = [r for r in index_rows if r.get("restricted")]
        linked = [r for r in index_rows if r.get("caseref") is not None]
        for r in linked:
            ref_map[r["caseref"]] = r.get("diaryref") or diaryref_for(r["caseref"])

        log.info("Uppräkning: %d ärenden, varav %d sekretessbelagda utan detaljsida",
                 len(index_rows), len(restricted))

        # Sekretessbelagda ärenden har ingen detaljsida. Det som är känt om dem
        # skrivs direkt, så att de inte blir en tyst lucka i täckningen.
        n_new_restricted = 0
        rw = JsonlWriter(DIARIUM_JSONL)
        for r in restricted:
            key = f"restricted:{r['diarienummer']}"
            if key in done:
                continue
            m = re.match(r"^(\d{4}):(\d+)$", r.get("diarienummer", "") or "")
            rec = {f: "" for f in FIELDS}
            rec.update({
                "caseref": key, "diaryref": "",
                "diarienummer": r.get("diarienummer", ""),
                "dno_si": f"SI {m.group(1)}:{int(m.group(2))}" if m else "",
                "year": m.group(1) if m else "",
                "case_no": m.group(2) if m else "",
                "arendemening": r.get("arendemening", ""),
                "reg_datum": r.get("reg_datum", ""),
                "restricted": "true",
                "url": "", "fetched_at": utcnow(),
                "_state": "ok", "_http": "", "_error": "", "_attempts": 0,
            })
            rw.write(rec)
            done[key] = rec
            n_new_restricted += 1
        rw.close()
        if n_new_restricted:
            log.info("  %d sekretessbelagda ärenden registrerade", n_new_restricted)

        todo = [c for c in sorted(ref_map)
                if c not in done
                or (args.retry_errors and done[c].get("_state") == "error")]
        span = f"{min(ref_map) if ref_map else 0}–{max(ref_map) if ref_map else 0}"
    else:
        hi = args.hi
        if not hi:
            hi = find_upper_bound(limiter, args)
            if not hi:
                log.error("Kunde inte fastställa övre gräns. Ange --to manuellt.")
                return 2
        todo = [c for c in range(args.lo, hi + 1)
                if c not in done
                or (args.retry_errors and done[c].get("_state") == "error")]
        span = f"{args.lo}–{hi}"

    log.info("=" * 72)
    log.info("Diarieskörd %s – logg: %s", span, logfile)
    log.info("  läge          : %s", "uppräknat index" if args.from_index
             else "svep över caseref-rymden")
    log.info("  redan prövade : %d", len(done))
    log.info("  att hämta     : %d", len(todo))
    log.info("  beräknad tid  : %.1f h", len(todo) * args.delay / 3600)
    log.info("=" * 72)

    if not todo:
        rows = sorted((r for r in done.values() if r.get("_state") == "ok"),
                      key=lambda r: (r.get("reg_datum") or "", str(r.get("caseref"))))
        log.info("Inget att hämta. Skriver om CSV: %d ärenden", write_csv(rows))
        return 0

    writer = JsonlWriter(DIARIUM_JSONL)
    q: queue.Queue = queue.Queue()
    for c in todo:
        q.put(c)

    c = {"done": 0, "ok": 0, "empty": 0, "err": 0}
    lock = threading.Lock()
    stop = threading.Event()
    total = len(todo)
    started = time.time()

    def worker():
        while not stop.is_set():
            try:
                caseref = q.get_nowait()
            except queue.Empty:
                return
            try:
                rec = fetch_case(caseref, limiter, args, ref_map.get(caseref))
            except Exception as exc:
                log.exception("caseref %d: oväntat fel", caseref)
                rec = {f: "" for f in FIELDS}
                rec.update({"caseref": caseref, "_state": "error",
                            "_error": f"{type(exc).__name__}: {exc}",
                            "fetched_at": utcnow()})
            writer.write(rec)
            done[caseref] = rec
            with lock:
                c["done"] += 1
                st = rec.get("_state")
                c["ok" if st == "ok" else "empty" if st == "empty" else "err"] += 1
                n = c["done"]
            if n % 500 == 0 or n == total:
                el = time.time() - started
                rate = n / el if el else 0
                log.info("DIARIUM %d/%d (%.1f%%) – %.1f/s – ETA %.1f h – "
                         "ärenden=%d tomma=%d fel=%d",
                         n, total, 100 * n / total, rate,
                         ((total - n) / rate / 3600) if rate else 0,
                         c["ok"], c["empty"], c["err"])
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

    rows = sorted((r for r in done.values() if r.get("_state") == "ok"),
                  key=lambda r: (r.get("reg_datum") or "", str(r.get("caseref"))))
    n_csv = write_csv(rows)

    log.info("=" * 72)
    log.info("Diarieskörd klar på %.1f h", (time.time() - started) / 3600)
    log.info("  ärenden hämtade : %d", c["ok"])
    log.info("  tomma caseref   : %d", c["empty"])
    log.info("  fel             : %d", c["err"])
    log.info("  totalt i CSV    : %d", n_csv)
    log.info("  %s", DIARIUM_CSV)
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
