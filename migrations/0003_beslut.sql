-- 0003_beslut.sql — Beslutstjänsten: ärenden och kopplingen till dokument

BEGIN;

CREATE TABLE beslut.cases (
    id                bigserial PRIMARY KEY,

    -- Kanonisk form 'SI ÅÅÅÅ:NNNN'. Äldre ärenden visas i diarieportalen som
    -- '2014:610' utan prefix och normaliseras hit, så att sökning fungerar
    -- likadant oavsett årgång.
    diarienummer      text NOT NULL UNIQUE,
    case_year         smallint,
    case_no           int,

    -- Diariets egna referenser, bevarade för spårbarhet mot källan.
    diary_series      text,      -- '2' = 2008-10..2018-12, '6' = 2019-01..
    diary_caseref     int,

    subject           text,      -- ärendemening
    case_type         text,      -- Anmälan | Riktad tillsyn | Uppgift ...
    status            text,      -- Ad acta | pågående ...
    department        text,      -- handläggande avdelning

    -- Registrerat i diariet, till skillnad från dagens data.json där kommun
    -- gissas med regex ur ärendemeningens text.
    municipality_code char(4) REFERENCES core.municipalities(code),
    municipality_name text,

    registered_date   date,
    closed_date       date,

    source            text NOT NULL DEFAULT 'diarium'
                      CHECK (source IN ('diarium','data_json','manuell')),

    import_run_id     bigint REFERENCES core.import_runs(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  beslut.cases IS 'Ärenden ur Skolinspektionens diarium, 2008-10-13 och framåt.';
COMMENT ON COLUMN beslut.cases.diarienummer IS 'Kanonisk form SI ÅÅÅÅ:NNNN.';
COMMENT ON COLUMN beslut.cases.municipality_name IS
    'Kommun som registrerad i diariet, inte härledd ur ärendemeningen.';


-- ── Kopplingen ärende ↔ dokument ──────────────────────────────────────────
--
-- Många-till-många. Ett samlingsbeslut räknar upp flera ärenden, och ett
-- tillsynsärende får beslut plus uppföljningsbeslut.
--
-- link_type är inte kosmetisk. Beslut skriver ut andra ärendens diarienummer i
-- löptext: siris-618125 (Al-Azhar) och siris-618126 (Edinit) nämner varandras
-- nummer. Utan distinktionen kopplas båda besluten till båda ärendena.
-- Publika API:et filtrerar som standard på own_dnr.

CREATE TABLE beslut.case_documents (
    case_id         bigint NOT NULL REFERENCES beslut.cases(id) ON DELETE CASCADE,
    document_id     bigint NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,

    link_type       text NOT NULL
                    CHECK (link_type IN ('own_dnr','mentioned')),
    link_method     text NOT NULL DEFAULT 'dnr_ur_pdf_text'
                    CHECK (link_method IN ('dnr_ur_pdf_text','katalog','manuell')),
    link_confidence text NOT NULL
                    CHECK (link_confidence IN ('high','reference',
                                               'unmatched_case','reference_unmatched')),
    dnr_position    int,
    created_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (case_id, document_id, link_type)
);

COMMENT ON TABLE  beslut.case_documents IS 'Många-till-många mellan ärende och dokument.';
COMMENT ON COLUMN beslut.case_documents.link_type IS
    'own_dnr = dokumentet tillhör ärendet. mentioned = dokumentet hänvisar bara till det.';
COMMENT ON COLUMN beslut.case_documents.link_method IS
    'Hur kopplingen härleddes. dnr_ur_pdf_text = diarienummer läst ur dokumentets egen text.';

COMMIT;
