# Överlämning — från arkiv till nytt system

Detta dokument beskriver hur arkivet under `D:\siris` tas in i den nya
plattformen. Målet är att inget transformationsarbete ska återstå: filerna
laddas, främmande nycklar löses upp, klart.

Läs `docs/target-data-model.md` för modellens motivering.

---

## 1. Vad som finns

```
D:\siris\
  pdf\                    139 691 dokument, platt: siris-<docID>.<ext>
  catalog\                dokumentkatalogen från Skolinspektionens API
  index\                  arbetsfiler: nedladdningsstatus, extraktion, diarium
  export\                 ← LADDNINGSKLART DATASET, det här är leveransen
  reports\                verifieringsrapport
  logs\                   körningsloggar
```

Arbetsfilerna i `index\` behövs inte av mottagarsystemet. De finns kvar för
att kunna bygga om exporten och för att kunna spåra hur varje uppgift uppstod.

---

## 2. Exporten

`D:\siris\export\` innehåller en CSV per tabell, plus `load.sql` och
`manifest.json` med radantal och SHA-256 per fil.

| Fil | Motsvarar | Innehåll |
|---|---|---|
| `municipalities.csv` | `core.municipalities` | 291 kommuner |
| `organizations.csv` | `core.organizations` | huvudmän, aktuella och historiska |
| `schools.csv` | `core.schools` | skolenheter med kontakt och statistik |
| `documents.csv` | `core.documents` | handlingar |
| `document_versions.csv` | `core.document_versions` | filer, storage_key, SHA-256 |
| `cases.csv` | `beslut.cases` | ärenden ur diariet |
| `case_documents.csv` | `beslut.case_documents` | kopplingen |
| `survey_reports.csv` | `skolenkaten.survey_reports` | Skolenkäten |
| `document_texts.csv` | `core.document_texts` | extraherad text (med `--with-text`) |
| `import_runs.csv` | `core.import_runs` | proveniens |

**Filerna använder naturliga nycklar**, inte löpnummer: `document_key`,
`diarienummer`, kommunkod, organisationsnummer, skolenhetskod. Det gör två
saker. Exporten kan laddas om utan att dubblera något, och den är oberoende av
databasens tillstånd — inga id-kollisioner mellan miljöer.

Bygg om exporten när som helst:

```bash
python tools/export_dataset.py                  # allt
python tools/export_dataset.py --product beslut # utan Skolenkäten
python tools/export_dataset.py --with-text      # med dokumenttext för FTS/RAG
```

---

## 3. Laddning

```bash
export DATABASE_URL="postgresql://user:pass@host:5432/skolinsyn"

psql "$DATABASE_URL" -f migrations/0001_core.sql
psql "$DATABASE_URL" -f migrations/0002_documents.sql
psql "$DATABASE_URL" -f migrations/0003_beslut.sql
psql "$DATABASE_URL" -f migrations/0004_skolenkaten.sql

psql "$DATABASE_URL" -v export_dir="/sökväg/till/export" -f export/load.sql

psql "$DATABASE_URL" -f migrations/0005_indexes.sql
psql "$DATABASE_URL" -f migrations/0006_views.sql
```

Index läggs på **efter** laddningen — att skapa dem efter `COPY` är väsentligt
snabbare än att ladda in i en indexerad tabell.

`load.sql` laddar först till staging-tabeller som text, löser sedan upp
främmande nycklar och gör upsert. Skriptet är idempotent och kan köras om.

---

## 4. Filerna till objektlagring

`document_versions.storage_key` har formen `siris/siris-648018.pdf` och
motsvarar exakt filnamnet i `D:\siris\pdf\`. Uppladdningen är därför en rak
synkronisering:

```bash
aws s3 sync D:/siris/pdf/ s3://$S3_BUCKET/siris/ \
    --endpoint-url "$S3_ENDPOINT"
```

Fungerar mot valfri S3-kompatibel lagring — Cloudflare R2, MinIO lokalt, eller
en svensk leverantör senare. Verifiera med checksummorna i
`document_versions.csv`.

**Applikationen ska aldrig konstruera en URL till lagringen.** Den slår upp
`storage_key` och låter lagringsabstraktionen signera eller strömma. Modellen
innehåller ingen leverantörsspecifik URL, och `source_url` är enbart
proveniens — den pekar på SIRIS och används inte för utlämning.

Konfiguration via environment variables:

```
DATABASE_URL=postgresql://...
S3_ENDPOINT=https://<konto>.r2.cloudflarestorage.com
S3_REGION=auto
S3_BUCKET=skolinsyn-dokument
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_FORCE_PATH_STYLE=true        # krävs av MinIO
```

---

## 5. De tre saker som är lätta att göra fel

### `link_type` måste filtreras

Beslut skriver ut andra ärendens diarienummer i löptext. `siris-618125`
(Al-Azhar) och `siris-618126` (Edinit) nämner varandras nummer. Ett API som
inte filtrerar på `link_type = 'own_dnr'` kopplar båda besluten till båda
ärendena.

Vyerna i `0006_views.sql` gör det åt dig: `api.case_documents` innehåller bara
`own_dnr`, och korsreferenser ligger separat i
`api.case_document_references`.

### Två diarienummerserier

`SI ÅÅÅÅ:NNNN` är ett relativt nytt format. Dokument före ca 2018 bär
`NNN-ÅÅÅÅ:NNNN`, t.ex. `401-2014:2380`, ibland med tankstreck. Prefixet är en
serie-/handlingsslagskod, inte en del av ärendereferensen: `401-2014:2380` är
samma ärende som diariets `2014:2380`.

Exporten normaliserar allt till `SI ÅÅÅÅ:NNNN` i `cases.diarienummer` och
`case_documents.diarienummer`, och bevarar originalformen i
`documents.legacy_diarienummer`. En sökning på diarienummer fungerar därmed
likadant oavsett årgång — men ett gränssnitt som visar dokumentets
diarienummer bör visa originalformen.

### Allt är inte PDF

Beslut från 2003–2010 ligger som Word. `document_versions.file_kind` är
`pdf`, `doc` eller `docx` och `mime_type` är satt därefter. En nedladdningsknapp
som antar PDF kommer att servera fel Content-Type.

---

## 6. Vad som inte är klart

**`documents.document_date` är tom.** Katalogen ger bara årtal. Beslutsdatumet
står i dokumenttexten och kan extraheras i efterhand — kolumnen finns redan.

**`core.document_texts` fylls bara med `--with-text`.** Fulltextsökning i
dokumentinnehåll och RAG bygger på den. Sökningen degraderar till metadata om
tabellen är tom, utan att API:et ändras.

**Dokument utan ärendekoppling.** Ett dokument vars diarienummer inte finns i
`beslut.cases` får ingen rad i `case_documents`. Det är fortfarande sökbart på
skola, huvudman, kommun, typ och år. Antalet framgår av verifieringsrapporten.

---

## 7. Drift och dimensionering

### Vad som ligger var

| Komponent | Var | Varför |
|---|---|---|
| API + PostgreSQL | Oracle Cloud Free | räcker gott, se siffrorna nedan |
| Dokument (89 GB) | Cloudflare R2 | Oracles gratis objektlagring är 10 GB |
| Insamlingsverktygen | arbetsstation | I/O-bundna, 12–22 h per full körning |

Verktygen i `tools/` hör inte hemma på servern. De hämtar från externa källor
i timmar och matar exporten vidare; servern behöver bara ta emot den.

### Databasens storlek

Uppmätt genom att skala exportens verkliga filstorlekar till slutligt radantal.

| | Storlek |
|---|---:|
| Tabeller (heap) | 0,16 GB |
| Index, btree + GIN för fritextsök | 0,15 GB |
| **Utan dokumenttext** | **~0,3 GB** |
| Dokumenttext, 139 691 dokument | +0,64 GB |
| GIN-index över dokumenttexten | +1,03 GB |
| **Med fulltextsökning i innehållet** | **~2 GB** |

En databas på 0,3 GB ligger helt i sidcache. Sökningarna är indexerade uppslag
och GIN-träffar över ~222 900 ärenden, alltså millisekunder.

### Den regel som avgör om 1 vCPU räcker

**Applikationen får aldrig strömma dokumenten genom sig själv.** De serveras
från objektlagringen via signerad URL eller redirect. En enda samtidig
nedladdning av ett 5 MB-beslut kostar mer kapacitet än tusen sökfrågor.

Modellen är byggd för det: applikationen slår upp `storage_key` och låter
lagringslagret signera. `source_url` pekar på SIRIS och är enbart proveniens.

Följs regeln klarar 1 vCPU storleksordningen hundratals förfrågningar per
sekund — långt mer än tjänsten kommer se.

### Instanstyp

Det trånga är RAM, inte CPU.

| | AMD `E2.1.Micro` | ARM Ampere A1 |
|---|---|---|
| Kärnor | 1 (burstbar) | upp till 4 |
| RAM | 1 GB | upp till 24 GB |
| Räcker för metadatasök | ja, men trångt | med god marginal |
| Räcker för fulltext i innehållet | nej | ja |

ARM-instansen ingår också i Oracles alltid-gratis-nivå. Kapaciteten är ofta
slut i populära regioner, så det kan kräva några försök. Verifiera villkoren
mot Oracles aktuella dokumentation — de har ändrats över tid.

PostgreSQL-inställningar för 1 GB RAM finns i `docker-compose.yml`:
`shared_buffers=256MB`, `effective_cache_size=512MB`, `work_mem=8MB`,
`max_connections=20` med pool framför.

### Instansen: image och uppsättning

**Ubuntu 24.04 LTS**, Oracles plattformsimage `Canonical-Ubuntu-24.04`.
Minimal-varianten räcker.

**Kontrollera arkitekturen först.** 1 vCPU med 6 GB RAM betyder Ampere A1
(ARM); AMD-microns minne är fast 1 GB. `uname -m` ger `aarch64` respektive
`x86_64`. PostgreSQL och MinIO har officiella arm64-byggen, så compose-filen
fungerar oförändrad — men egna containrar måste byggas för rätt arkitektur.

**Oracles Ubuntu-images blockerar allt utom SSH i instansens egen brandvägg.**
Att öppna porten i konsolens Security List räcker inte:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

PostgreSQL-tuning styrs av `.env`. Förvalen passar 6 GB:

| | 6 GB (Ampere A1) | 1 GB (AMD micro) |
|---|---|---|
| `shared_buffers` | 1536MB | 256MB |
| `effective_cache_size` | 4GB | 512MB |
| `work_mem` | 16MB | 8MB |
| `maintenance_work_mem` | 512MB | 128MB |
| `max_connections` | 40 | 20 |

Med 6 GB ryms hela systemet inklusive fulltextsökning i dokumentinnehållet
(~2 GB databas). Med 1 GB räcker det bara till metadatasökning.

### Kostnad

Dokumenten på R2: 89 GB × ca 0,015 USD/GB ≈ **1,3 USD/månad**. R2 tar inget
betalt för utgående trafik, vilket är avgörande för en dokumenttjänst. Det är
också S3-API, så en flytt till Cleura senare är en ändrad `S3_ENDPOINT`.

### Lokal stack

`docker-compose.yml` startar PostgreSQL och MinIO. MinIO talar samma S3-API som
R2 och Cleura, så hela systemet kan köras och demonstreras utan molnkonto.

```bash
cp .env.example .env          # fyll i lösenorden
docker compose up -d
docker compose run --rm migrate
EXPORT_DIR=D:/siris/export docker compose run --rm load
```

`load` kör `export/load.sql` och lägger därefter på index och vyer, i den
ordningen — index efter `COPY` är väsentligt snabbare.

---

## 8. Uppdatera arkivet senare

Hela kedjan är återupptagbar och idempotent. För att fånga nytillkommet
material:

```bash
python tools/catalog_crawl.py     # nya dokument i katalogen
python tools/fetch_pdfs.py        # hämtar bara det som saknas
python tools/diarium_crawl.py     # nya ärenden
python tools/build_index.py       # bygger om kopplingarna
python tools/verify_archive.py    # verifierar
python tools/export_dataset.py    # ny export
psql "$DATABASE_URL" -v export_dir=... -f export/load.sql
```

`load.sql` gör upsert, så en full export kan laddas om utan att dubblera
något. Diariet är primärkälla för ärenden och får skriva över rader som
ursprungligen kom från `data.json`.
