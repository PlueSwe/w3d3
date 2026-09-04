#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diarium_enumerate.py — räknar upp vilka ärenden som finns i diariet.

Steg 1 av två. Verktyget går igenom diariets hela tidsspann i datumfönster och
läser ut vilka ärenden som existerar, med portalens interna referenser.
`diarium_crawl.py` hämtar sedan detaljsidan för var och en.

Varför uppräkning i stället för att svepa caseref-rymden:

  * **Korrekthet.** Detaljsidans URL kräver både `diaryref` och `caseref`.
    Sökresultatets länkar innehåller båda. Att gissa `diaryref` från ett
    gränsvärde gör att ärenden nära seriegränsen tyst ser tomma ut.
  * **Fullständighet kan bevisas.** Portalen svarar med "Antal träffar: N" per
    fönster. Summan är det sanna antalet ärenden — inte en gissning utifrån
    hur många ID som råkade svara.
  * **Mindre arbete.** Caseref-rymden är gles, särskilt 2008–2010. Ett svep
    prövar tiotusentals ID som aldrig funnits.

Output:

    catalog/diarium_index.jsonl   en rad per funnet ärende
    catalog/diarium_windows.jsonl resume-state: färdiga datumfönster

Användning:
    python tools/diarium_enumerate.py
    python tools/diarium_enumerate.py --from 2008-01-01 --to 2026-12-31
    python tools/diarium_enumerate.py --window 7      # dagar per sökning
"""

from __future__ import annotations

import argparse
import datetime
import html as html_mod
import http.cookiejar
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_DIR, USER_AGENT, JsonlWriter, RateLimiter, ensure_dirs,
    read_jsonl, setup_logging, utcnow,
)

PORTAL = "https://externsearchport.skolinspektionen.se"
SEARCH_URL = PORTAL + "/search.aspx?view=ac"

INDEX_JSONL = os.path.join(CATALOG_DIR, "diarium_index.jsonl")
WINDOWS_JSONL = os.path.join(CATALOG_DIR, "diarium_windows.jsonl")

# Diariet startar när Skolinspektionen bildas.
DEFAULT_FROM = "2008-01-01"

# Portalen slutar lista efter 1000 träffar. Fönstret halveras automatiskt om
# ett svar närmar sig taket, så gränsen behöver inte gissas rätt från början.
RESULT_CAP = 1000

log = None


class Session:
    """ASP.NET-session med cookies och ViewState."""

    def __init__(self, limiter: RateLimiter, timeout: int):
        self.limiter = limiter
        self.timeout = timeout
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self.state: dict[str, str] = {}
        self.refresh()

    def refresh(self) -> None:
        self.limiter.wait()
        with self.opener.open(SEARCH_URL, timeout=self.timeout) as r:
            html = r.read().decode("utf-8", "replace")
        self.state = self._extract(html)
        if not self.state.get("__VIEWSTATE"):
            raise RuntimeError("kunde inte läsa ViewState från sökformuläret")

    @staticmethod
    def _extract(html: str) -> dict[str, str]:
        def f(name):
            m = re.search(r'id="' + name + r'"\s+value="([^"]*)"', html)
            return m.group(1) if m else ""
        return {
            "__VIEWSTATE": f("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": f("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": f("__EVENTVALIDATION"),
            "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
        }

    def search(self, frm: str, to: str) -> str:
        form = dict(self.state)
        form.update({
            "ddlDiaries": "",          # alla serier
            "txtSubject": "", "txtDno": "",
            "ddlCase_Type": "", "ddlDepartment": "", "ddlStatus": "",
            "txtFromDateReg": frm, "txtToDateReg": to,
            "txtFromDateEnd": "", "txtToDateEnd": "",
            "btnAdvancedSearch": "Sök",
        })
        data = urllib.parse.urlencode(form, encoding="utf-8").encode()
        req = urllib.request.Request(SEARCH_URL, data=data, headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": SEARCH_URL,
        })
        self.limiter.wait()
        with self.opener.open(req, timeout=self.timeout) as r:
            body = r.read().decode("utf-8", "replace")
        # Svaret bär ny ViewState; utan den fallerar nästa POST.
        new = self._extract(body)
        if new.get("__VIEWSTATE"):
            self.state = new
        return body


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub("<[^>]+>", "", s))).strip()


def parse_results(body: str) -> tuple[list[dict], int]:
    """Returnerar (ärenden, antal_träffar_enligt_portalen)."""
    text = html_mod.unescape(body)
    m = re.search(r"Antal träffar:\s*(\d+)", text)
    claimed = int(m.group(1)) if m else -1

    # Träfflistan sträcker sig till dokumentets slut. Att klippa ut tabellen
    # med en icke-girig regex fungerar inte — den stannar vid första </table>,
    # och sidan har nästlade tabeller. Allt efter träfflistan är sidfot utan
    # ärenderader, så det är ofarligt att ta med.
    start = body.find('class="searchListTable"')
    if start == -1:
        return [], claimed
    segment = body[start:]

    out: list[dict] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", segment, re.S | re.I):
        cells = [clean(c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        cells = [c for c in cells if c]
        # Serie 2 skriver diarienumret som '2010:78', serie 6 som 'SI 2024:300'.
        dno = next((c for c in cells
                    if re.match(r"^(?:SI\s*)?\d{4}:\d+$", c, re.I)), "")
        if not dno:
            continue                      # rubrikrad
        dno = re.sub(r"^SI\s*", "", dno, flags=re.I)
        date = next((c for c in cells if re.match(r"^\d{4}-\d\d-\d\d$", c)), "")
        subject = ""
        for c in cells:
            if re.match(r"^(?:SI\s*)?\d{4}:\d+$", c, re.I) or c == date:
                continue
            if len(c) > len(subject):
                subject = c

        # Serie 2 markerar sekretess i ärendemeningen, serie 6 med en
        # hänglåsikon i första kolumnen.
        is_restricted = bool(re.search(r"sekretessbelagt|icon-lock", row, re.I))

        link = re.search(r'case\.aspx\?diaryref=(\d+)&(?:amp;)?caseref=(\d+)',
                         row, re.I)
        if link:
            out.append({
                "diaryref": int(link.group(1)),
                "caseref": int(link.group(2)),
                "diarienummer": dno,
                "reg_datum": date,
                "arendemening": subject,
                "restricted": is_restricted,
                "found_at": utcnow(),
            })
        else:
            # Sekretessbelagda ärenden listas utan detaljlänk. Ärendet finns
            # och registreras — att det är sekretessbelagt är i sig uppgift.
            # Utan detta blir de en tyst lucka i täckningen.
            out.append({
                "diaryref": None,
                "caseref": None,
                "diarienummer": dno,
                "reg_datum": date,
                "arendemening": subject,
                "restricted": True,
                "found_at": utcnow(),
            })
    return out, claimed


def daterange(start: datetime.date, end: datetime.date, days: int):
    cur = start
    while cur <= end:
        stop = min(cur + datetime.timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + datetime.timedelta(days=1)


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(
        description="Räknar upp vilka ärenden som finns i diariet.")
    ap.add_argument("--from", dest="frm", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="to", default="")
    ap.add_argument("--window", type=int, default=10, help="dagar per sökning")
    ap.add_argument("--delay", type=float, default=0.6,
                    help="sekunder mellan sökningar (dessa är tunga för portalen)")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--force", action="store_true", help="gör om färdiga fönster")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("diarium_enum", args.verbose)

    start = datetime.date.fromisoformat(args.frm)
    end = (datetime.date.fromisoformat(args.to) if args.to
           else datetime.date.today())

    done_windows = {r["window"]: r for r in read_jsonl(WINDOWS_JSONL)
                    if r.get("window")}
    known: dict[str, dict] = {}
    for r in read_jsonl(INDEX_JSONL):
        if r.get("diarienummer"):
            known[r["diarienummer"]] = r

    windows = list(daterange(start, end, args.window))
    log.info("=" * 72)
    log.info("Uppräkning %s .. %s – logg: %s", start, end, logfile)
    log.info("  fönster om %d dagar: %d st", args.window, len(windows))
    log.info("  redan klara: %d", len(done_windows))
    log.info("  kända ärenden: %d", len(known))
    log.info("=" * 72)

    limiter = RateLimiter(args.delay)
    session = Session(limiter, args.timeout)
    idx_w = JsonlWriter(INDEX_JSONL)
    win_w = JsonlWriter(WINDOWS_JSONL)

    started = time.time()
    n_win = n_new = 0
    total_claimed = 0

    def do_window(a: datetime.date, b: datetime.date, depth: int = 0) -> int:
        """Söker ett fönster. Halverar det om portalens 1000-tak nås."""
        nonlocal n_new, total_claimed
        key = f"{a}..{b}"
        if key in done_windows and not args.force:
            return done_windows[key].get("found", 0)

        for attempt in (1, 2, 3):
            try:
                body = session.search(a.isoformat(), b.isoformat())
                break
            except Exception as exc:
                log.warning("  %s: försök %d misslyckades (%s)", key, attempt, exc)
                if attempt == 3:
                    win_w.write({"window": key, "status": "error",
                                 "error": str(exc)[:200], "found": 0,
                                 "fetched_at": utcnow()})
                    return 0
                time.sleep(5 * attempt)
                try:
                    session.refresh()
                except Exception:
                    pass

        rows, claimed = parse_results(body)

        # Taket nått: dela fönstret hellre än att tappa ärenden.
        if (claimed >= RESULT_CAP or len(rows) >= RESULT_CAP) and a < b and depth < 8:
            mid = a + (b - a) // 2
            log.info("  %s: %d träffar – delar fönstret", key, claimed)
            return do_window(a, mid, depth + 1) + \
                do_window(mid + datetime.timedelta(days=1), b, depth + 1)

        added = 0
        for r in rows:
            if r["diarienummer"] in known:
                continue
            known[r["diarienummer"]] = r
            idx_w.write(r)
            added += 1
        n_new += added
        total_claimed += max(0, claimed)

        win_w.write({"window": key, "status": "ok", "claimed": claimed,
                     "rows": len(rows), "found": added, "fetched_at": utcnow()})

        if claimed >= 0 and len(rows) != claimed:
            log.warning("  %s: portalen anger %d träffar men listan har %d rader",
                        key, claimed, len(rows))
        return added

    try:
        for a, b in windows:
            do_window(a, b)
            n_win += 1
            if n_win % 25 == 0 or n_win == len(windows):
                el = time.time() - started
                rate = n_win / el if el else 0
                log.info("UPPRÄKNING %d/%d fönster (%.1f%%) – ETA %.1f h – "
                         "%d ärenden kända",
                         n_win, len(windows), 100 * n_win / len(windows),
                         ((len(windows) - n_win) / rate / 3600) if rate else 0,
                         len(known))
    except KeyboardInterrupt:
        log.warning("Avbrott – kör om samma kommando för att återuppta.")
    finally:
        idx_w.close()
        win_w.close()

    years: dict[str, int] = {}
    for r in known.values():
        y = (r.get("reg_datum") or "")[:4]
        years[y] = years.get(y, 0) + 1

    log.info("=" * 72)
    log.info("Uppräkning klar på %.1f h", (time.time() - started) / 3600)
    log.info("  fönster:            %d", n_win)
    log.info("  nya ärenden:        %d", n_new)
    log.info("  TOTALT I DIARIET:   %d", len(known))
    refs = [r["caseref"] for r in known.values() if r.get("caseref")]
    n_restricted = sum(1 for r in known.values() if r.get("restricted"))
    if refs:
        log.info("  caseref-spann:      %d–%d", min(refs), max(refs))
    log.info("  sekretessbelagda:   %d (listade, saknar detaljsida)", n_restricted)
    log.info("  per år:")
    for y in sorted(years):
        log.info("    %s  %6d", y or "okänt", years[y])
    log.info("Nästa steg: python tools/diarium_crawl.py --from-index")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
