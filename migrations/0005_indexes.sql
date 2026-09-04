-- 0005_indexes.sql — sökvektorer och index
--
-- Körs efter laddning av data. Att skapa index efter COPY är väsentligt
-- snabbare än att ladda in i en indexerad tabell.
--
-- Textsökkonfigurationen 'swedish' ingår i PostgreSQL som standard.

BEGIN;

-- ── Ärenden ───────────────────────────────────────────────────────────────

ALTER TABLE beslut.cases ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('swedish', coalesce(diarienummer, '')),      'A') ||
        setweight(to_tsvector('swedish', coalesce(subject, '')),           'B') ||
        setweight(to_tsvector('swedish', coalesce(municipality_name, '')), 'C') ||
        setweight(to_tsvector('swedish', coalesce(case_type, '')),         'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS ix_cases_tsv       ON beslut.cases USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS ix_cases_regdate   ON beslut.cases (registered_date DESC);
CREATE INDEX IF NOT EXISTS ix_cases_closed    ON beslut.cases (closed_date DESC);
CREATE INDEX IF NOT EXISTS ix_cases_type      ON beslut.cases (case_type);
CREATE INDEX IF NOT EXISTS ix_cases_status    ON beslut.cases (status);
CREATE INDEX IF NOT EXISTS ix_cases_muni      ON beslut.cases (municipality_code);
CREATE INDEX IF NOT EXISTS ix_cases_year      ON beslut.cases (case_year);
CREATE INDEX IF NOT EXISTS ix_cases_dept      ON beslut.cases (department);

-- Prefixsökning på diarienummer, så att 'SI 2024:' fungerar.
CREATE INDEX IF NOT EXISTS ix_cases_dnr_prefix
    ON beslut.cases (diarienummer text_pattern_ops);


-- ── Dokument ──────────────────────────────────────────────────────────────

ALTER TABLE core.documents ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('swedish', coalesce(title, '')),         'A') ||
        setweight(to_tsvector('swedish', coalesce(document_type, '')), 'C') ||
        setweight(to_tsvector('swedish', coalesce(review_area, '')),   'C')
    ) STORED;

CREATE INDEX IF NOT EXISTS ix_documents_tsv     ON core.documents USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS ix_documents_product ON core.documents (product);
CREATE INDEX IF NOT EXISTS ix_documents_type    ON core.documents (document_type);
CREATE INDEX IF NOT EXISTS ix_documents_year    ON core.documents (document_year);
CREATE INDEX IF NOT EXISTS ix_documents_date    ON core.documents (document_date DESC);
CREATE INDEX IF NOT EXISTS ix_documents_school  ON core.documents (school_id);
CREATE INDEX IF NOT EXISTS ix_documents_org     ON core.documents (organization_id);
CREATE INDEX IF NOT EXISTS ix_documents_muni    ON core.documents (municipality_code);
CREATE INDEX IF NOT EXISTS ix_documents_pubstat ON core.documents (publication_status);

-- Den vanligaste publika frågan: publicerade beslut, nyast först.
CREATE INDEX IF NOT EXISTS ix_documents_published_beslut
    ON core.documents (document_year DESC, id)
    WHERE publication_status = 'published' AND product = 'beslut';


-- ── Filversioner ──────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS ix_versions_document ON core.document_versions (document_id);
CREATE INDEX IF NOT EXISTS ix_versions_sha256   ON core.document_versions (sha256);
CREATE INDEX IF NOT EXISTS ix_versions_status   ON core.document_versions (download_status);


-- ── Dokumenttext: fulltextsökning i innehållet ────────────────────────────
-- Tabellen kan vara tom vid lansering. Sökningen degraderar då till metadata
-- utan att API:et behöver ändras.

ALTER TABLE core.document_texts ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('swedish', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS ix_document_texts_tsv
    ON core.document_texts USING gin (content_tsv);


-- ── Koppling ──────────────────────────────────────────────────────────────
-- Primärnyckeln täcker uppslag från ärende. Detta index täcker det omvända.

CREATE INDEX IF NOT EXISTS ix_case_documents_doc
    ON beslut.case_documents (document_id, link_type);
CREATE INDEX IF NOT EXISTS ix_case_documents_own
    ON beslut.case_documents (case_id) WHERE link_type = 'own_dnr';


-- ── Organisationer och skolor ─────────────────────────────────────────────

ALTER TABLE core.organizations ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('swedish', coalesce(name, ''))) STORED;
ALTER TABLE core.schools ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('swedish', coalesce(name, ''))) STORED;

CREATE INDEX IF NOT EXISTS ix_orgs_tsv     ON core.organizations USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS ix_orgs_current ON core.organizations (is_current);
CREATE INDEX IF NOT EXISTS ix_schools_tsv  ON core.schools USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS ix_schools_org  ON core.schools (organization_id);
CREATE INDEX IF NOT EXISTS ix_schools_muni ON core.schools (municipality_code);


-- ── Skolenkäten ───────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS ix_survey_school ON skolenkaten.survey_reports (school_id);
CREATE INDEX IF NOT EXISTS ix_survey_org    ON skolenkaten.survey_reports (organization_id);
CREATE INDEX IF NOT EXISTS ix_survey_muni   ON skolenkaten.survey_reports (municipality_code);
CREATE INDEX IF NOT EXISTS ix_survey_year   ON skolenkaten.survey_reports (year DESC);
CREATE INDEX IF NOT EXISTS ix_survey_group  ON skolenkaten.survey_reports (respondent_group);

COMMIT;
