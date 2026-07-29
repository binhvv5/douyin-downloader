-- Migration v6 — index to support the Upload Management page (GET /uploads)
-- Ref: channel_admin/repositories/upload_management.py, README.md "Upload Management"
-- Usage: mysql -u <user> -p <your_database> < docs/pipeline-migrate-upload-management-indexes-v6.sql
--   (or: mysql -u <user> -p --database=<your_database> < ...)
--
-- Does NOT hard-code `USE douyin_downloader` — operates on whatever database
-- the calling session already selected (same rationale as
-- pipeline-migrate-upload-daily-quota-v5.sql).
--
-- Why this index is needed:
--   The Upload Management list defaults to `ORDER BY created_at DESC, id DESC`
--   with no filters applied (the very first page load). upload_records
--   already has indexes on `status` and `platform`
--   (docs/pipeline-migrate-existing-v1.sql), which help narrow a FILTERED
--   query, but neither helps the sort/pagination itself: MySQL cannot use a
--   single-column index on `status`/`platform` to satisfy an ORDER BY on
--   `created_at`, so the unfiltered (or loosely-filtered) case would require
--   a filesort across the whole table on every page load — increasingly
--   expensive as upload_records grows. `id` is an AUTO_INCREMENT primary key
--   assigned at the same INSERT that sets `created_at = NOW()` (upload_records
--   rows are never re-created, only updated in place — see
--   repositories/uploads.py), so (created_at, id) is always consistent with
--   insertion order and safe to use as a single composite sort key.
--
--   Every OTHER filter combination (status, platform, account_id, and the
--   channel/search filters that join through `aweme`) still benefits from
--   the EXISTING idx_upload_records_status / idx_upload_records_platform /
--   the implicit FK index on account_id / aweme's own indexes to narrow rows
--   first; a filesort over that (typically much smaller) filtered result set
--   is an acceptable tradeoff against adding several more composite indexes
--   and their write-amplification cost on a table the Facebook/YouTube
--   upload workers write to continuously.
--
-- Descending composite index — requires MySQL 8.0+ (already required
-- elsewhere in this project, e.g. JSON columns / CHECK constraints).
-- Guarded via information_schema so re-running this file is a no-op.
SET @index_exists = (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'upload_records'
    AND INDEX_NAME = 'idx_upload_records_created_at_id'
);
SET @ddl = IF(
  @index_exists = 0,
  'ALTER TABLE upload_records ADD INDEX idx_upload_records_created_at_id (created_at DESC, id DESC)',
  'SELECT 1'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
