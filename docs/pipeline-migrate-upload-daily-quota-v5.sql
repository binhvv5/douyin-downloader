-- Migration v5 — upload_accounts.daily_quota default + non-negative guard
-- Ref: docs/SRS-douyin-download-pipeline.md (§6.2 upload_accounts),
--      docs/pipeline-upload-feature.md, channel_admin/services/quota.py
-- Usage: mysql -u <user> -p <your_database> < docs/pipeline-migrate-upload-daily-quota-v5.sql
--   (or: mysql -u <user> -p --database=<your_database> < ...)
--
-- Does NOT hard-code `USE douyin_downloader` — operates on whatever database
-- the calling session already selected (same rationale as
-- pipeline-migrate-channel-pipeline-config-v4.sql).
--
-- upload_accounts.daily_quota already exists (`INT NULL`, no column default —
-- see pipeline-migrate-existing-v1.sql / SRS §7 DDL). This migration:
--
--   1. Sets the column DEFAULT to 2, so a bare INSERT that omits daily_quota
--      (e.g. channel_admin/repositories/facebook_pages.py::insert_upload_account)
--      gets 2 automatically instead of NULL.
--   2. Backfills EVERY existing row to 2 — an explicit product decision:
--      existing accounts adopt the new default rather than silently keeping
--      "unlimited" (NULL). If an account genuinely needs unlimited uploads,
--      an operator must explicitly clear its quota afterward via the
--      Facebook Page edit form (leave the field blank).
--   3. Adds a CHECK constraint rejecting negative values (NULL/0/positive
--      integers all remain valid) — requires MySQL 8.0.16+; guarded via
--      information_schema so re-running this file, or running it on an
--      older server that already has an equivalent constraint, is a no-op
--      rather than an error.
--
-- Semantics enforced by application code (channel_admin/services/quota.py),
-- not by this migration:
--   NULL             -> unlimited uploads/day
--   0                -> uploads disabled for this account
--   positive integer -> max uploads per local calendar day (see
--                        UPLOAD_QUOTA_TIMEZONE / DEFAULT_QUOTA_TIMEZONE)
--
-- Idempotent and safe to re-run.

ALTER TABLE upload_accounts
  MODIFY COLUMN daily_quota INT NULL DEFAULT 2;

UPDATE upload_accounts SET daily_quota = 2;

-- Step 3: add the non-negative CHECK constraint only if it doesn't already
-- exist (re-running `ADD CONSTRAINT` with the same name errors otherwise).
SET @constraint_exists = (
  SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'upload_accounts'
    AND CONSTRAINT_NAME = 'chk_upload_accounts_daily_quota_nonneg'
);
SET @ddl = IF(
  @constraint_exists = 0,
  'ALTER TABLE upload_accounts ADD CONSTRAINT chk_upload_accounts_daily_quota_nonneg CHECK (daily_quota IS NULL OR daily_quota >= 0)',
  'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
