-- 0004_skolenkaten.sql — Skolenkäten som egen informationsprodukt
--
-- Rapporterna ligger som vanliga rader i core.documents med product =
-- 'skolenkaten'. Endast den produktspecifika metadatan får ett eget schema.
-- Därför kan Skolenkäten ges egna endpoints och egen behörighet utan att
-- beslutsdatan berörs.
--
-- Skolenkäten har ingen koppling till beslut.cases: rapporterna saknar
-- diarienummer i källan och hör inte till ett ärende.

BEGIN;

CREATE TABLE skolenkaten.survey_reports (
    id                bigserial PRIMARY KEY,
    document_id       bigint NOT NULL UNIQUE
                      REFERENCES core.documents(id) ON DELETE CASCADE,

    report_level      text CHECK (report_level IN ('skolenhet','huvudman')),
    respondent_group  text,   -- elev | vardnadshavare | pedagogisk_personal
    school_form       text,   -- grundskola | forskoleklass | gymnasieskola ...
    grade             text,   -- 'åk 5', 'åk 9'
    term              text,   -- 'VT25', 'HT14'
    year              smallint,

    school_id         bigint REFERENCES core.schools(id),
    organization_id   bigint REFERENCES core.organizations(id),
    municipality_code char(4) REFERENCES core.municipalities(code),

    import_run_id     bigint REFERENCES core.import_runs(id),
    created_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE skolenkaten.survey_reports IS
    'Skolenkätens rapporter. Fälten härleds ur rapporttiteln, som är strukturerad: '
    '"Grundskolan, Elevenkäten, åk 5, Södertälje, Lina grundskola, VT18".';
COMMENT ON COLUMN skolenkaten.survey_reports.report_level IS
    'Rapporten avser en skolenhet eller en huvudman.';

COMMIT;
