-- ============================================================
-- 07_roles.sql - least-privilege database roles.
--
--   sil_reader    python-fhir-engine -> sildb, schema sil:   SELECT only
--   lisb_reader   python-fhir-engine -> lisbdb, schema lis:  SELECT only
--   audit_writer  node-middleware    -> sildb, schema audit:
--                   exchange_log, security_event: SELECT, INSERT (append-only)
--                   terminology_validation:       SELECT, INSERT, UPDATE (a cache)
--                   purge_old_payloads():         EXECUTE
--
-- sil_user (POSTGRES_USER) remains the owner and is used only to create
-- and seed the databases. Requirement R1 (the engine never writes to the
-- legacy system) is enforced here: an INSERT, UPDATE or DELETE issued by
-- the engine fails with "permission denied". The read-only roles also
-- default to read-only transactions as a second guard.
--
-- Passwords are demonstration values; set real ones before any deployment.
-- ============================================================
CREATE ROLE sil_reader   LOGIN PASSWORD 'sil_reader_pass';
CREATE ROLE lisb_reader  LOGIN PASSWORD 'lisb_reader_pass';
CREATE ROLE audit_writer LOGIN PASSWORD 'audit_writer_pass';

ALTER ROLE sil_reader  SET default_transaction_read_only = on;
ALTER ROLE lisb_reader SET default_transaction_read_only = on;

-- ── sildb ────────────────────────────────────────────────────────────────
\connect sildb
REVOKE ALL ON DATABASE sildb FROM PUBLIC;
GRANT CONNECT ON DATABASE sildb TO sil_reader, audit_writer;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE  ON SCHEMA sil TO sil_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sil TO sil_reader;

GRANT USAGE ON SCHEMA audit TO audit_writer;
GRANT SELECT, INSERT         ON audit.exchange_log, audit.security_event TO audit_writer;
GRANT SELECT, INSERT, UPDATE ON audit.terminology_validation TO audit_writer;
GRANT SELECT ON audit.exchange_summary, audit.terminology_summary TO audit_writer;
REVOKE EXECUTE ON FUNCTION audit.purge_old_payloads() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION audit.purge_old_payloads() TO audit_writer;

-- ── lisbdb ───────────────────────────────────────────────────────────────
\connect lisbdb
REVOKE ALL ON DATABASE lisbdb FROM PUBLIC;
GRANT CONNECT ON DATABASE lisbdb TO lisb_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE  ON SCHEMA lis TO lisb_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA lis TO lisb_reader;
