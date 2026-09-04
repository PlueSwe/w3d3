# Nulägesanalys – w3d3.skolinsyn.se

Datum för analys: 2026-09-04
Analyserad commit: `a9a1398` (branch `main`)

Dokumentet beskriver systemet **som det faktiskt ser ut idag**, inte som det borde se ut.
Inga ändringar i befintlig kod har gjorts som del av analysen.

---

## 1. Sammanfattning

Dagens w3d3 är **ingen applikation i vanlig mening**. Det är en statisk
enkelsidig webbplats (`index.html`) som laddar tre JSON-filer direkt i webbläsaren,
plus två fristående PHP-skript som gör live-scraping mot Skolinspektionen.

Det finns **ingen databas**, **ingen backend-modell**, **inget API** och
**ingen lokal kopia av ett enda beslutsdokument**. Samtliga beslut ligger hos
tredje part (Skolverket/SIRIS) och nås via en URL som konstrueras i frontend
från ett numeriskt `docID`.

Den enskilt viktigaste slutsatsen: **hela beslutstillgången är beroende av att
en extern myndighets legacy-tjänst (SIRIS) fortsätter svara på en
querystring-baserad fil-endpoint.** Om SIRIS stängs eller ID-rymden numreras om
finns ingenting kvar. Se avsnitt 8.

---

## 2. Teknikstack

| Lager | Teknik | Fil |
|---|---|---|
| Frontend | En enda HTML-fil, vanilla JS, ingen build, ingen ramverk, inline CSS | `index.html` (88 kB, ~1600 rader) |
| "Databas" | Statiska JSON-filer serverade av webbservern | `data.json`, `beslut.json`, `skolor.json` |
| Backend | Två fristående PHP-skript (PHP 7/8, cURL, DOMDocument) | `fetch_new.php`, `fetch_case.php` |
| Datainsamling | Fem fristående Python 3-skript, körs manuellt | `scan_siris.py`, `match_skolkod.py`, `build_skolor.py`, `fix_wrong_matches.py`, `add_pupils.py`, `add_stats_per_type.py` |
| Externa API:er | Skolverket Planned Educations API v4 (anropas direkt från webbläsaren) | – |
| Externt dokumentarkiv | SIRIS (`siris.skolverket.se/siris/ris.openfile`) | – |

Ingen paketmanifest (`package.json`, `requirements.txt`, `composer.json`) finns.
Inga tester. Ingen CI. Ingen containerisering. Deploy sker uppenbarligen genom
att filer läggs på en PHP-kapabel webbhotellsyta.

---

## 3. Var ligger databasen?

**Det finns ingen databas.**

Persistensen är tre JSON-filer som ligger i webbroten och laddas i sin helhet av
klienten vid varje sidladdning:

```js
// index.html:598
const [r, rb, rs] = await Promise.all([
  fetch('data.json'),
  fetch('beslut.json').catch(()=>null),
  fetch('skolor.json').catch(()=>null)
]);
```

| Fil | Storlek | Format | Poster |
|---|---|---|---|
| `data.json` | **23,1 MB** | JSON-array | 82 160 ärenden |
| `beslut.json` | 94,8 kB | JSON-objekt (map) | 3 953 dokumentkopplingar |
| `skolor.json` | 3,7 MB | JSON-array | ~6 700 skolenheter |
| `schools_raw.json` | 640 kB | JSON-array | rådata från Skolverket, används endast av Python-skripten |

Det innebär att varje besökare laddar ner ~27 MB JSON. Det är inte hållbart och
är i sig ett starkt argument för Etapp 6.

`data.json` skrivs av `fetch_new.php` med `file_put_contents()` — **utan lås,
utan backup, utan atomisk write**. Två samtidiga anrop kan korrumpera filen.

---

## 4. Datamodell (nuvarande)

### 4.1 `data.json` — ärenden

Array av objekt. Nycklarnas frekvens över 82 160 poster:

| Fält | Förekomst | Beskrivning | Exempel |
|---|---|---|---|
| `dno` | 82 160 (100 %) | Diarienummer, **primärnyckel** | `"SI 2026:8762"` |
| `date` | 82 160 (100 %) | Registreringsdatum (ISO) | `"2026-09-03"` |
| `subject` | 82 160 (100 %) | Ärendemening, fritext | `"Uppgift om Höglandsskolan med huvudmannen STOCKHOLMS KOMMUN i Stockholms kommun"` |
| `typ` | 82 160 (100 %) | Ärendetyp | `"Uppgift"`, `"Anmälan"`, `"Riktad tillsyn"` |
| `kommun` | 82 160 (100 %) | **Härledd** ur `subject` via regex/ordlista | `"Stockholms"` |
| `hauptman` | 82 160 (100 %) | **Härledd** – `"Kommunal"` / `"Enskild"` m.fl. | `"Kommunal"` |
| `skolkod` | 32 097 (39 %) | Skolverkets skolenhetskod, **härledd via fuzzy matchning** | `"12345678"` |

Endast `dno`, `date`, `subject` och `typ` är **källdata**. Övriga fält är
beräknade/gissade av lokala skript (se avsnitt 7) och har varierande kvalitet.

`dno` är unikt över alla 82 160 poster — det håller som naturlig nyckel.

Fördelning per år:

| År | Ärenden |
|---|---|
| 2019 | 9 857 |
| 2020 | 9 212 |
| 2021 | 9 025 |
| 2022 | 11 695 |
| 2023 | 10 383 |
| 2024 | 10 356 |
| 2025 | 12 870 |
| 2026 | 8 762 (t.o.m. 2026-09-03) |

Vanligaste ärendetyper: `Uppgift` (33 023), `Anmälan` (16 537),
`Uppföljning` (6 253), `Riktad tillsyn` (4 336), `Ansökan` (2 364).

### 4.2 `beslut.json` — dokumentkoppling

Ett platt objekt: **diarienummer → SIRIS docID**.

```json
{
  "SI 2023:1537": 648005,
  "SI 2023:2083": 648013,
  "SI 2023:10058": 649282
}
```

Detta är hela dokumentmodellen. Den har allvarliga strukturella begränsningar:

- **Ett ärende kan bara ha ett dokument.** Datastrukturen är en 1:1-map. Om ett
  ärende har flera publicerade beslut (t.ex. beslut + uppföljningsbeslut, eller
  ett beslut per skolenhet i ett samlingsärende) skriver det senast skannade
  `docID` över det tidigare — tyst.
- **Ingen dokumenttyp, inget datum, ingen filstorlek, ingen checksumma, ingen titel.**
- **Ingen indikation på matchningens säkerhet.**

### 4.3 `skolor.json` — skolregister

Berikningsdata från Skolverkets API (namn, skoltyp, `areaCode`, elevantal,
behörighetsgrad, kontaktuppgifter). Används enbart för presentation, inte för
dokumentkoppling.

### 4.4 Relationer

```
data.json (ärende)
   │  dno  (1:1, valfri)
   └──► beslut.json ──► docID ──► https://siris.skolverket.se/siris/ris.openfile?docID=N
   │
   │  skolkod (n:1, 39 % täckning, heuristisk)
   └──► skolor.json (skolenhet)
```

Det finns **ingen** entitet för huvudman, kommun, dokumentversion,
publiceringshändelse eller importkörning.

---

## 5. Hur identifieras diarienummer?

Två oberoende vägar, med olika normalisering — vilket i sig är en buggkälla.

**A. Från sökportalen** (`fetch_new.php:parseTable`): kolumn 0 i `GridView1`
måste matcha `/^SI\s*\d/`. Strängen sparas som den kommer, typiskt `"SI 2026:8762"`.

**B. Ur PDF-innehållet** (`scan_siris.py:fetch_dno`): PDF:ens första sida
extraheras med `pdftotext` och matchas mot `/Dnr\s+(SI\s*\d{4}:\d+)/`.

Normaliseringen i B är trasig:

```python
dno = m.group(1).replace(' ', ' ').strip()   # ersätter mellanslag med mellanslag
```

Raden är avsedd att normalisera **hårt mellanslag (U+00A0)** till vanligt
mellanslag, men båda argumenten är samma tecken i källfilen. Resultatet syns
direkt i data: `beslut.json` innehåller nycklar som `"SI2020:..."`,
`"SI2022:..."`, `"SI2023:..."` (utan mellanslag). **5 av 3 953 nycklar i
`beslut.json` matchar inget ärende i `data.json`** — dessa är sannolikt
huvudsakligen normaliseringsfel.

---

## 6. Hur avgör systemet att ett ärende har beslut?

Enbart genom en nyckelslagning i frontend:

```js
// index.html:1032
if (onlyBeslut && !beslutMap[r.dno]) return false;
```

`beslutMap[dno]` finns → röd prick i listan och en PDF-länk i detaljpanelen.
Saknas → en **heuristik** (`statusHeuristic`, `index.html:1115`) gissar om
ärendet är pågående baserat på ärendetyp och ålder, och visar en badge
`"Pågående?"`. Detta är en uppskattning och märks som sådan i UI:t.

**Täckning: 3 948 av 82 160 ärenden = 4,8 %.** Det betyder inte att 95 % saknar
beslut — det betyder att vi bara känner till 4,8 % (se avsnitt 7.1).

---

## 7. Varifrån kommer dokumentlänkarna?

Länkarna är **genererade i frontend** ur ett skannat `docID`:

```js
// index.html:1154
const docID = beslutMap[dno];
const beslutLink = docID
  ? `<a href="https://siris.skolverket.se/siris/ris.openfile?docID=${docID}" ...>`
```

De är alltså varken lagrade som URL:er i en databas eller hämtade vid
visningstillfället. Basen är hårdkodad i HTML.

### 7.1 Hur `docID` togs fram — brute force

`scan_siris.py` är kärnan i dagens dokumentkoppling. Den:

1. itererar över **hela ID-rymden** `docID = 630000 … 680000`,
2. hämtar `https://siris.skolverket.se/siris/ris.openfile?docID=N` för varje ID,
3. kastar allt som inte börjar med `%PDF`,
4. kör `pdftotext -l 1` på sida 1,
5. regex-matchar `Dnr SI YYYY:NNNN`,
6. skriver `{dno: docID}` till `beslut.json`.

Kopplingen ärende↔dokument härleds alltså **ur PDF:ens eget innehåll**, inte ur
någon katalog eller något API. Det är faktiskt den mest tillförlitliga metoden
som finns tillgänglig — men den är inte fullständig:

- Skanningen är **ofullständig**. Högsta funna `docID` är 666 872, trots att
  intervallet gick till 680 000. Körningen verkar ha avbrutits eller haft
  luckor. Lägsta funna är 610 065 — utanför standardintervallet, från en tidigare körning.
- Intervallet täcker i praktiken **ca 2022–2026**. Beslut per år i `beslut.json`:
  2018: 1, 2019: 25, 2020: 35, 2021: 74, 2022: 431, 2023: 939, 2024: 1 066,
  2025: 1 012, 2026: 367. Åren 2019–2021 är i praktiken otäckta.
- Fel (timeout, 5xx) räknas men **loggas inte per docID** och skannas aldrig om.
  Ett `docID` som tillfälligt fallerade är permanent förlorat för mappningen.
- Dokument utan `Dnr`-rad på sida 1, eller med annan formatering, missas tyst.

Ingen av dessa förluster syns någonstans i systemet idag.

### 7.2 `fetch_case.php` — död kod

`fetch_case.php` gör en live-sökning mot `skolinspektionen.se/beslut-rapporter/sok-beslut/`
och regex:ar ut SIRIS-länkar. Den **anropas inte längre från `index.html`**
(borttagen i commit `8b559c6`, "Simplify detail panel: remove PHP beslut fetch").
Den är kvar i repot men används inte. Den är dock **historiskt intressant**: den
visar att Skolinspektionens egen sökbeslutssida är en alternativ källa för
dokument-URL:er, inklusive icke-SIRIS-PDF:er.

### 7.3 `fetch_new.php` — inkrementell ärendeimport

Anropas via knapp i UI:t. Läser högsta `date` i `data.json`, scrapar
Skolinspektionens ASP.NET-sökportal (`externsearchport.skolinspektionen.se`)
med ViewState/EventValidation-hantering, delar datumintervall rekursivt när
>1000 träffar, och appendar nya `dno` till `data.json`.

Den hämtar **inga dokument** och rör aldrig `beslut.json`.

---

## 8. Kan ett ärende ha flera dokument? Finns dokumenttyper?

**Verkligheten: ja.** Ett tillsynsärende kan ha föreläggande, uppföljningsbeslut,
avslutsbeslut; ett samlingsärende (1 601 st i data) kan ha ett beslut per
skolenhet.

**Systemet: nej.** `beslut.json` är en `dict` — ett `docID` per `dno`, sista
skrivning vinner. I nuvarande fil finns 3 953 unika `docID` för 3 953 `dno`,
alltså exakt 1:1 utan dubbletter. Det bevisar inte att 1:1 är sant i
verkligheten — det bevisar att datastrukturen inte kan representera något annat.
**Eventuella extra dokument har redan tyst kastats bort under skanningen.**

Dokumenttyp finns inte alls som begrepp. Ärendetyp (`typ`) finns på ärendet, men
säger inget om dokumentets art.

---

## 9. Befintlig kod för import / scraping / matchning

| Skript | Syfte | Status |
|---|---|---|
| `fetch_new.php` | Inkrementell ärendescraping från sökportalen | **Aktiv**, anropas från UI |
| `fetch_case.php` | Live-uppslag av beslutslänk per ärende | **Död kod**, ej anropad |
| `scan_siris.py` | Brute-force-skanning av SIRIS docID → `beslut.json` | Manuell, ofullständig körning |
| `build_skolor.py` | Bygger `skolor.json` från Skolverkets API | Manuell |
| `match_skolkod.py` | Matchar ärende→skolenhetskod (inverterat ordindex) | Manuell, 39 % täckning |
| `fix_wrong_matches.py` | Rättar skolkod-matchningar med fel kommun | Manuell |
| `add_pupils.py`, `add_stats_per_type.py` | Berikar `skolor.json` med statistik | Manuell |
| `W3D3_Searchport_export.csv` | Manuell CSV-export från sökportalen (dno, datum, ärendemening) | Referensdata |

Ingen av dessa har loggning till fil, återupptagning, felrapport eller
idempotensgarantier värda namnet.

---

## 10. Identifierade risker

Rangordnade efter hur illa det blir om de inträffar.

### R1 — Kritisk: noll lokal kopia av beslutsdokument
Systemet äger inte en enda PDF. Allt innehåll som gör tjänsten värdefull ligger
hos Skolverket. Skulle SIRIS läggas ned, byggas om, eller numrera om sin
ID-rymd, förlorar w3d3 **all** dokumenttillgång omedelbart och oåterkalleligt.
SIRIS är en legacy-tjänst; `ris.openfile?docID=N` är precis den sortens
querystring-endpoint som försvinner vid en plattformsmigrering.

*Verifierat 2026-09-04: endpointen svarar fortfarande, HTTP 200,
`content-type: application/pdf`. Tidsfönstret är öppet nu.*

**Detta är anledningen till att Etapp 2 går före allt annat.**

### R2 — Kritisk: dokumentmodellen kan inte representera flera dokument per ärende
`beslut.json` som `dict` har redan orsakat tyst dataförlust under skanningen.
Förlusten är inte mätbar i efterhand utan att skanna om.

### R3 — Hög: dokumentkopplingen är ofullständig och luckorna är osynliga
4,8 % täckning, avbruten skanning, otäckta år 2019–2021, fel som aldrig
skannas om och aldrig loggas. Systemet kan inte skilja "inget beslut finns" från
"vi hittade inte beslutet".

### R4 — Hög: diarienummer normaliseras inkonsekvent
Trasig `.replace(' ', ' ')` i `scan_siris.py:41` ger nycklar utan mellanslag.
5 poster i `beslut.json` matchar inget ärende.

### R5 — Medel: `data.json` skrivs utan lås eller backup
`file_put_contents()` i `fetch_new.php` på en 23 MB-fil. Samtidiga anrop eller
avbruten skrivning ger korrupt fil utan återställningsväg. Ingen versionering.

### R6 — Medel: skolkod-matchning är heuristisk och odokumenterad i data
39 % täckning, byggd på fuzzy ordmatchning + kommunheuristik. Det finns inget
konfidensvärde i datat — en gissad matchning ser exakt ut som en säker.

### R7 — Medel: hela datamängden laddas i klienten
27 MB per sidladdning. Skalar inte, och blockerar server-side sökning,
paginering och API.

### R8 — Medel: scraping-beroende av ASP.NET-portal
`fetch_new.php` bygger på `GridView1`, `__VIEWSTATE`, `txtFromDate`. En
omdesign av portalen bryter ärendeimporten tyst (parse returnerar 0 rader,
skriptet svarar `{"status":"ok","new":0}`).

### R9 — Låg: `SSL_VERIFYPEER => false` i båda PHP-skripten
Ingen certifikatvalidering mot externa myndighetssajter.

### R10 — Låg: `fetch_case.php` är oautentiserad och gör godtyckliga utgående anrop
Död kod som fortfarande är exponerad om den ligger publikt.

---

## 11. Rekommenderad migreringsstrategi

Ordningen är vald så att det oåterkalleliga görs först och det ombyggbara sist.

### Steg 1 — Arkivera allt vi känner till, nu (Etapp 2)
Ladda ner alla 3 948 kända dokument till lokalt arkiv med SHA-256, HTTP-status
och fullständigt manifest. Detta är rent additivt, rör ingen befintlig kod och
kan köras omedelbart. **Prioritet framför allt annat.**

### Steg 2 — Verifiera arkivet (Etapp 3)
Bevisa att varje fil är en riktig PDF, inte bara ett HTTP 200. Rapportera
luckor, dubbletter och osäkra kopplingar explicit.

### Steg 3 — Utöka täckningen (efter Etapp 2–3, före Etapp 6)
Med arkivverktyget på plats: kör om SIRIS-skanningen fullständigt och
resumebar, med felloggning per `docID`, ett bredare ID-intervall, och en
datamodell som tillåter **flera dokument per ärende**. Återanvänd
`fetch_case.php`:s insikt om Skolinspektionens egen beslutssida som andra källa.
Detta höjer täckningen långt över 4,8 % — men får inte fördröja Steg 1.

### Steg 4 — Ny datamodell i PostgreSQL (Etapp 4)
Manifestet från Steg 1 är designunderlaget. Modellen ska från början ha
`documents` som egen entitet med n:1 mot `cases`, plus `document_versions`.
Härledda fält (`kommun`, `hauptman`, `skolkod`) ska bära konfidens och
härledningsmetod, inte låtsas vara källdata.

### Steg 5 — Objektlagring (Etapp 5)
Flytta arkivet från filsystem till S3-kompatibel lagring. Arkivets
katalogstruktur (`archive/<år>/<diarienummer>/<document-id>.pdf`) är avsiktligt
vald så att den kan mappas 1:1 till storage keys utan omorganisation.

### Steg 6 — Beslutstjänst v1 (Etapp 6)
Först här byggs ny frontend/API. Dokumenten serveras då från eget arkiv med
SIRIS-URL:en kvar enbart som `source_url`-proveniens.

### Uppdatering 2026-09-04: en betydligt bättre källa hittades

Under arbetet med Etapp 2 undersöktes vilka källor som faktiskt finns, i
stället för att bara arkivera det `beslut.json` råkade känna till. Det visade
sig att Skolinspektionen har ett **publikt, uppräkningsbart katalog-API** som
inte används av dagens system:

```
/api/siris/counties/                                    291 kommuner
/api/siris/counties/{kod}/documents                     dokument per kommun
/api/siris/counties/{kod}/schools/current               skolenheter i kommunen
/api/siris/schools/{kod}/documents                      dokument per skolenhet
/api/siris/companiesandorganisations/                   1 344 huvudmän
/api/siris/companiesandorganisations/{kod}/documents    dokument per huvudman
/api/siris/companiesandorganisations/{kod}/schools/current
```

Det hittades genom att spåra webbkomponenten `<decisionfilter>` på
`skolinspektionen.se/beslut-rapporter/sok-beslut/` till dess JavaScript-bundle.

Detta förändrar bilden på flera avgörande punkter:

| | `beslut.json` (idag) | Katalog-API:et |
|---|---|---|
| Metod | brute force över docID-rymden | auktoritativ uppräkning |
| Kända dokument | 3 953 | **32 000+** (räkningen pågår) |
| Dokumenttitel | saknas | ja |
| Dokumenttyp | saknas | härledbar ur titeln |
| År | saknas | ja |
| Granskningsområde | saknas | ja |
| Koppling till skolenhet/huvudman/kommun | saknas | ja |
| Flera dokument per enhet | omöjligt | ja |
| Historiska huvudmän | nej | ja (491 st) |
| Täckning kan bevisas | nej | ja, noder kan räknas |

Katalogen innehåller dessutom **Skolenkätens** rapporter och resultat från
ombedömning av nationella prov — alltså underlaget för Etapp 8, som därmed
inte behöver en egen insamlingskedja.

**Vad katalogen inte ger:** diarienummer. Kopplingen dokument↔ärende måste
fortfarande härledas ur dokumentets eget innehåll. Det är i sak samma metod som
`scan_siris.py` använder, och den är rätt metod — den är den enda som faktiskt
bevisar sambandet. Skillnaden är att den nu tillämpas på ett känt, uppräknat
dokumentbestånd i stället för på en gissad ID-rymd, och att resultatet lagras
som en många-till-många-relation i stället för en `dict`.

**Kvarstående blind fläck:** `schools/current` listar bara aktuella
skolenheter. Nedlagda enheters dokument nås via historiska huvudmän, men
täckningen kan inte bevisas den vägen. Därför behålls docID-svepningen som
komplement (`tools/sweep_docids.py`) — nu med loggning av varje prövat ID, så
att luckor blir synliga.

### Reviderad arkitektur för arkivet

Arkivet flyttades till `D:\siris` med **platt PDF-lager**:

```
D:\siris\pdf\siris-<docID>.pdf
```

`docID` är dokumentets identitet i källan och globalt unikt. Platt struktur ger
tre saker en trädstruktur inte ger: samma dokument kan inte hamna på två
ställen, ett dokument som gäller flera ärenden lagras en gång i stället för en
gång per ärende, och sökvägen mappas rakt av till en S3-nyckel i Etapp 5.

Kopplingen ligger i stället i `D:\siris\index\case_documents.csv` — en rad
per (ärende, dokument), många-till-många, med `link_confidence` och den
metod kopplingen härleddes ur.

### Konsekvens för risk R1–R3

- **R1 (ingen lokal kopia)** är åtgärdad för de 3 953 tidigare kända dokumenten
  och åtgärdas för hela katalogen i samma körning.
- **R2 (kan inte representera flera dokument per ärende)** är åtgärdad i
  arkivets datamodell. Kopplingstabellen är många-till-många från början.
- **R3 (ofullständig och osynlig täckning)** är kraftigt reducerad: täckningen
  går från 3 953 dokument till 32 000+, och varje nod som hämtas loggas så att
  luckor kan pekas ut.

En ny observation värd att notera: docID 658710 (`SI 2025:4284`) svarar
konsekvent med **HTTP 500** hos SIRIS. Det dokumentet går inte att arkivera —
det är trasigt hos källan, inte hos oss. Det är exakt den sortens tyst förlust
som dagens system inte kan upptäcka.

### Principer genom hela migreringen

- **Rör inte befintlig kod förrän arkivet är verifierat.** `index.html`,
  `data.json` och `beslut.json` ska fungera oförändrat under hela Etapp 2–3.
- **Nya verktyg läggs i `tools/`**, skriver till `archive/` och `reports/`.
- **Ingenting raderas.** `beslut.json` behålls som historiskt underlag även
  efter migrering till PostgreSQL.
- **Proveniens sparas alltid**: käll-URL, hämtningstidpunkt, HTTP-status och
  checksumma per dokument, så att arkivet kan revideras mot källan i efterhand.
