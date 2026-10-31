-- ============================================================
-- Terminology validation cache
--
-- One row per (system, code) pair seen by the engine. Acts as both
-- a cache for the external terminology server (avoid revalidating
-- the same code on every sync) and as the source of truth for the
-- validation-metrics dashboard.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit.terminology_validation (
    validation_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    code_system       VARCHAR(255) NOT NULL,   -- e.g. http://loinc.org
    code              VARCHAR(64)  NOT NULL,   -- e.g. 24629-1
    display_local     VARCHAR(500),            -- display name we sent
    is_valid          BOOLEAN      NOT NULL,   -- result from terminology server
    display_canonical VARCHAR(500),            -- display name server returned
    message           TEXT,                    -- server message (e.g. "not a valid LOINC code")
    server_url        VARCHAR(255),            -- which terminology server we asked
    validated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    revalidate_after  TIMESTAMPTZ  NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
    times_seen        INTEGER      NOT NULL DEFAULT 1,
    last_seen_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (code_system, code)
);

CREATE INDEX IF NOT EXISTS ix_term_validation_system   ON audit.terminology_validation(code_system);
CREATE INDEX IF NOT EXISTS ix_term_validation_valid    ON audit.terminology_validation(is_valid);
CREATE INDEX IF NOT EXISTS ix_term_validation_revalid  ON audit.terminology_validation(revalidate_after);

-- Helper view: per-system summary for the metrics dashboard
CREATE OR REPLACE VIEW audit.terminology_summary AS
SELECT
    code_system,
    COUNT(*)                              AS total_codes,
    SUM(CASE WHEN is_valid THEN 1 ELSE 0 END) AS valid_codes,
    SUM(CASE WHEN is_valid THEN 0 ELSE 1 END) AS invalid_codes,
    ROUND(100.0 * SUM(CASE WHEN is_valid THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)
                                          AS resolution_rate_pct,
    SUM(times_seen)                       AS total_validations,
    MAX(last_seen_at)                     AS last_seen
FROM audit.terminology_validation
GROUP BY code_system;
