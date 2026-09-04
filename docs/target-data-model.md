# Måldatamodell — PostgreSQL

Status: förslag, klart att implementera
Datum: 2026-09-04
Underlag: `docs/current-system-analysis.md`, arkivet under `D:\siris`

Modellen är dimensionerad efter vad arkivet faktiskt innehåller, inte efter vad
dagens `data.json` råkar ha. Siffrorna nedan är uppmätta, inte uppskattade.

| Storhet | Antal |
|---|---:|
| Ärenden i diariet (2008-10-13 →) | ~222 900 |
| Dokument i katalogen | 139 691 |
| — varav beslutsdokument | 47 320 |
| — varav Skolenkäten | 92 371 |
| Skolenheter | 6 409 |
| Huvudmän (inkl. historiska) | 1 344 |
| Kommuner | 291 |

---

## 1. Principer

**Ett dokument kan höra till flera ärenden, och ett ärende till flera dokument.**
Detta är inte ett gränsfall utan normalfallet: ett samlingsbeslut räknar upp
flera ärenden, och ett tillsynsärende får beslut plus uppföljningsbeslut.
Dagens `beslut.json` är en `dict` som inte kan uttrycka det, och har därför
redan tappat data. Kopplingen är många-till-många från början.

**Kopplingen bär sin egen härledning.** Varje rad i `case_documents` säger
*hur* den uppstod och *hur säker* den är. Ett dokument som bara nämner ett
annat ärendes diarienummer får `link_type = 'mentioned'`, inte samma status som
dokumentets eget ärende. Utan den distinktionen kopplas beslut till fel ärende.

**Härledda fält får aldrig se ut som källdata.** Dagens `kommun` gissas med
regex ur ärendemeningen. I modellen finns kommun som registrerat fält från
diariet, och där något är härlett bär det metod och konfidens.

**Filer och metadata är skilda.** `core.documents` beskriver handlingen,
`core.document_versions` beskriver filen. En ny version av samma beslut skapar
en ny rad i `document_versions`, aldrig en överskrivning.

**Lagring adresseras med interna nycklar.** Applikationen känner bara
`storage_key`. Ingen leverantörsspecifik URL finns i modellen.

**Fler informationsprodukter ska kunna tillkomma.** Delad infrastruktur ligger
i schemat `core`; produktspecifik data i egna scheman. Skolenkäten är den
första tillämpningen av det, en AI-assistent blir en senare.

---

## 2. Scheman

```
core          delad infrastruktur: dokument, filer, organisationer, import
beslut        Beslutstjänsten: ärenden och deras dokument
skolenkaten   Skolenkäten som egen informationsprodukt
```

Separationen är logisk, inte fysisk: samma databas, samma anslutning, samma
drift. Skolenkätens rapporter ligger som vanliga rader i `core.documents` —
det är bara den produktspecifika metadatan som får ett eget schema. Därför kan
Skolenkäten brytas ut, ges egna endpoints och egen behörighet utan att
beslutsdatan rörs.

```
                  ┌─────────────────────┐
                  │ core.import_runs    │  proveniens för varje körning
                  └──────────┬──────────┘
                             │
   ┌────────────────┐   ┌────▼─────────────┐   ┌──────────────────────┐
   │core.organizations│◄─┤ core.documents   ├──►│core.document_versions│
   └────────────────┘   │  (handlingen)    │   │      (filen)         │
   ┌────────────────┐   └──┬────────────┬──┘   └──────────┬───────────┘
   │ core.schools   │◄─────┘            │                 │
   └────────────────┘                   │      ┌──────────▼───────────┐
   ┌────────────────┐                   │      │ core.document_texts  │
   │core.municipali-│◄──────────────────┤      │  (FTS + RAG-krok)    │
   │      ties      │                   │      └──────────────────────┘
   └───────┬────────┘                   │
           │              ┌─────────────▼──────────┐
           │              │ core.publication_events│
           │              └────────────────────────┘
           │
   ┌───────▼────────┐   ┌────────────────────────┐
   │ beslut.cases   │◄──┤ beslut.case_documents  │──► core.documents
   └────────────────┘   └────────────────────────┘
                        ┌────────────────────────┐
                        │skolenkaten.survey_repo-│──► core.documents
                        │         rts            │
                        └────────────────────────┘
```

---

## 3. core — delad infrastruktur

### `core.import_runs`

En rad per körning av ett insamlingsverktyg. Allt som importeras pekar tillbaka
hit, så att en felaktig körning kan spåras och rullas tillbaka.

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `tool` | text | `catalog_crawl`, `fetch_pdfs`, `diarium_crawl`, `build_index` |
| `source_system` | text | `SIRIS`, `SI_DIARIUM`, `SI_CATALOG_API` |
| `started_at` / `finished_at` | timestamptz | |
| `status` | text | `running`, `completed`, `failed`, `interrupted` |
| `params` | jsonb | anropsparametrar, för reproducerbarhet |
| `stats` | jsonb | antal hämtade, fel, byte |
| `notes` | text | |

### `core.municipalities`

| Kolumn | Typ | Kommentar |
|---|---|---|
| `code` | char(4) PK | kommunkod, t.ex. `0180` |
| `name` | text | `Stockholm` |
| `county_code` | char(2) | härledd ur kommunkoden |

### `core.organizations` — huvudmän

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `code` | text UNIQUE | organisationsnummer |
| `name` | text | |
| `legal_form` | text | `Kommunal`, `Enskild`, `Region`, `Statlig` … |
| `is_current` | boolean | `false` för historiska huvudmän (491 st) |
| `municipality_code` | char(4) FK | |
| `search_tsv` | tsvector | genererad |

Historiska huvudmän behålls. Det är genom dem nedlagda skolenheters beslut går
att nå.

### `core.schools` — skolenheter

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `code` | text UNIQUE | Skolverkets skolenhetskod |
| `name` | text | |
| `organization_id` | bigint FK | |
| `municipality_code` | char(4) FK | |
| `school_forms` | text[] | `{grundskola,forskoleklass}` |
| `is_current` | boolean | |
| `status` | text | |
| `contact` | jsonb | telefon, e-post, webb, adress |
| `statistics` | jsonb | elevantal, behörighet — ögonblicksbild |
| `search_tsv` | tsvector | genererad |

Kontakt- och statistikfälten ligger i `jsonb` därför att de kommer från
Skolverkets API i en form som ändras över tid och inte hör till kärnmodellen.
Det som söks på är normaliserat; resten är bevarad rådata.

### `core.documents` — handlingen

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | intern nyckel |
| `document_key` | text UNIQUE | **publik, stabil identitet**: `siris-648018` |
| `source_system` | text | `SIRIS` |
| `source_id` | text | `648018` |
| `product` | text | `beslut`, `skolenkaten`, `ombedomning`, `ovrigt` |
| `document_type` | text | `tillsynsbeslut`, `uppfoljningsbeslut`, … |
| `title` | text | från katalogen |
| `document_year` | smallint | |
| `document_date` | date | NULL tills beslutsdatum extraherats ur texten |
| `review_area` | text | granskningsområde |
| `source_url` | text | proveniens, används aldrig för utlämning |
| `school_id` / `organization_id` | bigint FK | |
| `municipality_code` | char(4) FK | |
| `current_version_id` | bigint FK | pekar på aktuell fil |
| `publication_status` | text | `published`, `unpublished`, `draft` |
| `published_at` | timestamptz | |
| `import_run_id` | bigint FK | |
| `search_tsv` | tsvector | genererad ur titel + typ + skola + huvudman |
| `created_at` / `updated_at` | timestamptz | |

UNIQUE `(source_system, source_id)` gör importen idempotent: samma dokument kan
hämtas om utan att dubbleras.

`document_key` är den identitet som exponeras i API och permalänkar. Den är
härledd ur källans id och ändras aldrig.

### `core.document_versions` — filen

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | bigint FK | |
| `version_no` | int | 1, 2, 3 … |
| `storage_key` | text UNIQUE | `siris/siris-648018.pdf` |
| `file_name` | text | |
| `mime_type` | text | `application/pdf`, `application/msword` … |
| `file_size` | bigint | |
| `sha256` | char(64) | |
| `file_kind` | text | `pdf`, `doc`, `docx` |
| `http_status` | int | från hämtningen |
| `download_status` | text | `ok`, `ok_not_pdf`, `http_error` … |
| `error_message` | text | |
| `downloaded_at` | timestamptz | |
| `is_current` | boolean | |
| `import_run_id` | bigint FK | |

UNIQUE `(document_id, version_no)` och UNIQUE `(document_id) WHERE is_current`.

Checksumman ligger här, inte på dokumentet, eftersom det är filen som har en
checksumma. Adminportalens "ersätt dokument med ny version" (Etapp 7) blir en
ny rad — originalet raderas aldrig.

### `core.document_texts` — extraherad text

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `document_version_id` | bigint FK UNIQUE | |
| `extraction_method` | text | `pdftotext`, `antiword`, `docx`, `fallback` |
| `char_count` | int | |
| `page_count` | int | |
| `content` | text | |
| `content_tsv` | tsvector | genererad, GIN-indexerad |
| `extracted_at` | timestamptz | |

Detta är kroken för fulltextsökning i dokumentinnehåll. Den kan fyllas i
efterhand utan att någon annan tabell ändras — arkiveringsverktyget sparar
redan texten med `build_index.py --keep-text`.

### `core.publication_events` — revisionshistorik

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | bigint FK | |
| `document_version_id` | bigint FK | nullable |
| `event_type` | text | `imported`, `published`, `unpublished`, `version_added`, `metadata_changed` |
| `actor` | text | användaridentitet, `system` vid import |
| `occurred_at` | timestamptz | |
| `note` | text | |
| `metadata` | jsonb | före/efter vid ändringar |

Grunden för Etapp 7:s audit log. Införs nu så att historiken är komplett från
första importen, inte från den dag adminportalen byggs.

---

## 4. beslut — Beslutstjänsten

### `beslut.cases` — ärenden

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `diarienummer` | text UNIQUE | `SI 2024:21120` — kanonisk form |
| `case_year` | smallint | |
| `case_no` | int | |
| `diary_series` | text | `2` (2008–2018) eller `6` (2019–) |
| `diary_caseref` | int | portalens interna id, för spårbarhet |
| `subject` | text | ärendemening |
| `case_type` | text | `Anmälan`, `Riktad tillsyn`, `Uppgift` … |
| `status` | text | `Ad acta`, pågående … |
| `department` | text | handläggande avdelning |
| `municipality_code` | char(4) FK | |
| `municipality_name` | text | som registrerad i diariet |
| `registered_date` | date | |
| `closed_date` | date | |
| `source` | text | `diarium` eller `data_json` |
| `import_run_id` | bigint FK | |
| `search_tsv` | tsvector | genererad ur diarienummer + ärendemening + kommun |
| `created_at` / `updated_at` | timestamptz | |

`diarienummer` är unikt, btree-indexerat och normaliserat till `SI ÅÅÅÅ:NNNN`.
Äldre ärenden visas i portalen som `2014:610` utan prefix; de normaliseras till
samma form, så att en sökning fungerar likadant oavsett årgång.

### `beslut.case_documents` — kopplingen

| Kolumn | Typ | Kommentar |
|---|---|---|
| `case_id` | bigint FK | |
| `document_id` | bigint FK | |
| `link_type` | text | `own_dnr` eller `mentioned` |
| `link_method` | text | `dnr_ur_pdf_text`, `manuell`, `katalog` |
| `link_confidence` | text | `high`, `reference`, `unmatched_case` |
| `dnr_position` | int | ordningen numret förekom i texten |
| `created_at` | timestamptz | |

PRIMARY KEY `(case_id, document_id, link_type)`.

**`link_type` är inte kosmetisk.** Beslut skriver ut andra ärendens
diarienummer i löptext. `siris-618125` (Al-Azhar) och `siris-618126` (Edinit)
nämner varandras nummer — utan distinktionen kopplas båda besluten till båda
ärendena. Publika API:et ska som standard filtrera på `own_dnr`; `mentioned`
exponeras som korsreferenser.

---

## 5. skolenkaten — egen informationsprodukt

### `skolenkaten.survey_reports`

| Kolumn | Typ | Kommentar |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | bigint FK UNIQUE | → `core.documents` |
| `report_level` | text | `skolenhet`, `huvudman` |
| `respondent_group` | text | `elev`, `vardnadshavare`, `pedagogisk_personal` |
| `school_form` | text | `grundskola`, `forskoleklass`, `gymnasieskola` … |
| `grade` | text | `åk 5`, `åk 9` … |
| `term` | text | `VT25`, `HT14` |
| `year` | smallint | |
| `school_id` / `organization_id` | bigint FK | |
| `municipality_code` | char(4) FK | |

Fälten härleds ur rapporttiteln, som är strukturerad:
`"Grundskolan, Elevenkäten, åk 5, Södertälje, Lina grundskola, VT18"`.

Skolenkäten har **ingen** koppling till `beslut.cases`. Rapporterna saknar
diarienummer i källan, och hör inte till ett ärende. Det är också skälet till
att den fungerar som en egen produkt.

---

## 6. Sökning

**Metadata** söks via genererade `tsvector`-kolumner med GIN-index:

```sql
ALTER TABLE beslut.cases ADD COLUMN search_tsv tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('swedish', coalesce(diarienummer,'')), 'A') ||
    setweight(to_tsvector('swedish', coalesce(subject,'')),      'B') ||
    setweight(to_tsvector('swedish', coalesce(municipality_name,'')), 'C')
  ) STORED;
CREATE INDEX ix_cases_tsv ON beslut.cases USING gin (search_tsv);
```

Konfigurationen `swedish` finns i PostgreSQL som standard.

**Diarienummer** söks exakt via unikt btree-index, och som prefix via
`text_pattern_ops`, så att `SI 2024:` fungerar.

**Dokumentinnehåll** söks via `core.document_texts.content_tsv`. Tabellen kan
vara tom vid lansering; sökningen degraderar då till metadata utan att API:et
ändras.

---

## 7. Förberedd för AI/RAG — men inte byggd

Etapp 9 ska inte implementeras nu. Modellen är dock lagd så att den kan
tillkomma **utan att någon befintlig tabell ändras**:

```sql
-- Läggs till senare, kräver pgvector.
CREATE TABLE core.document_chunks (
    id                  bigserial PRIMARY KEY,
    document_version_id bigint NOT NULL REFERENCES core.document_versions(id),
    chunk_no            int    NOT NULL,
    page_from           int,
    page_to             int,
    content             text   NOT NULL,
    token_count         int,
    embedding           vector(1024),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_version_id, chunk_no)
);
```

Det som gör detta möjligt utan omdesign är tre val som görs nu:

1. Texten hänger på **version**, inte på dokument. En ny version ger nya
   chunkar utan att gamla blir felaktiga.
2. `document_key` är stabil, så en RAG-källhänvisning pekar rätt för alltid.
3. `case_documents` bär `link_type`, så en AI-assistent kan skilja "beslutet i
   ärendet" från "ett beslut som nämner ärendet" — annars blir svaren fel på
   just den punkt där de måste vara rätt.

---

## 8. Migreringsordning

```
migrations/0001_core.sql          scheman, import_runs, geografi, organisationer
migrations/0002_documents.sql     documents, versions, texts, publication_events
migrations/0003_beslut.sql        cases, case_documents
migrations/0004_skolenkaten.sql   survey_reports
migrations/0005_indexes.sql       index och sökvektorer
migrations/0006_views.sql         läsvyer för API:et
```

Laddning sker med `COPY` från exporten i `D:\siris\export\`, se
`docs/handover.md`.

---

## 9. Vad modellen medvetet inte gör

**Ingen egen användartabell för lösenord.** Etapp 7 ska kunna använda extern
IdP (Entra ID/OIDC). Modellen har `actor` som text i `publication_events` och
förutsätter inget om var identiteten kommer ifrån.

**Ingen normalisering av ärendetyp till en kodtabell.** Diariets `ärendetyp`
och den 108-poster långa processklassificeringen i portalen är myndighetens
egna och ändras över tid. De lagras som text och normaliseras i vy-lagret om
det behövs.

**Ingen lagring av filinnehåll i databasen.** Filerna ligger i objektlagring.
Databasen håller `storage_key`, checksumma och storlek.
