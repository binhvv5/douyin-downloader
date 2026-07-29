-- Migration: Facebook token sources — persists the Facebook User Access
-- Token(s) used to discover/sync many Facebook Pages in one action, without
-- creating a new Page/account model.
-- Ref: docs/facebook-page-management-v1.sql (the Page-credential schema this
-- migration attaches to), channel_admin/services/facebook_pages.py.
-- Usage: mysql -u <user> -p <your_database> < docs/facebook-token-sources-v1.sql
--   (or: mysql -u <user> -p --database=<your_database> < ...)
--
-- Does NOT hard-code `USE douyin_downloader` — operates on whatever database
-- the calling session already selected (same convention as this repo's other
-- docs/pipeline-migrate-*.sql / docs/facebook-page-management-v1.sql files).
--
-- One token source (one stored Facebook User Access Token) can manage many
-- Facebook Pages; multiple token sources may exist, each managing a
-- different group of Pages. This migration does NOT create a new Page/
-- account model: a Facebook Page stays exactly what
-- docs/facebook-page-management-v1.sql already established — one
-- `tbl_social_account_token` row (platform=Facebook) paired with one
-- `upload_accounts` row. This migration only adds a nullable "which token
-- source discovered/synced this Page, if any" relationship on top of that
-- existing row.
--
-- Idempotent and safe to re-run:
--   - `CREATE TABLE IF NOT EXISTS facebook_token_sources` is naturally
--     idempotent.
--   - The `tbl_social_account_token.token_source_id` column, its index, and
--     its foreign key are each added only if not already present, via an
--     INFORMATION_SCHEMA check + dynamic SQL (the same technique
--     docs/pipeline-migrate-channel-pipeline-config-v4.sql Step 4 uses for
--     aweme.voice_id) — real MySQL 8.0 has no `ADD COLUMN IF NOT EXISTS`
--     (that is a MariaDB-only extension; see facebook-page-management-v1.sql
--     for where this was first discovered the hard way).
--
-- Audited 2026-07-26: `tbl_social_account_token` is ENGINE=InnoDB (confirmed
-- via SHOW CREATE TABLE), so the new foreign key is real (not silently
-- dropped, as would happen on a non-InnoDB table). `facebook_token_sources`
-- is created fresh and empty by this migration — nothing to migrate.
--
-- "Only one token source enabled at a time" is NOT enforced here — the task
-- driving this migration is explicit that this is a UI-only selection rule,
-- not a database constraint. Multiple token sources may be `enabled = 1`
-- simultaneously.

-- ── Step 1: facebook_token_sources ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS facebook_token_sources (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    name                 VARCHAR(255) NOT NULL,

    -- Plaintext by design — this is a local/internal system; see README.md
    -- "Token storage" for the project-wide rationale against encryption.
    -- Never selected by any list/read path except the one dedicated
    -- credential-fetch function that performs the actual Meta discover/sync
    -- call (mirrors tbl_social_account_token.access_token's own convention).
    user_access_token    TEXT NOT NULL,

    enabled              TINYINT(1) NOT NULL DEFAULT 1,

    -- Reflects whether the last discover/sync-all call against this source's
    -- Meta /me/accounts succeeded authentication-wise — same three values as
    -- tbl_social_account_token.verification_status (unverified/verified/failed).
    verification_status  VARCHAR(32) NOT NULL DEFAULT 'unverified',
    verification_error   TEXT DEFAULT NULL,
    last_verified_at     DATETIME DEFAULT NULL,

    -- Set only after a successful sync-all (discover alone never writes Page
    -- rows, so it does not advance this column).
    last_synced_at       DATETIME DEFAULT NULL,

    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    KEY idx_facebook_token_sources_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Step 2: tbl_social_account_token.token_source_id (nullable relationship) ─
-- NULL = this Page credential was added manually (services/facebook_pages.py
-- add_page/replace_page_token), not owned by any token source. ON DELETE SET
-- NULL: deleting a token source never deletes/breaks the Pages it synced —
-- they simply become "no known source" and keep publishing exactly as before.
SET @has_token_source_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_social_account_token'
      AND COLUMN_NAME = 'token_source_id'
);
SET @add_token_source_id_sql = CASE
    WHEN @has_token_source_id = 0
        THEN 'ALTER TABLE tbl_social_account_token ADD COLUMN token_source_id INT DEFAULT NULL COMMENT ''Facebook token source that discovered/synced this Page (NULL = manually added)'''
    ELSE 'SELECT 1'
END;
PREPARE add_token_source_id_stmt FROM @add_token_source_id_sql;
EXECUTE add_token_source_id_stmt;
DEALLOCATE PREPARE add_token_source_id_stmt;

SET @has_token_source_id_index = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_social_account_token'
      AND INDEX_NAME = 'idx_tbl_social_account_token_token_source_id'
);
SET @add_token_source_id_index_sql = CASE
    WHEN @has_token_source_id_index = 0
        THEN 'ALTER TABLE tbl_social_account_token ADD KEY idx_tbl_social_account_token_token_source_id (token_source_id)'
    ELSE 'SELECT 1'
END;
PREPARE add_token_source_id_index_stmt FROM @add_token_source_id_index_sql;
EXECUTE add_token_source_id_index_stmt;
DEALLOCATE PREPARE add_token_source_id_index_stmt;

SET @has_token_source_fk = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_social_account_token'
      AND CONSTRAINT_NAME = 'fk_tbl_social_account_token_token_source' AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
SET @add_token_source_fk_sql = CASE
    WHEN @has_token_source_fk = 0
        THEN 'ALTER TABLE tbl_social_account_token ADD CONSTRAINT fk_tbl_social_account_token_token_source FOREIGN KEY (token_source_id) REFERENCES facebook_token_sources(id) ON DELETE SET NULL ON UPDATE CASCADE'
    ELSE 'SELECT 1'
END;
PREPARE add_token_source_fk_stmt FROM @add_token_source_fk_sql;
EXECUTE add_token_source_fk_stmt;
DEALLOCATE PREPARE add_token_source_fk_stmt;

-- ── Step 3: tbl_social_account_token.tasks — Meta's raw per-Page "tasks"
-- list (JSON-encoded array of strings, e.g. '["CREATE_CONTENT","MANAGE"]'),
-- captured at discover/sync-all time for display and future permission
-- checks. NULL for every Page added before this column existed, or added
-- manually (services/facebook_pages.py never populates it — only the
-- token-source discover/sync-all path does, see
-- services/facebook_token_sources.py).
SET @has_tasks_column = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tbl_social_account_token'
      AND COLUMN_NAME = 'tasks'
);
SET @add_tasks_column_sql = CASE
    WHEN @has_tasks_column = 0
        THEN 'ALTER TABLE tbl_social_account_token ADD COLUMN tasks TEXT DEFAULT NULL COMMENT ''JSON-encoded array of Meta Page task permissions (e.g. CREATE_CONTENT) as of the last discover/sync'''
    ELSE 'SELECT 1'
END;
PREPARE add_tasks_column_stmt FROM @add_tasks_column_sql;
EXECUTE add_tasks_column_stmt;
DEALLOCATE PREPARE add_tasks_column_stmt;
