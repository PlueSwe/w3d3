-- 0001_core.sql — scheman, proveniens, geografi och organisationer
-- PostgreSQL 14+
--
-- Delad infrastruktur för samtliga informationsprodukter. Produktspecifika
-- tabeller ligger i egna scheman (se 0003, 0004).

BEGIN;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS beslut;
CREATE SCHEMA IF NOT EXISTS skolenkaten;

COMMENT ON SCHEMA core        IS 'Delad infrastruktur: dokument, filer, organisationer, import.';
COMMENT ON SCHEMA beslut      IS 'Beslutstjänsten: Skolinspektionens ärenden och deras dokument.';
COMMENT ON SCHEMA skolenkaten IS 'Skolenkäten som egen informationsprodukt.';


-- ── Proveniens ────────────────────────────────────────────────────────────
-- En rad per körning av ett insamlingsverktyg. Allt importerat pekar hit, så
-- att en felaktig körning kan spåras och vid behov rullas tillbaka.

CREATE TABLE core.import_runs (
    id            bigserial PRIMARY KEY,
    tool          text        NOT NULL,
    source_system text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text        NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','completed','failed','interrupted')),
    params        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    stats         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    notes         text
);

COMMENT ON TABLE  core.import_runs IS 'En rad per insamlingskörning.';
COMMENT ON COLUMN core.import_runs.tool IS
    'catalog_crawl | fetch_pdfs | diarium_crawl | build_index | sweep_docids';
COMMENT ON COLUMN core.import_runs.source_system IS
    'SIRIS | SI_CATALOG_API | SI_DIARIUM';


-- ── Geografi ──────────────────────────────────────────────────────────────

CREATE TABLE core.municipalities (
    code        char(4) PRIMARY KEY,
    name        text NOT NULL,
    county_code char(2) GENERATED ALWAYS AS (substring(code from 1 for 2)) STORED
);

COMMENT ON TABLE  core.municipalities IS 'Kommuner. code = kommunkod, t.ex. 0180.';
COMMENT ON COLUMN core.municipalities.county_code IS
    'Länskod, de två första siffrorna i kommunkoden.';


-- ── Huvudmän ──────────────────────────────────────────────────────────────
-- Historiska huvudmän behålls (is_current = false). Det är genom dem som
-- nedlagda skolenheters beslut går att nå.

CREATE TABLE core.organizations (
    id                bigserial PRIMARY KEY,
    code              text NOT NULL UNIQUE,
    name              text NOT NULL,
    legal_form        text,
    is_current        boolean NOT NULL DEFAULT true,
    municipality_code char(4) REFERENCES core.municipalities(code),
    import_run_id     bigint REFERENCES core.import_runs(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  core.organizations IS 'Huvudmän, aktuella och historiska.';
COMMENT ON COLUMN core.organizations.code IS 'Organisationsnummer.';
COMMENT ON COLUMN core.organizations.is_current IS
    'false för huvudmän som upphört. Deras dokument finns kvar.';


-- ── Skolenheter ───────────────────────────────────────────────────────────
-- contact och statistics ligger i jsonb: de kommer från Skolverkets API i en
-- form som ändras över tid och hör inte till kärnmodellen. Det som söks på är
-- normaliserat; resten är bevarad rådata.

CREATE TABLE core.schools (
    id                bigserial PRIMARY KEY,
    code              text NOT NULL UNIQUE,
    name              text NOT NULL,
    organization_id   bigint REFERENCES core.organizations(id),
    municipality_code char(4) REFERENCES core.municipalities(code),
    school_forms      text[] NOT NULL DEFAULT '{}',
    is_current        boolean NOT NULL DEFAULT true,
    status            text,
    contact           jsonb NOT NULL DEFAULT '{}'::jsonb,
    statistics        jsonb NOT NULL DEFAULT '{}'::jsonb,
    import_run_id     bigint REFERENCES core.import_runs(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  core.schools IS 'Skolenheter enligt Skolverkets skolenhetskod.';
COMMENT ON COLUMN core.schools.code IS 'Skolenhetskod, åtta siffror.';

COMMIT;
