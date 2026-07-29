-- Verification v4.0 — post-migration schema and data checks for
-- docs/pipeline-migrate-channel-pipeline-config-v4.sql
-- Ref: docs/channel-pipeline-config-plan.md
-- Usage: mysql -u douyin -p <your_database> < docs/pipeline-verify-channel-pipeline-config-v4.sql
--
-- This file does NOT hard-code `USE douyin_downloader` (review item 2) -
-- it operates on whatever database the calling session already has
-- selected.
--
-- Run this immediately after the migration. Every query below has a
-- documented expectation in its trailing comment - a result that doesn't
-- match means the migration did not complete as intended (e.g. a partial
-- prior run left the table in an unexpected shape); stop and reconcile
-- before treating the migration as done, rather than discovering the gap
-- later via a data problem. `CREATE TABLE IF NOT EXISTS` alone does NOT
-- guarantee any of the below - it only guarantees the table exists with
-- *some* shape (the migration's own pre-backfill verification procedure
-- already checks most of this as a hard gate before backfill ever runs -
-- these queries are for post-hoc, human-readable confirmation, and for
-- catching anything that gate does not cover, e.g. value-level parity).
--
-- State-aware (review item 5): every query below is safe to run
-- unconditionally against a database in EITHER State A (legacy
-- channels.voice_id/logo_id present) or State B (clean install, those
-- columns never existed) - none of them raise "Unknown column" errors in
-- either state. Section 7 uses dynamic SQL, guarded by an
-- INFORMATION_SCHEMA existence check, specifically so it degrades to a
-- harmless no-result-set in State B rather than failing to parse.

-- ── 1. Columns, types, nullability, defaults ────────────────────────────────
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
ORDER BY ORDINAL_POSITION;
-- Expect exactly, in this order: id, channel_id, translation_enabled,
-- dubbing_enabled, voice_id, logo_id, logo_enabled, opening_hook_enabled,
-- daily_video_limit, created_at, updated_at - defaults matching
-- docs/pipeline-migrate-channel-pipeline-config-v4.sql Step 3.

-- ── 2. Indexes (PK, unique, plain) ──────────────────────────────────────────
SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE, SEQ_IN_INDEX
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
ORDER BY INDEX_NAME, SEQ_IN_INDEX;
-- Expect: PRIMARY (id), uq_channel_pipeline_configs_channel_id (channel_id,
-- NON_UNIQUE=0), idx_channel_pipeline_configs_voice_id,
-- idx_channel_pipeline_configs_logo_id.

-- ── 3. CHECK constraints present and attached to this table ─────────────────
SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
  ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
 AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA = DATABASE()
  AND tc.TABLE_NAME = 'channel_pipeline_configs';
-- Expect three rows: chk_channel_pipeline_configs_valid_combo,
-- chk_channel_pipeline_configs_boolean_values,
-- chk_channel_pipeline_configs_daily_limit.

-- ── 4. Foreign keys present, pointing at the right tables, right ON DELETE ──
SELECT
    kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME,
    kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME,
    rc.DELETE_RULE, rc.UPDATE_RULE
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
 AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = 'channel_pipeline_configs'
  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL;
-- Expect exactly 3 rows:
--   fk_channel_pipeline_configs_channel -> channels.id,  DELETE_RULE=CASCADE
--   fk_channel_pipeline_configs_voice   -> voices.id,    DELETE_RULE=SET NULL
--   fk_channel_pipeline_configs_logo    -> logos.id,     DELETE_RULE=SET NULL

-- ── 5. Engine/charset (matches project convention) ──────────────────────────
SELECT ENGINE, TABLE_COLLATION
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs';
-- Expect: InnoDB, utf8mb4_unicode_ci.

-- ── 6. aweme.voice_id nullability/default (review item 1) ──────────────────
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id';
-- Expect: IS_NULLABLE='YES', COLUMN_DEFAULT=NULL.

SELECT COUNT(*) AS aweme_rows_with_voice_id_1
FROM aweme
WHERE voice_id = 1;
-- Informational only (not a failure condition either way) - see
-- docs/pipeline-preflight-channel-pipeline-config-v4.sql for why these
-- rows are deliberately left unchanged by the migration.

-- ── 7. Value-parity spot check (State A only - dynamic SQL, guarded) ───────
-- Every backfilled row's voice_id/logo_id must match the (possibly
-- just-remediated) legacy channels columns exactly. In State B (clean
-- install, channels.voice_id/logo_id never existed), this section
-- produces no result set at all rather than an "Unknown column" error.
SET @has_legacy_voice_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'voice_id'
);
SET @has_legacy_logo_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'logo_id'
);

-- Built with CONCAT(), not `||` - MySQL's DEFAULT sql_mode does NOT enable
-- PIPES_AS_CONCAT, so `||` means logical OR, not string concatenation;
-- CONCAT() is correct regardless of sql_mode.
--
-- NULL-safe comparison throughout (`NOT (a <=> b)`, never `a <> b`): plain
-- `<>` evaluates to NULL (neither true nor false) whenever either side is
-- NULL, and a WHERE clause treats a NULL result as "exclude this row" -
-- meaning a real mismatch where the legacy value is non-NULL and the
-- migrated value has since become NULL (e.g. an operator deleted the
-- referenced voice, which channel_pipeline_configs.voice_id's real FK
-- ON DELETE SET NULL would produce) was PREVIOUSLY invisible to this
-- report entirely - not flagged as a mismatch, just silently absent from
-- the result set. `<=>` (NULL-safe equality) never returns NULL - it
-- returns 1 when both sides are NULL, 0 when exactly one side is NULL,
-- and behaves like `=` otherwise - so `NOT (a <=> b)` correctly reports
-- every case: (1, NULL) and (NULL, 1) as mismatches, (NULL, NULL) and
-- (1, 1) as matches, and (1, 2) as a mismatch.
SET @value_parity_sql = CASE
    WHEN @has_legacy_voice_id > 0 AND @has_legacy_logo_id > 0 THEN CONCAT(
        'SELECT c.id, c.voice_id AS legacy_voice_id, cpc.voice_id AS new_voice_id, ',
        '       c.logo_id AS legacy_logo_id, cpc.logo_id AS new_logo_id ',
        'FROM channels c JOIN channel_pipeline_configs cpc ON cpc.channel_id = c.id ',
        'WHERE NOT (c.voice_id <=> cpc.voice_id) OR NOT (c.logo_id <=> cpc.logo_id)'
    )
    WHEN @has_legacy_voice_id > 0 THEN CONCAT(
        'SELECT c.id, c.voice_id AS legacy_voice_id, cpc.voice_id AS new_voice_id ',
        'FROM channels c JOIN channel_pipeline_configs cpc ON cpc.channel_id = c.id ',
        'WHERE NOT (c.voice_id <=> cpc.voice_id)'
    )
    WHEN @has_legacy_logo_id > 0 THEN CONCAT(
        'SELECT c.id, c.logo_id AS legacy_logo_id, cpc.logo_id AS new_logo_id ',
        'FROM channels c JOIN channel_pipeline_configs cpc ON cpc.channel_id = c.id ',
        'WHERE NOT (c.logo_id <=> cpc.logo_id)'
    )
    ELSE 'SELECT 0 AS state_b_no_legacy_columns_to_compare LIMIT 0'
END;
PREPARE value_parity_stmt FROM @value_parity_sql;
EXECUTE value_parity_stmt;
DEALLOCATE PREPARE value_parity_stmt;
-- Expect zero rows in State A (or an empty/absent result in State B). A
-- non-empty result IS this query's failure report - each returned row
-- identifies one channel whose migrated voice_id/logo_id no longer
-- matches its legacy source value and must be investigated before
-- trusting the migration for that channel.

-- ── 8. Row-count parity ──────────────────────────────────────────────────────
SELECT
    (SELECT COUNT(*) FROM channels) AS channel_count,
    (SELECT COUNT(*) FROM channel_pipeline_configs) AS config_count;
-- Expect equal.

-- ── 9. Reconciliation: every channel has EXACTLY one config row ────────────
-- (review item 5/8 - a stronger check than row-count parity alone, since
-- row counts could coincidentally match even if some channel had zero
-- rows and another had two - impossible here given the UNIQUE constraint,
-- but this query proves it directly rather than relying on the constraint
-- never having been bypassed, e.g. via a direct schema edit).
SELECT c.id AS channel_id, c.name, COUNT(cpc.id) AS config_row_count
FROM channels c
LEFT JOIN channel_pipeline_configs cpc ON cpc.channel_id = c.id
GROUP BY c.id, c.name
HAVING COUNT(cpc.id) <> 1;
-- Expect zero rows. Any row here is a channel with zero or more-than-one
-- channel_pipeline_configs rows - a data-integrity problem to investigate
-- before relying on this table. Also serves as the "new channel missing a
-- config row" diagnostic referenced in docs/channel-pipeline-config.md
-- (review item 8) - a channel created after the last migration/backfill
-- run without a corresponding config-row insert will show up here with
-- config_row_count = 0.

-- ── 10. Reconciliation: no config references a missing channel/voice/logo ──
-- Belt-and-suspenders: the FK constraints (section 4) already make this
-- structurally impossible under normal operation, but this proves it
-- directly rather than trusting that FOREIGN_KEY_CHECKS was never
-- disabled around a manual data fix elsewhere.
SELECT cpc.id, cpc.channel_id, cpc.voice_id, cpc.logo_id
FROM channel_pipeline_configs cpc
LEFT JOIN channels c ON c.id = cpc.channel_id
LEFT JOIN voices v ON v.id = cpc.voice_id
LEFT JOIN logos l ON l.id = cpc.logo_id
WHERE c.id IS NULL
   OR (cpc.voice_id IS NOT NULL AND v.id IS NULL)
   OR (cpc.logo_id IS NOT NULL AND l.id IS NULL);
-- Expect zero rows.

-- ── 11. Every existing channel's effective combo is the zero-behavior-change default ─
SELECT COUNT(*) AS unexpected_non_default_rows FROM channel_pipeline_configs
WHERE NOT (translation_enabled = 1 AND dubbing_enabled = 1
           AND logo_enabled = 1 AND opening_hook_enabled = 0
           AND daily_video_limit IS NULL);
-- Expect 0 immediately after backfill (before any operator customizes a row).
