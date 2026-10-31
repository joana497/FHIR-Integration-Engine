-- ============================================================
-- 05_lisb_schema.sql - a second, differently structured laboratory
-- system ("LIS-B"), in its own database.
--
-- It exists to test hypothesis H6 (legacy diversity): the engine
-- reaches it through a separate adapter (adapters/lisb.py) without
-- changing the FHIR mappers. Deliberate differences from SIL:
--   * English names, surrogate integer keys, separate patient table
--   * timestamps instead of dates, priority/status as letter codes
--   * no examination header table: results hang off order lines
--   * typed result columns and a textual reference range ("13.0-17.0")
--   * its own local test/analyte codes and unit spellings
-- ============================================================
CREATE DATABASE lisbdb;
\connect lisbdb

CREATE SCHEMA lis;

CREATE TABLE lis.patient (
    patient_id   INTEGER      PRIMARY KEY,
    mrn          VARCHAR(20)  NOT NULL UNIQUE,
    national_id  VARCHAR(20),
    given_name   VARCHAR(80),
    family_name  VARCHAR(80),
    birth_date   DATE,
    sex          CHAR(1)      CHECK (sex IN ('M', 'F', 'U'))
);

CREATE TABLE lis.requisition (
    req_id        INTEGER      PRIMARY KEY,
    accession     VARCHAR(20)  NOT NULL UNIQUE,
    patient_id    INTEGER      NOT NULL REFERENCES lis.patient (patient_id),
    requested_at  TIMESTAMP    NOT NULL,
    collected_at  TIMESTAMP,
    priority      CHAR(1)      NOT NULL DEFAULT 'R' CHECK (priority IN ('R', 'A', 'S')),
    status        VARCHAR(2)   NOT NULL CHECK (status IN ('NW', 'IP', 'CM', 'CA')),
    requester     VARCHAR(80),
    department    VARCHAR(60),
    clinical_info TEXT
);

CREATE TABLE lis.req_test (
    req_id     INTEGER      NOT NULL REFERENCES lis.requisition (req_id),
    line_no    SMALLINT     NOT NULL,
    test_code  VARCHAR(12)  NOT NULL,
    test_name  VARCHAR(120),
    PRIMARY KEY (req_id, line_no)
);

CREATE TABLE lis.result (
    result_id      INTEGER       PRIMARY KEY,
    req_id         INTEGER       NOT NULL,
    line_no        SMALLINT      NOT NULL,
    analyte_code   VARCHAR(12)   NOT NULL,
    analyte_name   VARCHAR(120),
    value_type     VARCHAR(2)    NOT NULL CHECK (value_type IN ('NM', 'CE', 'ST')),
    num_value      NUMERIC(14,4),
    operator       VARCHAR(2),
    code_value     VARCHAR(20),
    text_value     VARCHAR(255),
    unit           VARCHAR(20),
    ref_range      VARCHAR(40),
    abnormal_flag  VARCHAR(3),
    result_status  CHAR(1)       NOT NULL DEFAULT 'F' CHECK (result_status IN ('F', 'P', 'C')),
    resulted_at    TIMESTAMP,
    validated_by   VARCHAR(80),
    FOREIGN KEY (req_id, line_no) REFERENCES lis.req_test (req_id, line_no)
);

CREATE INDEX ix_result_req ON lis.result (req_id, line_no);
