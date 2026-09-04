#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catalog_crawl.py — bygger en fullständig dokumentkatalog från Skolinspektionens
publika SIRIS-API.

Detta ersätter brute force-skanningen av docID-rymden som primär källa. API:et
är en auktoritativ, uppräkningsbar katalog som dessutom ger dokumenttitel,
år, granskningsområde och koppling till kommun/huvudman/skolenhet.

Uppräknade noder:

    /api/siris/counties/                              291 kommuner
    /api/siris/counties/{kod}/documents               dokument på kommunnivå
    /api/siris/counties/{kod}/schools/current         skolenheter i kommunen
    /api/siris/schools/{kod}/documents                dokument per skolenhet
    /api/siris/companiesandorganisations/             huvudmän (aktuella + gamla)
    /api/siris/companiesandorganisations/{kod}/documents
    /api/siris/companiesandorganisations/{kod}/schools/current

Output (under SIRIS_ROOT):

    catalog/counties.json, organisations.json, schools.json
    catalog/catalog.jsonl   en rad per (docID, upptäcktsnod) — bevarar all kontext
    catalog/nodes.jsonl     resume-state: vilka noder som är färdiga

Körningen är återupptagbar: avbryt när som helst och kör om samma kommando.

Användning:
    python tools/catalog_crawl.py
    python tools/catalog_crawl.py --workers 4 --delay 0.3
    python tools/catalog_crawl.py --stage counties     # bara kommunnivån
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from siris_common import (  # noqa: E402
    CATALOG_DIR, CATALOG_JSONL, SI_API,
    JsonlWriter, RateLimiter, classify_document, clean_title, docid_from_url,
    ensure_dirs, http_get_retry, load_api_json, read_jsonl, setup_logging,
    size_hint, utcnow,
)

NODES_JSONL = os.path.join(CATALOG_DIR, "nodes.jsonl")
COUNTIES_JSON = os.path.join(CATALOG_DIR, "counties.json")
ORGS_JSON = os.path.join(CATALOG_DIR, "organisations.json")
SCHOOLS_JSON = os.path.join(CATALOG_DIR, "schools.json")

log = None  # sätts i main


# ──────────────────────────────────────────────────────────────────────────
#  Hjälp
# ──────────────────────────────────────────────────────────────────────────


def flatten_groups(data) -> list[dict]:
    """
    API:et svarar med [{"aktuella": [...]}, {"gamla": [...]}].
    Plattar ut till en lista och behåller gruppnamnet i 'grupp'.
    """
    out: list[dict] = []
    if isinstance(data, dict):
        data = [data]
    for group in data or []:
        if not isinstance(group, dict):
            continue
        for gname, items in group.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("kod"):
                    out.append({
                        "kod": str(it["kod"]).strip(),
                        "namn": str(it.get("namn", "")).strip(),
                        "grupp": gname,
                    })
    return out


def fetch_json(url: str, limiter, tries: int, timeout: int):
    status, body, _ct, err, attempts = http_get_retry(
        url, limiter, log, tries=tries, timeout=timeout, accept="application/json"
    )
    if status != 200 or not body:
        return None, status, err or f"status {status}", attempts
    try:
        return load_api_json(body), status, "", attempts
    except Exception as exc:
        return None, status, f"kunde inte tolka JSON: {exc}", attempts


def parse_documents(payload) -> list[dict]:
    """Normaliserar ett /documents-svar till en lista med dokumentposter."""
    if not isinstance(payload, dict):
        return []
    docs = []
    for item in payload.get("svar") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        docid = docid_from_url(url)
        if docid is None:
            continue
        raw_title = item.get("text") or ""
        # Nyckeln för årtal är 'år' men kan komma teckenkodad på olika sätt.
        year = ""
        for k, v in item.items():
            if k.lower().strip() in ("år", "ar", "�r") and v:
                year = str(v).strip()
                break
        docs.append({
            "docid": docid,
            "title": clean_title(raw_title),
            "raw_title": raw_title,
            "size_hint": size_hint(raw_title),
            "year": year,
            "gransknomr": str(item.get("gransknomr", "") or "").strip(),
            "source_url": url,
            "document_type": classify_document(raw_title),
        })
    return docs


# ──────────────────────────────────────────────────────────────────────────
#  Nodhantering
# ──────────────────────────────────────────────────────────────────────────


class Crawler:
    def __init__(self, args):
        self.args = args
        self.limiter = RateLimiter(args.delay)
        self.done_nodes: dict[str, dict] = read_jsonl(NODES_JSONL, key="node")
        self.cat_writer = JsonlWriter(CATALOG_JSONL)
        self.node_writer = JsonlWriter(NODES_JSONL)
        self.lock = threading.Lock()
        self.stats = {"nodes": 0, "docs": 0, "errors": 0, "skipped": 0}
        self.seen_docids: set[int] = set()
        for rec in read_jsonl(CATALOG_JSONL):
            if rec.get("docid"):
                self.seen_docids.add(rec["docid"])
        if self.done_nodes:
            log.info("Återupptar: %d noder redan hämtade, %d docID kända",
                     len(self.done_nodes), len(self.seen_docids))

    def node_key(self, kind: str, code: str) -> str:
        return f"{kind}:{code}"

    def crawl_node(self, kind: str, code: str, name: str, extra: dict) -> None:
        """Hämtar dokumentlistan för en nod och skriver katalograder."""
        key = self.node_key(kind, code)
        if key in self.done_nodes and not self.args.force:
            with self.lock:
                self.stats["skipped"] += 1
            return

        url = f"{SI_API}/{kind}/{code}/documents"
        payload, status, err, attempts = fetch_json(
            url, self.limiter, self.args.max_retries, self.args.timeout
        )

        if payload is None:
            with self.lock:
                self.stats["errors"] += 1
            log.error("%s %s (%s): %s", kind, code, name, err)
            self.node_writer.write({
                "node": key, "kind": kind, "code": code, "name": name,
                "status": "error", "http_status": status, "error": err,
                "documents": 0, "attempts": attempts, "fetched_at": utcnow(),
            })
            return

        docs = parse_documents(payload)
        claimed = payload.get("antal_svar")

        for d in docs:
            row = dict(d)
            row.update({
                "found_via_kind": kind,
                "found_via_code": code,
                "found_via_name": name,
                "found_at": utcnow(),
            })
            row.update(extra)
            self.cat_writer.write(row)

        with self.lock:
            self.stats["nodes"] += 1
            self.stats["docs"] += len(docs)
            new = {d["docid"] for d in docs} - self.seen_docids
            self.seen_docids |= new
            total_unique = len(self.seen_docids)

        self.node_writer.write({
            "node": key, "kind": kind, "code": code, "name": name,
            "status": "ok", "http_status": status,
            "documents": len(docs), "antal_svar": claimed,
            "attempts": attempts, "fetched_at": utcnow(),
        })

        if docs:
            log.debug("%s %s (%s): %d dokument", kind, code, name, len(docs))

        n = self.stats["nodes"]
        if n % 100 == 0:
            log.info("FRAMSTEG %d noder klara – %d katalograder – %d unika docID – "
                     "%d fel", n, self.stats["docs"], total_unique, self.stats["errors"])

    def close(self):
        self.cat_writer.close()
        self.node_writer.close()


def run_parallel(crawler: Crawler, tasks: list[tuple], label: str) -> None:
    """Kör en lista noduppgifter parallellt."""
    if not tasks:
        return
    log.info("── %s: %d noder ──", label, len(tasks))
    q: queue.Queue = queue.Queue()
    for t in tasks:
        q.put(t)
    started = time.time()
    total = len(tasks)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                kind, code, name, extra = q.get_nowait()
            except queue.Empty:
                return
            try:
                crawler.crawl_node(kind, code, name, extra)
            except Exception:
                log.exception("oväntat fel i nod %s/%s", kind, code)
                with crawler.lock:
                    crawler.stats["errors"] += 1
            q.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, crawler.args.workers))]
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log.warning("Avbrott – körningen kan återupptas med samma kommando.")
        stop.set()
        for t in threads:
            t.join(timeout=10)
        raise

    log.info("── %s klar på %.1f min ──", label, (time.time() - started) / 60)


# ──────────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    global log
    ap = argparse.ArgumentParser(description="Bygger dokumentkatalog från Skolinspektionens SIRIS-API.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.35,
                    help="minsta sekunder mellan anrop, globalt (standard 0.35)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--max-retries", type=int, default=4)
    ap.add_argument("--force", action="store_true",
                    help="hämta om noder som redan är klara")
    ap.add_argument("--stage", choices=["all", "counties", "orgs", "schools"],
                    default="all")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    ensure_dirs()
    log, logfile = setup_logging("catalog", args.verbose)

    log.info("=" * 72)
    log.info("Katalogcrawl startad – logg: %s", logfile)
    log.info("API: %s", SI_API)
    log.info("Parametrar: workers=%d delay=%.2fs", args.workers, args.delay)
    log.info("=" * 72)

    limiter = RateLimiter(args.delay)
    started = time.time()

    # ── 1. Rotlistor ──
    log.info("Hämtar rotlistor ...")
    counties_raw, st, err, _ = fetch_json(f"{SI_API}/counties/", limiter,
                                          args.max_retries, args.timeout)
    if counties_raw is None:
        log.error("Kunde inte hämta kommunlistan: %s", err)
        return 2
    counties = flatten_groups(counties_raw) or [
        {"kod": str(c.get("kod")), "namn": c.get("namn", ""), "grupp": "aktuella"}
        for c in counties_raw if isinstance(c, dict) and c.get("kod")
    ]
    with open(COUNTIES_JSON, "w", encoding="utf-8") as f:
        json.dump(counties, f, ensure_ascii=False, indent=1)
    log.info("  %d kommuner", len(counties))

    orgs_raw, st, err, _ = fetch_json(f"{SI_API}/companiesandorganisations/",
                                      limiter, args.max_retries, args.timeout)
    if orgs_raw is None:
        log.error("Kunde inte hämta huvudmannalistan: %s", err)
        return 2
    orgs = flatten_groups(orgs_raw)
    with open(ORGS_JSON, "w", encoding="utf-8") as f:
        json.dump(orgs, f, ensure_ascii=False, indent=1)
    n_old = sum(1 for o in orgs if o["grupp"] != "aktuella")
    log.info("  %d huvudmän (%d aktuella, %d historiska)",
             len(orgs), len(orgs) - n_old, n_old)

    crawler = Crawler(args)

    try:
        # ── 2. Dokument per kommun ──
        if args.stage in ("all", "counties"):
            run_parallel(crawler, [
                ("counties", c["kod"], c["namn"],
                 {"county_code": c["kod"], "county_name": c["namn"]})
                for c in counties
            ], "Dokument per kommun")

        # ── 3. Dokument per huvudman ──
        if args.stage in ("all", "orgs"):
            run_parallel(crawler, [
                ("companiesandorganisations", o["kod"], o["namn"],
                 {"org_code": o["kod"], "org_name": o["namn"],
                  "org_group": o["grupp"]})
                for o in orgs
            ], "Dokument per huvudman")

        # ── 4. Skolenheter: samla in från kommuner och huvudmän ──
        if args.stage in ("all", "schools"):
            schools: dict[str, dict] = {}
            if os.path.exists(SCHOOLS_JSON) and not args.force:
                for s in json.load(open(SCHOOLS_JSON, encoding="utf-8")):
                    schools[s["kod"]] = s
                log.info("Läste %d kända skolenheter från %s",
                         len(schools), SCHOOLS_JSON)

            if not schools:
                log.info("── Samlar in skolenheter från kommuner och huvudmän ──")
                targets = (
                    [("counties", c["kod"], c["namn"], "county") for c in counties]
                    + [("companiesandorganisations", o["kod"], o["namn"], "org")
                       for o in orgs]
                )
                slock = threading.Lock()
                sq: queue.Queue = queue.Queue()
                for t in targets:
                    sq.put(t)
                done = [0]

                def sworker():
                    while True:
                        try:
                            kind, code, name, parent = sq.get_nowait()
                        except queue.Empty:
                            return
                        payload, _st, serr, _a = fetch_json(
                            f"{SI_API}/{kind}/{code}/schools/current",
                            crawler.limiter, args.max_retries, args.timeout)
                        if payload is not None:
                            for s in flatten_groups(payload):
                                with slock:
                                    prev = schools.get(s["kod"])
                                    if prev is None:
                                        s = dict(s)
                                        s["parent_kind"] = parent
                                        s["parent_code"] = code
                                        s["parent_name"] = name
                                        schools[s["kod"]] = s
                        else:
                            log.warning("skolor för %s %s: %s", kind, code, serr)
                        with slock:
                            done[0] += 1
                            n = done[0]
                        if n % 200 == 0:
                            log.info("  %d/%d noder – %d unika skolenheter",
                                     n, len(targets), len(schools))
                        sq.task_done()

                sthreads = [threading.Thread(target=sworker, daemon=True)
                            for _ in range(max(1, args.workers))]
                for t in sthreads:
                    t.start()
                while any(t.is_alive() for t in sthreads):
                    for t in sthreads:
                        t.join(timeout=0.5)

                with open(SCHOOLS_JSON, "w", encoding="utf-8") as f:
                    json.dump(sorted(schools.values(), key=lambda x: x["kod"]),
                              f, ensure_ascii=False, indent=1)
                log.info("  %d unika skolenheter sparade i %s",
                         len(schools), SCHOOLS_JSON)

            # ── 5. Dokument per skolenhet ──
            run_parallel(crawler, [
                ("schools", s["kod"], s["namn"],
                 {"school_code": s["kod"], "school_name": s["namn"],
                  "parent_kind": s.get("parent_kind", ""),
                  "parent_code": s.get("parent_code", ""),
                  "parent_name": s.get("parent_name", "")})
                for s in sorted(schools.values(), key=lambda x: x["kod"])
            ], "Dokument per skolenhet")

    finally:
        crawler.close()

    elapsed = time.time() - started
    log.info("=" * 72)
    log.info("Katalogcrawl klar på %.1f min", elapsed / 60)
    log.info("  noder hämtade:     %d", crawler.stats["nodes"])
    log.info("  noder överhoppade: %d (redan klara)", crawler.stats["skipped"])
    log.info("  katalograder:      %d", crawler.stats["docs"])
    log.info("  UNIKA DOKUMENT:    %d", len(crawler.seen_docids))
    log.info("  fel:               %d", crawler.stats["errors"])
    log.info("Katalog: %s", CATALOG_JSONL)
    log.info("Nästa steg: python tools/build_index.py")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
