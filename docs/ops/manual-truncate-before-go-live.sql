-- =============================================================================
-- MANUAL OPERATIONS ONLY — DESTRUCTIVE
-- =============================================================================
--
-- Location: docs/ops/  (intentionally NOT under feature/migration docs)
--
-- THIS SCRIPT IS NOT:
--   - a migration
--   - a Facebook publishing prerequisite
--   - part of startup, deployment, CI, or tests
--   - referenced by any application or migration runner
--
-- THIS SCRIPT DELETES:
--   tokens, upload accounts, channel↔destination mappings, pipeline jobs,
--   upload history, channels, pipeline configs, aweme rows, and related
--   operational tables listed below.
--
-- WARNING: irreversible. Run only against a non-production or explicitly
-- approved target database AFTER a verified backup. Do not execute as part
-- of enabling Facebook publishing.
--
-- How to run (manual operator only):
--   1) mysql CLI (recommended):
--        mysql -h <host> -u <user> -p <database> < docs/ops/manual-truncate-before-go-live.sql
--   2) DBeaver / DataGrip / JetBrains:
--        Use "Execute SQL Script" (Alt+X in DBeaver), NOT "Execute SQL Statement"
--        (Ctrl+Enter). Ctrl+Enter sends multiple statements as ONE query and
--        causes: SQL Error [1064] near 'SET FOREIGN_KEY_CHECKS = 0'.
--        Or enable allowMultiQueries=true on the JDBC connection.
--
-- Tables truncated:
--   aweme
--   channel_pipeline_configs
--   channels
--   download_history
--   job
--   pipeline_jobs
--   tbl_channel_social_account
--   tbl_social_account_token
--   transcript_job
--   upload_accounts
--   upload_records
--   video_assets
--
-- Tables preserved:
--   voices
--   logos
-- =============================================================================

SET FOREIGN_KEY_CHECKS = 0;

TRUNCATE TABLE aweme;
TRUNCATE TABLE channel_pipeline_configs;
TRUNCATE TABLE channels;
TRUNCATE TABLE download_history;
TRUNCATE TABLE job;
TRUNCATE TABLE pipeline_jobs;
TRUNCATE TABLE tbl_channel_social_account;
TRUNCATE TABLE tbl_social_account_token;
TRUNCATE TABLE transcript_job;
TRUNCATE TABLE upload_accounts;
TRUNCATE TABLE upload_records;
TRUNCATE TABLE video_assets;

SET FOREIGN_KEY_CHECKS = 1;

-- Sanity check: preserved catalogs should still have rows; truncated tables should be empty.
SELECT TABLE_NAME, TABLE_ROWS
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME;
