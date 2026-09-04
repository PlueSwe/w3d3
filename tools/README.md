# tools/ — migrerings- och arkiveringsverktyg

Fristående verktyg som **läser** dagens datakällor men aldrig ändrar dem.
Webbplatsen (`index.html`, PHP-skripten, `data.json`) fungerar oförändrat
medan verktygen körs.

Krav: Python 3.9+. Inga externa beroenden — enbart standardbiblioteket.
`pdftotext` (poppler/xpdf) används om den finns i PATH, annars används en
inbyggd reservtolk.

---

## Var saker ligger

Sökvägar styrs av environment variables, så arkivet kan flyttas utan
kodändring — samma princip som senare gäller databas och objektlagring.

| Variabel | Default | Betydelse |
|---|---|---|
| `SIRIS_ROOT` | `D:\siris` | arkivets rot |
| `SIRIS_SOURCE` | repot | var `data.json`, `beslut.json`, `skolor.json` ligger |

```
D:\siris\
  catalog\
    counties.json          291 kommuner
    organisations.json     1 344 huvudmän (aktuella + historiska)
    schools.json           samtliga skolenheter
    catalog.jsonl          en rad per (dokument, upptäcktsnod)
    nodes.jsonl            resume-state för katalogcrawlen
  pdf\
    siris-<docID>.pdf      SAMTLIGA dokument, platt, ett ställe
  index\
    downloads.jsonl        nedladdningsstatus per dokument
    extracted.jsonl        textextraktion + funna diarienummer
    documents.csv / .jsonl en rad per dokument med metadata + SHA-256
    case_documents.csv     ← KOPPLINGSTABELLEN ärende ↔ dokument
    cases.csv              ärenden med dokumenträkning
    text\<doc-id>.txt      extraherad text (med --keep-text)
  reports\
    archive-verification.md / .csv
  logs\
```

### Varför platt PDF-lager?

`docID` är dokumentets identitet i källan och är globalt unikt. En platt
katalog ger tre saker som en trädstruktur inte ger:

- samma dokument kan inte hamna på två ställen
- ett dokument som gäller **flera** ärenden lagras en gång, inte en gång per ärende
- sökvägen mappas rakt av till en S3-nyckel i Etapp 5

Kopplingen mellan ärende och fil ligger i `index/`, inte i katalognamnen.

### Hur man kopplar ärende till rätt PDF

`index/case_documents.csv` är kopplingstabellen. En rad per (ärende, dokument):

| diarienummer | document_id | filename | document_type | link_type | link_confidence |
|---|---|---|---|---|---|
| SI 2024:21120 | siris-648018 | siris-648018.pdf | tillsynsbeslut | own_dnr | high |
| SI 2024:2120 | siris-648018 | siris-648018.pdf | tillsynsbeslut | mentioned | reference |

Filen finns på `D:\siris\pdf\<filename>`. Ett ärende kan förekomma på flera
rader (flera dokument), och ett dokument kan förekomma på flera rader.

**Läs alltid `link_type` innan du använder en rad.**

| `link_type` | Betydelse |
|---|---|
| `own_dnr` | Dokumentet **tillhör** ärendet. Diarienumret står i sidhuvudet, efter `Dnr`. Detta är kopplingen du vill ha. |
| `mentioned` | Dokumentet **hänvisar till** ärendet i löptext, t.ex. "vi fattar idag beslut i ärendet med dnr SI 2019:7841". Dokumentet tillhör inte det ärendet. |

Distinktionen är inte kosmetisk. Ett beslut som skriver ut ett annat ärendes
diarienummer skulle utan den kopplas till fel PDF. Exempel: `siris-618125`
(Al-Azhar) och `siris-618126` (Edinit) nämner varandras diarienummer — utan
`link_type` blir båda besluten kopplade till båda ärendena.

För en enkel "vilken PDF hör till det här ärendet"-uppslagning:

```
filtrera case_documents.csv på link_type = own_dnr
```

`link_confidence`:

| Värde | Betydelse |
|---|---|
| `high` | eget diarienummer **och** ärendet finns i diariet |
| `unmatched_case` | eget diarienummer, men ärendet saknas i `data.json` (vanligt för beslut som föregår diariets start 2019-01-01) |
| `reference` | hänvisning till ett ärende som finns i diariet |
| `reference_unmatched` | hänvisning till ett ärende som inte finns i diariet |

`index/cases.csv` ger motsatt vy: en rad per ärende med `document_count` och
en semikolonseparerad `document_ids`.

---

## Kedjan

```bash
python tools/catalog_crawl.py     # 1. bygg katalogen  (~40 min)
python tools/fetch_pdfs.py        # 2. ladda ner allt  (~3 h)
python tools/build_index.py       # 3. koppla ärende ↔ dokument  (~30 min)
python tools/verify_archive.py    # 4. verifiera
```

Varje steg är återupptagbart: avbryt med Ctrl+C och kör om samma kommando.

---

## `catalog_crawl.py` — bygg dokumentkatalogen

Räknar upp Skolinspektionens publika SIRIS-API. Detta ersätter brute
force-skanningen som primär källa.

```
/api/siris/counties/                              291 kommuner
/api/siris/counties/{kod}/documents               dokument på kommunnivå
/api/siris/counties/{kod}/schools/current         skolenheter i kommunen
/api/siris/schools/{kod}/documents                dokument per skolenhet
/api/siris/companiesandorganisations/             huvudmän (aktuella + gamla)
/api/siris/companiesandorganisations/{kod}/documents
/api/siris/companiesandorganisations/{kod}/schools/current
```

API:et ger dokumenttitel, år, granskningsområde och koppling till
kommun/huvudman/skolenhet — metadata som brute force inte kan ge.

```bash
python tools/catalog_crawl.py
python tools/catalog_crawl.py --stage counties    # bara kommunnivån
python tools/catalog_crawl.py --workers 4 --delay 0.3
```

---

## `fetch_pdfs.py` — ladda ner till det platta lagret

```bash
python tools/fetch_pdfs.py
python tools/fetch_pdfs.py --limit 50                    # provkörning
python tools/fetch_pdfs.py --retry-failed                # gör om misslyckade
python tools/fetch_pdfs.py --import-from archive --import-only
```

`--import-from` tar in PDF:er som redan hämtats i en tidigare körning
(filnamn `siris-<id>.pdf`) i stället för att ladda ner dem igen. Originalen
lämnas orörda.

**Garantier:**

- skriver aldrig över befintlig fil utan kontroll — checksummerar och behåller
- atomisk skrivning via `.part` + `os.replace()`
- retry med exponentiell backoff (2→4→8 s, tak 60 s) på nätverksfel, 429, 5xx;
  4xx görs inte om
- global takthållning över alla trådar (`--delay`, standard 0,4 s ≈ 2,5 anrop/s)
- **prioritetsordning**: tillsyns- och granskningsbeslut hämtas före
  Skolenkäten, så att en avbruten körning ändå har säkrat det Beslutstjänsten
  bygger på

### Filtypen avgörs på innehållet, inte på headern

Allt i SIRIS är inte PDF. Beslut från 2003–2010 ligger ofta som Word, och
`content-type` kan vara `application/octet-stream`. Filtypen bestäms därför på
magiska bytes:

| Signatur | Typ | Filändelse | Räknas som handling |
|---|---|---|---|
| `%PDF-` | PDF | `.pdf` | ja |
| `D0CF11E0…` (OLE2) | Word 97–2003 | `.doc` | ja |
| `PK\x03\x04` + `word/` | Word OOXML | `.docx` | ja |
| `{\rtf` | RTF | `.rtf` | ja |
| `<html` / `<!doctype` | HTML-felsida | `.html` | **nej** |
| annat | okänt | `.bin` | **nej** |

Ett Word-beslut är en fullvärdig handling, inte en misslyckad hämtning. Bara
det sista blocket får status `ok_not_pdf` — där svarade servern 200 utan att
leverera en handling.

---

## `build_index.py` — koppla ärende till rätt PDF

Textextraherar varje PDF och plockar ut alla diarienummer på formen
`SI YYYY:NNNN`. **Kopplingen härleds ur dokumentets eget innehåll** — det är
den enda källan som faktiskt bevisar sambandet. Katalogen innehåller inga
diarienummer, och en gissning utifrån skolnamn eller datum vore inte spårbar.

```bash
python tools/build_index.py
python tools/build_index.py --keep-text     # spara texten (underlag för Etapp 9/RAG)
python tools/build_index.py --pages 0       # extrahera hela dokumentet
python tools/build_index.py --skip-extract  # bygg om index utan att extrahera
```

Kopplingen är **många-till-många**: ett samlingsbeslut kan räkna upp flera
ärenden, och ett ärende kan ha beslut + uppföljningsbeslut.

Verktyget skiljer på dokumentets **eget** diarienummer och diarienummer det
bara **hänvisar till**. Diskrimineringen bygger på två signaler som följer av
hur besluten är satta:

1. **Etikett** — numret föregås av `Dnr` / `Diarienummer`.
2. **Position** — det egna numret står i sidhuvudet på sida 1, tillsammans med
   beslutsdatum och sidnumrering. Hänvisningar står i brödtext, längre ned.

Det egna numret är den första etiketterade förekomsten inom sidhuvudsfönstret
(1 500 tecken). Se `link_type` ovan.

### Två diarienummerserier

`SI ÅÅÅÅ:NNNN` är ett relativt nytt format. Äldre dokument använder en
serieprefixad form, ibland med tankstreck i stället för bindestreck:

| Period | Format | Exempel |
|---|---|---|
| 2003–2008 | `NN-ÅÅÅÅ:NNNN` | `53-2006:2989` (Skolverkets utbildningsinspektion) |
| 2009–2015 | `NNN-ÅÅÅÅ:NNNN` | `401-2014:2380` |
| ca 2018– | `SI ÅÅÅÅ:NNNN` | `SI 2024:21120` |

Bara `SI`-serien kan kopplas mot `data.json`, som börjar 2019-01-01. Äldre
nummer fångas ändå och sparas i kolumnerna `legacy_diarienummer` och
`legacy_diarienummer_list` i `documents.csv`, så att kopplingen kan göras i
efterhand om ett äldre diarium blir tillgängligt. Ett dokument utan
`primary_diarienummer` men med `legacy_diarienummer` saknar alltså inte
diarienummer — det ligger utanför diariets tidsspann.

### Text ur Word

`office_text.py` extraherar text ur `.docx` (ZIP + `word/document.xml`, med
sidhuvudena först eftersom diarienumret står där) och `.doc` (`antiword` om
den finns i PATH, annars strängextraktion ur OLE2-containern). Ambitionen är
att hitta diarienumret — inte fulltextindexering.

---

## `verify_archive.py` — bevisa att arkivet håller

Utgår från att **HTTP 200 inte är bevis**. Varje fil kontrolleras mot disk och
innehåll:

1. filen finns och är större än 0 byte
2. SHA-256 räknas om från disk och jämförs mot indexet
3. filen inleds med `%PDF-`
4. filen avslutas med `%%EOF` (upptäcker trunkering)
5. PDF:en har `/Root` och sidobjekt
6. misstänkt små PDF:er (< 1 kB) flaggas
7. HTML-svar som utger sig för att vara PDF identifieras explicit

```bash
python tools/verify_archive.py
python tools/verify_archive.py --no-rehash    # snabbare, svagare
```

Exitkod 0 om arkivet är komplett, 1 annars — användbart i CI.

---

## `sweep_docids.py` — fullständighetskontroll (valfri, långkörning)

Katalogen har en känd blind fläck: skolenheter som lagts ner listas inte under
`schools/current`. Svepningen prövar docID-rymden direkt och hittar dokument
som ingen katalognod pekar på.

```bash
python tools/sweep_docids.py --probe            # kartlägg rymdens gränser
python tools/sweep_docids.py --around-catalog   # svep katalogens intervall + marginal
python tools/sweep_docids.py --from 600000 --to 670000
python tools/sweep_docids.py --status           # hur långt har vi kommit
```

Till skillnad från den ursprungliga `scan_siris.py` loggas **varje prövat
docID**, så luckor är synliga i efterhand. Funna dokument skrivs in i samma
katalog- och nedladdningsfiler som övriga, så `build_index.py` och
`verify_archive.py` behöver inte veta varifrån de kom.

Räkna med ca 0,4 s per docID — ett intervall på 100 000 ID tar ungefär 11
timmar. Kör den när katalogkedjan är klar och verifierad.

---

## `archive_documents.py` — första generationen (behålls)

Det ursprungliga verktyget som arkiverade `beslut.json` (3 953 dokument) till
`archive/<år>/<diarienummer>/`. Ersatt av kedjan ovan, men behålls eftersom
dess manifest är underlag för att jämföra gammal och ny täckning.

Dess PDF:er är importerade till det platta lagret via
`fetch_pdfs.py --import-from archive`.

---

## Om lagring och git

`.gitignore` utesluter PDF-filerna men versionshanterar manifest och index —
de är beviset för vad arkivet innehåller. Själva filerna hör hemma i
objektlagring (Etapp 5).
