-- 0006_views.sql — läsvyer för API-lagret
--
-- Vyerna gör två saker som annars måste upprepas i varje endpoint:
--   1. döljer opublicerat material
--   2. filtrerar bort 'mentioned'-kopplingar, som är korsreferenser och inte
--      säger att dokumentet tillhör ärendet
--
-- API:et kan läsa direkt från vyerna. Den som vill åt korsreferenser eller
-- opublicerat går mot bastabellerna.

BEGIN;

CREATE SCHEMA IF NOT EXISTS api;
COMMENT ON SCHEMA api IS 'Läsvyer för det publika API:et.';


-- ── Dokument med aktuell fil ──────────────────────────────────────────────

CREATE OR REPLACE VIEW api.documents AS
SELECT
    d.document_key,
    d.product,
    d.document_type,
    d.title,
    d.document_year,
    d.document_date,
    d.review_area,
    d.publication_status,
    d.published_at,
    v.storage_key,
    v.file_name,
    v.mime_type,
    v.file_kind,
    v.file_size,
    v.sha256,
    v.version_no,
    s.code  AS school_code,
    s.name  AS school_name,
    o.code  AS organization_code,
    o.name  AS organization_name,
    m.code  AS municipality_code,
    m.name  AS municipality_name,
    d.source_system,
    d.source_url
FROM core.documents d
JOIN core.document_versions v ON v.id = d.current_version_id
LEFT JOIN core.schools       s ON s.id = d.school_id
LEFT JOIN core.organizations o ON o.id = d.organization_id
LEFT JOIN core.municipalities m ON m.code = d.municipality_code
WHERE d.publication_status = 'published';

COMMENT ON VIEW api.documents IS
    'Publicerade dokument med sin aktuella filversion.';


-- ── Ärenden med dokumenträkning ───────────────────────────────────────────

CREATE OR REPLACE VIEW api.cases AS
SELECT
    c.diarienummer,
    c.case_year,
    c.case_no,
    c.subject,
    c.case_type,
    c.status,
    c.department,
    c.municipality_code,
    c.municipality_name,
    c.registered_date,
    c.closed_date,
    c.diary_series,
    COALESCE(dc.document_count, 0) AS document_count
FROM beslut.cases c
LEFT JOIN (
    SELECT cd.case_id, count(*) AS document_count
    FROM beslut.case_documents cd
    JOIN core.documents d ON d.id = cd.document_id
    WHERE cd.link_type = 'own_dnr'
      AND d.publication_status = 'published'
    GROUP BY cd.case_id
) dc ON dc.case_id = c.id;

COMMENT ON VIEW api.cases IS
    'Ärenden med antal publicerade dokument som tillhör dem (link_type = own_dnr).';


-- ── Kopplingen: bara dokument som faktiskt tillhör ärendet ────────────────

CREATE OR REPLACE VIEW api.case_documents AS
SELECT
    c.diarienummer,
    d.document_key,
    d.document_type,
    d.title,
    d.document_year,
    d.document_date,
    v.storage_key,
    v.file_name,
    v.mime_type,
    v.file_size,
    v.sha256,
    cd.link_confidence
FROM beslut.case_documents cd
JOIN beslut.cases       c ON c.id = cd.case_id
JOIN core.documents     d ON d.id = cd.document_id
JOIN core.document_versions v ON v.id = d.current_version_id
WHERE cd.link_type = 'own_dnr'
  AND d.publication_status = 'published';

COMMENT ON VIEW api.case_documents IS
    'Dokument som tillhör ett ärende. Korsreferenser (mentioned) ingår inte.';


-- ── Korsreferenser, separat ───────────────────────────────────────────────

CREATE OR REPLACE VIEW api.case_document_references AS
SELECT
    c.diarienummer,
    d.document_key,
    d.title,
    cd.link_confidence
FROM beslut.case_documents cd
JOIN beslut.cases       c ON c.id = cd.case_id
JOIN core.documents     d ON d.id = cd.document_id
WHERE cd.link_type = 'mentioned'
  AND d.publication_status = 'published';

COMMENT ON VIEW api.case_document_references IS
    'Dokument som hänvisar till ett ärende utan att tillhöra det.';


-- ── Skolenkäten ───────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW api.survey_reports AS
SELECT
    d.document_key,
    r.report_level,
    r.respondent_group,
    r.school_form,
    r.grade,
    r.term,
    r.year,
    s.code AS school_code,
    s.name AS school_name,
    o.name AS organization_name,
    m.code AS municipality_code,
    m.name AS municipality_name,
    d.title,
    v.storage_key,
    v.file_size,
    v.sha256
FROM skolenkaten.survey_reports r
JOIN core.documents         d ON d.id = r.document_id
JOIN core.document_versions v ON v.id = d.current_version_id
LEFT JOIN core.schools       s ON s.id = r.school_id
LEFT JOIN core.organizations o ON o.id = r.organization_id
LEFT JOIN core.municipalities m ON m.code = r.municipality_code
WHERE d.publication_status = 'published';

COMMIT;
