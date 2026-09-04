-- 0002_documents.sql — dokument, filversioner, extraherad text, publiceringshistorik
--
-- Handlingen (core.documents) och filen (core.document_versions) är skilda åt.
-- En ny version av samma beslut skapar en ny rad, aldrig en överskrivning.

BEGIN;

-- ── Handlingen ────────────────────────────────────────────────────────────

CREATE TABLE core.documents (
    id                  bigserial PRIMARY KEY,

    -- Publik, stabil identitet. Används i API och permalänkar och ändras
    -- aldrig. Härledd ur källans id: 'siris-648018'.
    document_key        text NOT NULL UNIQUE,

    source_system       text NOT NULL,
    source_id           text NOT NULL,

    -- Informationsprodukt. Styr vilken tjänst dokumentet tillhör och gör att
    -- Skolenkäten kan brytas ut utan att beslutsdatan rörs.
    product             text NOT NULL DEFAULT 'beslut'
                        CHECK (product IN ('beslut','skolenkaten','ombedomning','ovrigt')),

    document_type       text,
    title               text,
    document_year       smallint,

    -- NULL tills beslutsdatum extraherats ur dokumentets text. Katalogen ger
    -- bara årtal.
    document_date       date,

    review_area         text,

    -- Proveniens. Används aldrig för utlämning till användare — dokumentet
    -- serveras alltid ur egen lagring via storage_key.
    source_url          text,

    school_id           bigint REFERENCES core.schools(id),
    organization_id     bigint REFERENCES core.organizations(id),
    municipality_code   char(4) REFERENCES core.municipalities(code),

    current_version_id  bigint,          -- FK sätts efter document_versions

    publication_status  text NOT NULL DEFAULT 'published'
                        CHECK (publication_status IN ('published','unpublished','draft')),
    published_at        timestamptz,

    import_run_id       bigint REFERENCES core.import_runs(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- Gör importen idempotent: samma dokument kan hämtas om utan att dubbleras.
    UNIQUE (source_system, source_id)
);

COMMENT ON TABLE  core.documents IS 'En rad per handling. Filen ligger i core.document_versions.';
COMMENT ON COLUMN core.documents.document_key IS
    'Publik stabil identitet, t.ex. siris-648018. Ändras aldrig.';
COMMENT ON COLUMN core.documents.product IS
    'Informationsprodukt. Skolenkäten separeras logiskt från beslutsdata.';


-- ── Filen ─────────────────────────────────────────────────────────────────

CREATE TABLE core.document_versions (
    id              bigserial PRIMARY KEY,
    document_id     bigint NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    version_no      int    NOT NULL,

    -- Intern lagringsnyckel. Applikationen känner bara denna; bucket och
    -- endpoint kommer från konfiguration. Ingen leverantörsspecifik URL
    -- förekommer i modellen.
    storage_key     text NOT NULL UNIQUE,

    file_name       text NOT NULL,
    mime_type       text,
    file_kind       text,           -- pdf | doc | docx | rtf
    file_size       bigint,
    sha256          char(64),

    http_status     int,
    download_status text,
    error_message   text,
    downloaded_at   timestamptz,

    is_current      boolean NOT NULL DEFAULT true,
    import_run_id   bigint REFERENCES core.import_runs(id),
    created_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (document_id, version_no)
);

-- Exakt en aktuell version per dokument.
CREATE UNIQUE INDEX ux_document_versions_current
    ON core.document_versions (document_id) WHERE is_current;

ALTER TABLE core.documents
    ADD CONSTRAINT fk_documents_current_version
    FOREIGN KEY (current_version_id) REFERENCES core.document_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON TABLE  core.document_versions IS 'En rad per fil. Versioner ersätter aldrig varandra.';
COMMENT ON COLUMN core.document_versions.storage_key IS
    'S3-kompatibel nyckel, t.ex. siris/siris-648018.pdf. Bucket kommer från konfiguration.';
COMMENT ON COLUMN core.document_versions.sha256 IS
    'Checksumma över filens innehåll. Ligger på versionen, inte på dokumentet.';


-- ── Extraherad text ───────────────────────────────────────────────────────
-- Kroken för fulltextsökning i dokumentinnehåll och, senare, RAG. Kan vara tom
-- vid lansering; sökningen degraderar då till metadata utan att API:et ändras.

CREATE TABLE core.document_texts (
    id                  bigserial PRIMARY KEY,
    document_version_id bigint NOT NULL UNIQUE
                        REFERENCES core.document_versions(id) ON DELETE CASCADE,
    extraction_method   text,        -- pdftotext | antiword | docx | fallback
    char_count          int,
    page_count          int,
    content             text,
    extracted_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE core.document_texts IS
    'Extraherad dokumenttext per filversion. Underlag för fulltextsökning och framtida RAG.';


-- ── Publicerings- och revisionshistorik ───────────────────────────────────
-- Införs nu, inte när adminportalen byggs, så att historiken är komplett från
-- första importen.

CREATE TABLE core.publication_events (
    id                  bigserial PRIMARY KEY,
    document_id         bigint NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    document_version_id bigint REFERENCES core.document_versions(id) ON DELETE SET NULL,
    event_type          text NOT NULL
                        CHECK (event_type IN ('imported','published','unpublished',
                                              'version_added','metadata_changed','deleted')),
    -- Identitet som text: modellen förutsätter inget om var den kommer ifrån,
    -- så extern IdP (OIDC/Entra ID) kan införas utan schemaändring.
    actor               text NOT NULL DEFAULT 'system',
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    note                text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE core.publication_events IS
    'Revisionshistorik per dokument. Grund för adminportalens audit log.';

COMMIT;
