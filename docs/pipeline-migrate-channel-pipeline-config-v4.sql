-- Migration v4.0 — channel_pipeline_configs (consolidated with voices/logos
-- DDL) + aweme.voice_id restoration/normalization
-- Ref: docs/channel-pipeline-config-plan.md, docs/SRS-douyin-download-pipeline.md
-- Usage: mysql -u douyin -p <your_database> < docs/pipeline-migrate-channel-pipeline-config-v4.sql
--   (or: mysql -u douyin -p --database=<your_database> < ...)
--
-- IMPORTANT (review item 2): this file does NOT hard-code `USE
-- douyin_downloader`. It operates entirely on whatever database the calling
-- session/connection has already selected (via `mysql ... <dbname> < file`,
-- `--database=`, or a prior `USE` statement, or a client library's own
-- `database=` connection parameter, e.g. PyMySQL's `pymysql.connect(...,
-- database=...)`). This is deliberate: a hard-coded `USE douyin_downloader`
-- previously made it possible for an integration-test run (which may target
-- a differently-named disposable test database) to silently execute this
-- file's DDL/DML against the real `douyin_downloader` database instead of
-- the intended test database. See dub_worker/tests/test_db_pipeline_config_integration.py
-- for the test-side safety checks that complement this.
--
-- Consolidates and supersedes docs/pipeline-migrate-voice-config-v2.sql and
-- docs/pipeline-migrate-channel-logo-v3.sql — both files are DELETED as part
-- of this change (see docs/channel-pipeline-config-plan.md's "Migration-file
-- policy"). This file takes over their still-required `voices`/`logos`
-- catalog DDL, plus `aweme.voice_id` (formerly also created by the deleted
-- v2 file), so a clean install (after v1) does not lose any of that schema.
-- docs/voices-seed-data.sql and docs/logos-seed-data.sql are UNCHANGED and
-- still apply against the tables created here (same names/columns).
--
-- ─────────────────────────────────────────────────────────────────────────
-- REQUIRED BEFORE RUNNING THIS ON AN EXISTING DATABASE (State A - one that
-- already ran the former v2/v3 migrations, i.e. channels.voice_id/logo_id
-- and aweme.voice_id exist):
--
--   Run docs/pipeline-preflight-channel-pipeline-config-v4.sql first and
--   inspect its diagnostic queries. This migration ALSO independently
--   re-checks for orphan channels.voice_id/channels.logo_id itself (see
--   Step 6 below) and REFUSES to backfill if any are found - the standalone
--   preflight script is a diagnostic aid, not the only safety net. There is
--   no "exclude the orphaned channel and continue" mode: an orphan blocks
--   the ENTIRE migration until remediated (see docs/pipeline-preflight-
--   channel-pipeline-config-v4.sql for the remediation options).
--
-- After running, run docs/pipeline-verify-channel-pipeline-config-v4.sql
-- and confirm every query matches its documented expectation.
-- ─────────────────────────────────────────────────────────────────────────
--
-- Supports two starting states, detected automatically at each step:
--   State A — existing database that already ran the former v2/v3:
--     channels.voice_id/channels.logo_id, aweme.voice_id, and the
--     voices/logos tables already exist. This file creates
--     channel_pipeline_configs and migrates both channel-level selections
--     into it verbatim (without changing any channel's effective resolved
--     voice/logo), and normalizes aweme.voice_id to nullable/DEFAULT NULL
--     WITHOUT changing any existing row's value (see Step 4).
--   State B — clean database bootstrapped from v1 only (the former v2/v3
--     files no longer exist to apply): this file creates voices/logos
--     fresh, creates aweme.voice_id fresh (nullable, DEFAULT NULL), then
--     creates channel_pipeline_configs with voice_id/logo_id left NULL for
--     every channel — nothing to migrate; NULL resolves to the
--     system-default voice / no logo, identical to what a brand-new
--     channel would resolve to under either state.
--
-- This migration does NOT drop channels.voice_id/channels.logo_id. Per
-- docs/channel-pipeline-config-plan.md §4 Phase 4 (D13), that is deferred
-- to a later, separate forward migration once dub_worker's runtime
-- dual-read adoption has been verified against a real production database.
--
-- Idempotent and safe to re-run: every CREATE TABLE uses IF NOT EXISTS, the
-- aweme.voice_id step is a no-op once already nullable/DEFAULT NULL, and
-- the backfill only inserts rows for channels that don't already have one.
-- Re-running after a channel was added since the last run only backfills
-- the new channel(s).
--
-- ─────────────────────────────────────────────────────────────────────────
-- REQUIRED EXECUTION ORDER (review item 4) - enforced by this file's own
-- statement order, not just documented:
--   1. Create/alter required schema (Steps 1-4 below).
--   2. Validate the COMPLETE resulting schema (Step 6, via a temporary
--      stored procedure using SIGNAL SQLSTATE - see Step 5's comment for
--      why ordinary conditional SQL cannot do this).
--   3. Validate source data - orphan channels.voice_id/logo_id references
--      (also inside Step 6's procedure).
--   4. Backfill channel_pipeline_configs (Step 7).
--   5. The temporary procedure is dropped immediately after use (Step 8);
--      run docs/pipeline-verify-channel-pipeline-config-v4.sql separately
--      afterward for the full post-backfill reconciliation report.
-- If Step 6 SIGNALs (schema incomplete/incompatible, or an orphan
-- reference exists), the `mysql` CLI client aborts the rest of this script
-- by default (no --force flag is used) - Step 7's backfill and Step 8's
-- cleanup never run. The temporary procedure created in Step 5 is then
-- left behind; the next successful run's own `DROP PROCEDURE IF EXISTS` at
-- the start of Step 5 cleans it up automatically - no manual cleanup step
-- is required, but nothing is silently swallowed either: the failure is
-- visible in the client's own error output.

-- ── Step 1: voices (created only if this is a clean v1-only database) ──────
-- Identical definition to the former docs/pipeline-migrate-voice-config-v2.sql.
CREATE TABLE IF NOT EXISTS voices (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    filename        VARCHAR(255) NOT NULL,
    target_wps      DECIMAL(4,2) NOT NULL DEFAULT 4.30,
    min_wps         DECIMAL(4,2) NOT NULL DEFAULT 3.90,
    max_wps         DECIMAL(4,2) NOT NULL DEFAULT 4.70,
    speed           DECIMAL(4,2) NOT NULL DEFAULT 1.00,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    is_default      TINYINT(1) NOT NULL DEFAULT 0,
    description     TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_voices_name (name),
    KEY idx_voices_enabled (enabled),
    CONSTRAINT chk_voices_wps_positive
        CHECK (target_wps > 0 AND min_wps > 0 AND max_wps > 0),
    CONSTRAINT chk_voices_wps_order
        CHECK (min_wps <= target_wps AND target_wps <= max_wps),
    CONSTRAINT chk_voices_speed_range
        CHECK (speed >= 0.70 AND speed <= 1.30)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Step 2: logos (created only if this is a clean v1-only database) ───────
-- Identical definition to the former docs/pipeline-migrate-channel-logo-v3.sql.
CREATE TABLE IF NOT EXISTS logos (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    filename        VARCHAR(512) NOT NULL,
    size_px         INT NULL,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    description     TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_logos_name (name),
    UNIQUE KEY uq_logos_filename (filename),
    KEY idx_logos_enabled (enabled),
    CONSTRAINT chk_logos_size_px_positive
        CHECK (size_px IS NULL OR size_px > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Step 3: channel_pipeline_configs — the new per-channel config table ────
-- One row per channel (1:1, enforced by uq_channel_pipeline_configs_channel_id).
-- See docs/channel-pipeline-config-plan.md §2 for the full column-by-column
-- rationale.
CREATE TABLE IF NOT EXISTS channel_pipeline_configs (
    id                     INT AUTO_INCREMENT PRIMARY KEY,
    channel_id             INT NOT NULL,

    -- Processing toggles. CHECK below rejects translation_enabled=0 AND
    -- dubbing_enabled=1 (invalid: cannot dub without translating first).
    translation_enabled    TINYINT(1) NOT NULL DEFAULT 1,
    dubbing_enabled        TINYINT(1) NOT NULL DEFAULT 1,

    -- Channel-level selection, moved from channels.voice_id/channels.logo_id.
    -- Nullable + real FK here (unlike the legacy channels.voice_id, which is
    -- NOT NULL DEFAULT 1 with no FK, "by request") — safe because
    -- dub_worker/voice_resolver.py's system-default-voice fallback already
    -- guarantees something resolves even with no channel-level override.
    voice_id               INT NULL,
    logo_id                INT NULL,

    -- New capability: an independent kill-switch, distinct from "is logo_id
    -- set". Default 1 is an inert no-op for every existing channel, since
    -- today "has a logo" already IS "logo_id is set" (no separate toggle
    -- exists yet).
    logo_enabled           TINYINT(1) NOT NULL DEFAULT 1,

    -- Opening-hook overlay is OFF everywhere today (render_modules/
    -- opening_hook_intro.py: "OFF by default", no per-channel exceptions),
    -- so default 0 preserves that exactly for every existing channel.
    opening_hook_enabled   TINYINT(1) NOT NULL DEFAULT 0,

    -- NULL = unlimited (matches today's total absence of any per-channel
    -- daily limit). See docs/channel-pipeline-config-plan.md §6 for
    -- semantics still requiring approval before this field is
    -- read/enforced anywhere - the column itself is schema-only for now
    -- (Phase 3, not implemented).
    daily_video_limit      INT NULL,

    created_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_channel_pipeline_configs_channel_id (channel_id),
    KEY idx_channel_pipeline_configs_voice_id (voice_id),
    KEY idx_channel_pipeline_configs_logo_id (logo_id),

    -- translation_enabled=0 AND dubbing_enabled=1 is invalid (combo 4 in the
    -- plan) - cannot dub without translating. Enforced here AND in
    -- application code (dub_worker/pipeline_config.py::
    -- validate_pipeline_config_combo) since CHECK enforcement is
    -- MySQL-version-dependent (this project already relies on 8.0.16+).
    CONSTRAINT chk_channel_pipeline_configs_valid_combo
        CHECK (translation_enabled = 1 OR dubbing_enabled = 0),

    -- TINYINT(1)'s "(1)" is only a display-width hint - MySQL does not
    -- restrict the column to {0,1} on its own. These CHECKs close that gap
    -- explicitly for all four boolean-flag columns.
    CONSTRAINT chk_channel_pipeline_configs_boolean_values
        CHECK (
            translation_enabled IN (0, 1)
            AND dubbing_enabled IN (0, 1)
            AND logo_enabled IN (0, 1)
            AND opening_hook_enabled IN (0, 1)
        ),

    CONSTRAINT chk_channel_pipeline_configs_daily_limit
        CHECK (daily_video_limit IS NULL OR daily_video_limit >= 0),

    -- Owned/composed 1:1 entity of channels - CASCADE (unlike voices/logos'
    -- SET NULL below, which are shared, independently-referenced catalogs).
    CONSTRAINT fk_channel_pipeline_configs_channel
        FOREIGN KEY (channel_id) REFERENCES channels(id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT fk_channel_pipeline_configs_voice
        FOREIGN KEY (voice_id) REFERENCES voices(id)
        ON DELETE SET NULL ON UPDATE CASCADE,

    CONSTRAINT fk_channel_pipeline_configs_logo
        FOREIGN KEY (logo_id) REFERENCES logos(id)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── Step 4: aweme.voice_id — restore, nullable, DEFAULT NULL (review item 1) ─
-- Semantics (see dub_worker/voice_resolver.py and
-- docs/channel-pipeline-config-plan.md for the full resolution-order
-- documentation):
--   aweme.voice_id IS NULL     -> no per-video override; fall through to
--                                 channel_pipeline_configs.voice_id, then
--                                 the system-default voice.
--   aweme.voice_id IS NOT NULL -> explicit per-video override (highest
--                                 priority in the resolution chain).
--
-- State A: the legacy column already exists as
-- `voice_id INT NOT NULL DEFAULT 1` (added by the now-deleted v2 migration,
-- no FK - "by request"). `MODIFY COLUMN` only changes the column's
-- definition (nullability/default) - it does NOT rewrite existing row
-- values, so every existing aweme row KEEPS its current voice_id value
-- (typically 1, from the old default) UNCHANGED. This is deliberate: the
-- database cannot distinguish "this row's voice_id=1 because nobody ever
-- set it" from "this row's voice_id=1 because an operator explicitly chose
-- voice id 1" - converting existing non-NULL values to NULL would silently
-- discard that ambiguous-but-real data. See the verification script's
-- report query for exactly how many rows still have voice_id=1 - those
-- rows are now interpreted as explicit per-video overrides (harmless: they
-- resolve to the same voice they always did) until an operator separately
-- reviews/remediates them if desired.
--
-- State B: the column does not exist at all (clean v1-only install) -
-- ADD COLUMN creates it fresh, nullable, DEFAULT NULL, so every future
-- INSERT that omits voice_id gets NULL automatically (never an implicit 1).
--
-- No foreign key is added here, consistent with this column's existing,
-- deliberate "by request" no-FK convention (see the former v2 migration's
-- own header) and because this migration must not risk failing on
-- pre-existing dangling values it cannot safely remediate on its own (an
-- aweme.voice_id orphan-diagnostic report query is included in
-- docs/pipeline-verify-channel-pipeline-config-v4.sql, informational only
-- - not a blocking gate, since - unlike channels.voice_id feeding into the
-- new FK-constrained channel_pipeline_configs.voice_id - aweme.voice_id is
-- never copied into any FK-constrained column by this migration). A plain
-- index is added for lookup/reporting query performance - indexes carry no
-- referential-integrity risk and are cheap to add/keep.
SET @has_aweme_voice_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id'
);
SET @aweme_voice_id_is_nullable = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id'
      AND IS_NULLABLE = 'YES'
);

SET @aweme_voice_id_sql = CASE
    WHEN @has_aweme_voice_id = 0
        THEN 'ALTER TABLE aweme ADD COLUMN voice_id INT NULL DEFAULT NULL AFTER channel_id'
    WHEN @aweme_voice_id_is_nullable = 0
        THEN 'ALTER TABLE aweme MODIFY COLUMN voice_id INT NULL DEFAULT NULL'
    ELSE 'SELECT 1'  -- already migrated (nullable, DEFAULT NULL) - idempotent no-op
END;
PREPARE aweme_voice_id_stmt FROM @aweme_voice_id_sql;
EXECUTE aweme_voice_id_stmt;
DEALLOCATE PREPARE aweme_voice_id_stmt;

SET @has_aweme_voice_id_index = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND INDEX_NAME = 'idx_aweme_voice_id'
);
SET @aweme_voice_id_index_sql = CASE
    WHEN @has_aweme_voice_id_index = 0 THEN 'ALTER TABLE aweme ADD KEY idx_aweme_voice_id (voice_id)'
    ELSE 'SELECT 1'
END;
PREPARE aweme_voice_id_index_stmt FROM @aweme_voice_id_index_sql;
EXECUTE aweme_voice_id_index_stmt;
DEALLOCATE PREPARE aweme_voice_id_index_stmt;

-- ── Orphan / source-data pre-checks, computed here (top level) so Step 6's
-- procedure can reference them via session (@) variables without needing
-- dynamic SQL inside the procedure body itself (review items 4/5). Each is
-- 0 when the corresponding legacy column doesn't exist (State B) - nothing
-- to check in that state. ──
SET @has_legacy_voice_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'voice_id'
);
SET @has_legacy_logo_id = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'logo_id'
);

SET @orphan_voice_count = 0;
SET @orphan_logo_count = 0;

SET @orphan_voice_sql = CASE
    WHEN @has_legacy_voice_id > 0
        THEN 'SELECT COUNT(*) INTO @orphan_voice_count FROM channels c LEFT JOIN voices v ON v.id = c.voice_id WHERE v.id IS NULL'
    ELSE 'SET @orphan_voice_count = 0'
END;
PREPARE orphan_voice_stmt FROM @orphan_voice_sql;
EXECUTE orphan_voice_stmt;
DEALLOCATE PREPARE orphan_voice_stmt;

SET @orphan_logo_sql = CASE
    WHEN @has_legacy_logo_id > 0
        THEN 'SELECT COUNT(*) INTO @orphan_logo_count FROM channels c LEFT JOIN logos l ON l.id = c.logo_id WHERE c.logo_id IS NOT NULL AND l.id IS NULL'
    ELSE 'SET @orphan_logo_count = 0'
END;
PREPARE orphan_logo_stmt FROM @orphan_logo_sql;
EXECUTE orphan_logo_stmt;
DEALLOCATE PREPARE orphan_logo_stmt;

-- ── Step 5/6: pre-backfill schema + source-data verification ────────────────
-- Ordinary conditional SQL (IF/CASE) can build and run different
-- statements, but it cannot itself ABORT a script with a custom error
-- message - only a stored routine can (SIGNAL SQLSTATE is only valid
-- inside a compound-statement block). This temporary procedure is created,
-- called once, and dropped again within this same file/session - it never
-- persists as part of the schema (review item 4's explicit suggestion).
--
-- Validates the COMPLETE resulting schema, not just channel_pipeline_configs:
-- since v4 owns the voices/logos DDL formerly provided by the deleted v2/v3
-- migrations (CREATE TABLE IF NOT EXISTS is a silent no-op against ANY
-- pre-existing table of that name, however incompatible), sections J/K below
-- validate voices/logos column shape, keys, and CHECK constraints just as
-- strictly as channel_pipeline_configs itself, and section L validates
-- aweme.voice_id's type/index in addition to section H's nullability/default
-- check. Every check validates the actual definition (column coverage of an
-- index, the real CHECK expression, the FK's own local/referenced columns) -
-- never an object's name alone - so a same-named-but-wrong index/constraint
-- still fails this gate. A partial/incompatible voices or logos table, or an
-- incorrect aweme.voice_id definition, aborts here, before any backfill.
DROP PROCEDURE IF EXISTS sp_v4_verify_before_backfill;

DELIMITER $$

CREATE PROCEDURE sp_v4_verify_before_backfill()
BEGIN
    DECLARE v_count INT DEFAULT 0;
    -- Behavioral CHECK-constraint validation (section O, near the end of
    -- this procedure): review feedback correctly identified that matching
    -- CHECK_CLAUSE text against a few expected substrings (e.g. LIKE
    -- '%`speed` >=%') passes a constraint that has been weakened (e.g.
    -- `speed >= 0 AND speed <= 999`) or made vacuous (e.g. `... OR 1 = 1`)
    -- while still containing every expected fragment. Exact-string
    -- comparison of CHECK_CLAUSE is not used instead because MySQL's own
    -- rendering of it (parenthesization, spacing, keyword case) is not
    -- guaranteed stable across versions/builds. This procedure instead
    -- PROBES actual behavior: inside a transaction that is unconditionally
    -- rolled back before this procedure returns (see section O), it
    -- attempts specific known-good and known-bad values against the real
    -- table and confirms the constraint actually accepts/rejects them -
    -- this is immune to both the "loose LIKE fragment" and "exact-string-
    -- fragility" problems, because it tests what the constraint DOES, not
    -- how MySQL happens to print it. @probe_rejected is set by this
    -- CONTINUE HANDLER whenever a probe statement hits a CHECK violation
    -- (MySQL error 3819); it is a session variable (not a local one) so it
    -- can be read/reset freely throughout the procedure without violating
    -- the rule that all DECLAREs must precede other statements in a block.
    DECLARE CONTINUE HANDLER FOR 3819 SET @probe_rejected = 1;

    -- ── A. channel_pipeline_configs: exact column count ──────────────────
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs';
    IF v_count <> 11 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs must have exactly 11 columns (partial/incompatible table)';
    END IF;

    -- ── A2. Storage engine / collation - checked EARLY (right after the
    -- basic column-count sanity check), before indexes/FKs/CHECKs.
    -- Deliberate ordering, not incidental: MySQL SILENTLY DROPS foreign key
    -- constraints on a non-InnoDB table (e.g. MyISAM) - the FK clause in
    -- CREATE TABLE is accepted but produces a plain KEY, no actual FK
    -- metadata at all (confirmed against a real server) - so a wrong-engine
    -- table would otherwise be reported as "FK missing" by section E below,
    -- which is a confusing, indirect symptom of the real problem. Checking
    -- engine/collation first gives the operator the actionable root cause
    -- directly.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND ENGINE = 'InnoDB' AND TABLE_COLLATION = 'utf8mb4_unicode_ci';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs must be ENGINE=InnoDB, COLLATE=utf8mb4_unicode_ci';
    END IF;

    -- ── B. channel_pipeline_configs: each column's type/nullability/default ─
    -- id: type, PRIMARY KEY membership, AND auto_increment - checked as
    -- three independent properties (COLUMN_KEY='PRI' alone would also be
    -- true for a lone single-column UNIQUE KEY promoted to display as PRI
    -- in some contexts; EXTRA='auto_increment' is the authoritative signal
    -- for the increment behavior specifically).
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'id' AND DATA_TYPE = 'int' AND COLUMN_KEY = 'PRI';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.id must be INT PRIMARY KEY';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'id' AND EXTRA LIKE '%auto_increment%';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.id must be AUTO_INCREMENT';
    END IF;
    -- Also confirm the PRIMARY KEY is exactly {id}, via INFORMATION_SCHEMA.STATISTICS
    -- (COLUMN_KEY='PRI' on the column alone does not rule out a composite PK).
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs' AND INDEX_NAME = 'PRIMARY';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs PRIMARY KEY must cover exactly id';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'channel_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'NO';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.channel_id has an unexpected type/nullability';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'translation_enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.translation_enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'dubbing_enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.dubbing_enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'voice_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.voice_id has an unexpected type/nullability';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'logo_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.logo_id has an unexpected type/nullability';
    END IF;

    -- voice_id/logo_id/daily_video_limit must each explicitly default to
    -- NULL (not merely be nullable with some other default, e.g. 0) -
    -- checked as its own property, separate from the type/nullability
    -- checks above.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'voice_id' AND COLUMN_DEFAULT IS NULL;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.voice_id must default to NULL';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'logo_id' AND COLUMN_DEFAULT IS NULL;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.logo_id must default to NULL';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'logo_enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.logo_enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'opening_hook_enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '0';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.opening_hook_enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'daily_video_limit' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES' AND COLUMN_DEFAULT IS NULL;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.daily_video_limit wrong type/nullability/default';
    END IF;

    -- created_at/updated_at: default AND update behavior. EXTRA carries
    -- the ON UPDATE CURRENT_TIMESTAMP behavior - COLUMN_DEFAULT alone
    -- cannot distinguish "defaults to CURRENT_TIMESTAMP but never auto-
    -- updates" from "defaults to and auto-updates on CURRENT_TIMESTAMP".
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'created_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP' AND EXTRA = 'DEFAULT_GENERATED';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.created_at wrong definition';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND COLUMN_NAME = 'updated_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP'
      AND EXTRA = 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: channel_pipeline_configs.updated_at wrong definition';
    END IF;

    -- ── C. Unique key on channel_id — validated by the COLUMN it actually
    -- covers, not just its name (a same-named index over the wrong column,
    -- e.g. left behind by a botched hand edit, must still fail this gate). ─
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'uq_channel_pipeline_configs_channel_id'
      AND COLUMN_NAME = 'channel_id' AND NON_UNIQUE = 0 AND SEQ_IN_INDEX = 1;
    IF v_count = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing UNIQUE key uq_channel_pipeline_configs_channel_id covering exactly channel_id';
    END IF;
    -- Also confirm the unique key covers ONLY channel_id (single-column) -
    -- a composite unique key of the same name over additional columns would
    -- still satisfy the query above alone.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'uq_channel_pipeline_configs_channel_id';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: uq_channel_pipeline_configs_channel_id must cover exactly one column (channel_id)';
    END IF;

    -- ── D. Required plain indexes — validated by the COLUMN each covers,
    -- AND that each index covers ONLY that one column (an unexpected
    -- composite index, e.g. (voice_id, logo_id), would still satisfy a
    -- bare "covers voice_id" check but is not what the approved schema
    -- defines). ────────────────────────────────────────────────────────
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'idx_channel_pipeline_configs_voice_id' AND COLUMN_NAME = 'voice_id';
    IF v_count = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing index idx_channel_pipeline_configs_voice_id covering voice_id';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'idx_channel_pipeline_configs_voice_id';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: idx_..._voice_id must cover exactly one column';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'idx_channel_pipeline_configs_logo_id' AND COLUMN_NAME = 'logo_id';
    IF v_count = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing index idx_channel_pipeline_configs_logo_id covering logo_id';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND INDEX_NAME = 'idx_channel_pipeline_configs_logo_id';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: idx_..._logo_id must cover exactly one column';
    END IF;

    -- ── E. Foreign keys — local column, referenced table+column, and
    -- ON DELETE/ON UPDATE rules ALL validated together (a same-named FK
    -- pointing at the wrong referenced column, or with the wrong local
    -- column, must still fail this gate - not just a table-name/rule check). ─
    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
      ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
    WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = 'channel_pipeline_configs'
      AND kcu.CONSTRAINT_NAME = 'fk_channel_pipeline_configs_channel'
      AND kcu.COLUMN_NAME = 'channel_id'
      AND kcu.REFERENCED_TABLE_NAME = 'channels' AND kcu.REFERENCED_COLUMN_NAME = 'id'
      AND rc.DELETE_RULE = 'CASCADE' AND rc.UPDATE_RULE = 'CASCADE';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: fk_channel_pipeline_configs_channel wrong (want channel_id->channels.id CASCADE/CASCADE)';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
      ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
    WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = 'channel_pipeline_configs'
      AND kcu.CONSTRAINT_NAME = 'fk_channel_pipeline_configs_voice'
      AND kcu.COLUMN_NAME = 'voice_id'
      AND kcu.REFERENCED_TABLE_NAME = 'voices' AND kcu.REFERENCED_COLUMN_NAME = 'id'
      AND rc.DELETE_RULE = 'SET NULL' AND rc.UPDATE_RULE = 'CASCADE';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: fk_channel_pipeline_configs_voice wrong (want voice_id->voices.id SET NULL/CASCADE)';
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
      ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
    WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = 'channel_pipeline_configs'
      AND kcu.CONSTRAINT_NAME = 'fk_channel_pipeline_configs_logo'
      AND kcu.COLUMN_NAME = 'logo_id'
      AND kcu.REFERENCED_TABLE_NAME = 'logos' AND kcu.REFERENCED_COLUMN_NAME = 'id'
      AND rc.DELETE_RULE = 'SET NULL' AND rc.UPDATE_RULE = 'CASCADE';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: fk_channel_pipeline_configs_logo wrong (want logo_id->logos.id SET NULL/CASCADE)';
    END IF;

    -- ── F. CHECK constraints — structural existence ONLY at this point
    -- (object exists, attached to this table, of type CHECK). This is
    -- deliberately NOT where semantic correctness is decided - a CHECK's
    -- actual logical expression is validated behaviorally in section O,
    -- near the end of this procedure (see that section's own comment for
    -- why LIKE-fragment/exact-string CHECK_CLAUSE matching is not used).
    -- A completely missing or renamed constraint is still caught here,
    -- before the behavioral probes ever run.
    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND CONSTRAINT_NAME = 'chk_channel_pipeline_configs_valid_combo' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing CHECK chk_valid_combo';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND CONSTRAINT_NAME = 'chk_channel_pipeline_configs_boolean_values' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing CHECK chk_boolean_values';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
      AND CONSTRAINT_NAME = 'chk_channel_pipeline_configs_daily_limit' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: missing CHECK chk_daily_limit';
    END IF;

    -- (Storage engine/collation for this table is checked early - see
    -- section A2 above.)

    -- ── H. aweme.voice_id nullability/default (must already be normalized
    -- by Step 4 above, which always runs before this procedure is called) ─
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id'
      AND IS_NULLABLE = 'YES' AND COLUMN_DEFAULT IS NULL;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: aweme.voice_id must be nullable with DEFAULT NULL after Step 4';
    END IF;

    -- ── I. Source-data validation: orphan legacy references (review item 5) ─
    -- @orphan_voice_count / @orphan_logo_count are computed at the top
    -- level, above, into session variables this procedure can read
    -- directly (0 in State B, where the legacy columns don't exist at all).
    IF @orphan_voice_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 preflight failed: orphan channels.voice_id reference(s) found - see preflight script to remediate';
    END IF;

    IF @orphan_logo_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 preflight failed: orphan channels.logo_id reference(s) found - see preflight script to remediate';
    END IF;

    -- ── J. voices: v4 owns this DDL (formerly the deleted v2 migration) -
    -- validate its full required shape, not just that the table exists,
    -- since CREATE TABLE IF NOT EXISTS silently no-ops against ANY
    -- pre-existing table of that name, however incompatible. ──────────────
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices';
    IF v_count <> 12 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 schema check failed: voices must have exactly 12 columns (partial/incompatible table)';
    END IF;

    -- id: type, PK, AUTO_INCREMENT.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'id' AND DATA_TYPE = 'int' AND COLUMN_KEY = 'PRI' AND EXTRA LIKE '%auto_increment%';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.id must be INT PRIMARY KEY AUTO_INCREMENT';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices' AND INDEX_NAME = 'PRIMARY';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices PRIMARY KEY must cover exactly id';
    END IF;

    -- name/filename: type, nullability, AND the approved VARCHAR length.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'name' AND DATA_TYPE = 'varchar' AND IS_NULLABLE = 'NO' AND CHARACTER_MAXIMUM_LENGTH = 100;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.name must be VARCHAR(100) NOT NULL';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'filename' AND DATA_TYPE = 'varchar' AND IS_NULLABLE = 'NO' AND CHARACTER_MAXIMUM_LENGTH = 255;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.filename must be VARCHAR(255) NOT NULL';
    END IF;

    -- target_wps/min_wps/max_wps/speed: type, nullability, default, AND the
    -- approved DECIMAL(4,2) precision/scale - a DECIMAL(6,2) or DECIMAL(4,1)
    -- column would otherwise still satisfy a bare DATA_TYPE='decimal' check.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'target_wps' AND DATA_TYPE = 'decimal' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '4.30'
      AND NUMERIC_PRECISION = 4 AND NUMERIC_SCALE = 2;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.target_wps must be DECIMAL(4,2) DEFAULT 4.30';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'min_wps' AND DATA_TYPE = 'decimal' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '3.90'
      AND NUMERIC_PRECISION = 4 AND NUMERIC_SCALE = 2;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.min_wps must be DECIMAL(4,2) DEFAULT 3.90';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'max_wps' AND DATA_TYPE = 'decimal' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '4.70'
      AND NUMERIC_PRECISION = 4 AND NUMERIC_SCALE = 2;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.max_wps must be DECIMAL(4,2) DEFAULT 4.70';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'speed' AND DATA_TYPE = 'decimal' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1.00'
      AND NUMERIC_PRECISION = 4 AND NUMERIC_SCALE = 2;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.speed must be DECIMAL(4,2) DEFAULT 1.00';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'is_default' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '0';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.is_default has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'created_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP' AND EXTRA = 'DEFAULT_GENERATED';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.created_at wrong definition';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND COLUMN_NAME = 'updated_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP'
      AND EXTRA = 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices.updated_at wrong definition';
    END IF;

    -- Unique/plain keys, validated by the COLUMN they cover AND that each
    -- covers exactly one column (reject an unexpected composite index).
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND INDEX_NAME = 'uq_voices_name' AND COLUMN_NAME = 'name' AND NON_UNIQUE = 0;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices missing UNIQUE key uq_voices_name on name';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices' AND INDEX_NAME = 'uq_voices_name';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: uq_voices_name must cover exactly one column';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND INDEX_NAME = 'idx_voices_enabled' AND COLUMN_NAME = 'enabled';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices missing index idx_voices_enabled on enabled';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices' AND INDEX_NAME = 'idx_voices_enabled';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: idx_voices_enabled must cover exactly one column';
    END IF;

    -- CHECK constraints: structural existence only here - semantic
    -- correctness (positive bounds, ordering, exact speed range) is
    -- validated behaviorally in section O (see that section's comment).
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND CONSTRAINT_NAME = 'chk_voices_wps_positive' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices missing CHECK chk_voices_wps_positive';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND CONSTRAINT_NAME = 'chk_voices_wps_order' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices missing CHECK chk_voices_wps_order';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND CONSTRAINT_NAME = 'chk_voices_speed_range' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices missing CHECK chk_voices_speed_range';
    END IF;

    -- Storage engine / collation.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'voices'
      AND ENGINE = 'InnoDB' AND TABLE_COLLATION = 'utf8mb4_unicode_ci';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: voices must be ENGINE=InnoDB, COLLATE=utf8mb4_unicode_ci';
    END IF;

    -- Existing-data compatibility: every existing voice row must itself
    -- satisfy the CHECK constraints' approved semantics (belt-and-
    -- suspenders - MySQL already enforces this on write, but a table
    -- created before the CHECKs existed, e.g. an older MySQL version,
    -- could hold violating rows the behavioral probes below would never
    -- see, since those probes use their OWN throwaway row).
    SELECT COUNT(*) INTO v_count FROM voices
    WHERE NOT (target_wps > 0 AND min_wps > 0 AND max_wps > 0)
       OR NOT (min_wps <= target_wps AND target_wps <= max_wps)
       OR NOT (speed >= 0.70 AND speed <= 1.30)
       OR enabled NOT IN (0, 1) OR is_default NOT IN (0, 1);
    IF v_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 schema check failed: voices contains existing row(s) violating its own CHECK constraints - incompatible pre-existing data';
    END IF;

    -- ── K. logos: v4 owns this DDL (formerly the deleted v3 migration) ──────
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos';
    IF v_count <> 8 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 schema check failed: logos must have exactly 8 columns (partial/incompatible table)';
    END IF;

    -- id: type, PK, AUTO_INCREMENT.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'id' AND DATA_TYPE = 'int' AND COLUMN_KEY = 'PRI' AND EXTRA LIKE '%auto_increment%';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.id must be INT PRIMARY KEY AUTO_INCREMENT';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos' AND INDEX_NAME = 'PRIMARY';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos PRIMARY KEY must cover exactly id';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'name' AND DATA_TYPE = 'varchar' AND IS_NULLABLE = 'NO' AND CHARACTER_MAXIMUM_LENGTH = 100;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.name must be VARCHAR(100) NOT NULL';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'filename' AND DATA_TYPE = 'varchar' AND IS_NULLABLE = 'NO' AND CHARACTER_MAXIMUM_LENGTH = 512;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.filename must be VARCHAR(512) NOT NULL';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'size_px' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES' AND COLUMN_DEFAULT IS NULL;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.size_px must be INT NULL DEFAULT NULL';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'enabled' AND DATA_TYPE = 'tinyint' AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = '1';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.enabled has an unexpected type/nullability/default';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'created_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP' AND EXTRA = 'DEFAULT_GENERATED';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.created_at wrong definition';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND COLUMN_NAME = 'updated_at' AND DATA_TYPE = 'datetime' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'CURRENT_TIMESTAMP'
      AND EXTRA = 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos.updated_at wrong definition';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND INDEX_NAME = 'uq_logos_name' AND COLUMN_NAME = 'name' AND NON_UNIQUE = 0;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos missing UNIQUE key uq_logos_name on name';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos' AND INDEX_NAME = 'uq_logos_name';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: uq_logos_name must cover exactly one column';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND INDEX_NAME = 'uq_logos_filename' AND COLUMN_NAME = 'filename' AND NON_UNIQUE = 0;
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos missing UNIQUE key uq_logos_filename on filename';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos' AND INDEX_NAME = 'uq_logos_filename';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: uq_logos_filename must cover exactly one column';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND INDEX_NAME = 'idx_logos_enabled' AND COLUMN_NAME = 'enabled';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos missing index idx_logos_enabled on enabled';
    END IF;
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos' AND INDEX_NAME = 'idx_logos_enabled';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: idx_logos_enabled must cover exactly one column';
    END IF;

    -- CHECK constraint: structural existence only - semantics validated
    -- behaviorally in section O.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND CONSTRAINT_NAME = 'chk_logos_size_px_positive' AND CONSTRAINT_TYPE = 'CHECK';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos missing CHECK chk_logos_size_px_positive';
    END IF;

    -- Storage engine / collation.
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'logos'
      AND ENGINE = 'InnoDB' AND TABLE_COLLATION = 'utf8mb4_unicode_ci';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: logos must be ENGINE=InnoDB, COLLATE=utf8mb4_unicode_ci';
    END IF;

    SELECT COUNT(*) INTO v_count FROM logos
    WHERE NOT (size_px IS NULL OR size_px > 0) OR enabled NOT IN (0, 1);
    IF v_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 schema check failed: logos contains existing row(s) violating its own CHECK constraints - incompatible pre-existing data';
    END IF;

    -- ── L. aweme.voice_id: type and index, beyond the nullability/default
    -- already checked in section H above ─────────────────────────────────
    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id'
      AND DATA_TYPE = 'int';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: aweme.voice_id must be of type INT';
    END IF;

    SELECT COUNT(*) INTO v_count FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme'
      AND INDEX_NAME = 'idx_aweme_voice_id' AND COLUMN_NAME = 'voice_id';
    IF v_count <> 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 schema check failed: aweme missing index idx_aweme_voice_id on voice_id';
    END IF;

    -- ── O. Behavioral CHECK-constraint probes ────────────────────────────
    -- See this procedure's own top-of-body comment (next to the
    -- `DECLARE CONTINUE HANDLER FOR 3819` line) for why behavior is probed
    -- instead of matching CHECK_CLAUSE text. Everything below runs inside
    -- ONE transaction; every failure branch explicitly ROLLBACKs before
    -- SIGNALing (not left to an implicit rollback-on-disconnect), and the
    -- success path ROLLBACKs at the very end - no probe row is ever left
    -- behind in either outcome, and none of this runs until every
    -- structural check above (A-L) has already passed, so a probe
    -- INSERT/UPDATE failing for an unrelated structural reason cannot happen.
    START TRANSACTION;

    -- Throwaway probe channel/voice/logo. The AUTO_INCREMENT gap this
    -- leaves on channels/voices/logos afterward is expected and harmless -
    -- InnoDB's auto_increment counter is not transactional and is not
    -- reclaimed by the ROLLBACKs below.
    INSERT INTO channels (name, douyin_url) VALUES ('__v4_check_probe__', 'https://v4-check-probe.invalid');
    SET @probe_channel_id = LAST_INSERT_ID();

    INSERT INTO voices (name, filename, target_wps, min_wps, max_wps, speed, enabled, is_default)
    VALUES ('__v4_check_probe__', '__v4_check_probe__.wav', 4.30, 3.90, 4.70, 1.00, 1, 0);
    SET @probe_voice_id = LAST_INSERT_ID();

    INSERT INTO logos (name, filename, size_px, enabled) VALUES ('__v4_check_probe__', '__v4_check_probe__.png', 100, 1);
    SET @probe_logo_id = LAST_INSERT_ID();

    -- Baseline valid channel_pipeline_configs row - must succeed (not
    -- itself a specific probe; a failure here is an ordinary, uncaught
    -- SQL error, since only 3819 has a handler).
    INSERT INTO channel_pipeline_configs
        (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id, logo_enabled, opening_hook_enabled, daily_video_limit)
    VALUES (@probe_channel_id, 1, 1, @probe_voice_id, @probe_logo_id, 1, 0, 0);

    -- chk_channel_pipeline_configs_valid_combo
    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET translation_enabled = 0, dubbing_enabled = 1 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: valid_combo allowed translation=0/dubbing=1';
    END IF;

    -- chk_channel_pipeline_configs_boolean_values (each of the four flags)
    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET translation_enabled = 2 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: boolean_values allowed translation_enabled=2';
    END IF;

    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET dubbing_enabled = 2 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: boolean_values allowed dubbing_enabled=2';
    END IF;

    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET logo_enabled = 2 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: boolean_values allowed logo_enabled=2';
    END IF;

    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET opening_hook_enabled = 2 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: boolean_values allowed opening_hook_enabled=2';
    END IF;

    -- chk_channel_pipeline_configs_daily_limit (negative rejected; NULL/0 accepted)
    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET daily_video_limit = -1 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: daily_limit allowed -1';
    END IF;

    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET daily_video_limit = NULL WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 1 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: daily_limit incorrectly rejected NULL';
    END IF;

    SET @probe_rejected = 0;
    UPDATE channel_pipeline_configs SET daily_video_limit = 0 WHERE channel_id = @probe_channel_id;
    IF @probe_rejected = 1 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: daily_limit incorrectly rejected 0';
    END IF;

    -- chk_voices_wps_positive
    SET @probe_rejected = 0;
    UPDATE voices SET target_wps = -1 WHERE id = @probe_voice_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: wps_positive allowed target_wps=-1';
    END IF;

    -- chk_voices_wps_order (min_wps > target_wps must be rejected)
    SET @probe_rejected = 0;
    UPDATE voices SET min_wps = 10.00 WHERE id = @probe_voice_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: wps_order allowed min_wps > target_wps';
    END IF;

    -- chk_voices_speed_range - exact approved bounds 0.70/1.30 (review
    -- feedback's own example: a weakened `speed >= 0 AND speed <= 999`
    -- still contains the `>=`/`<=` fragments but must fail these probes).
    SET @probe_rejected = 0;
    UPDATE voices SET speed = 0.10 WHERE id = @probe_voice_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: speed_range allowed speed=0.10';
    END IF;

    -- 9.99 (not 999): must stay within DECIMAL(4,2)'s own representable
    -- range (max 99.99) so this probes the CHECK constraint's bound, not
    -- an unrelated numeric-overflow error (1264) that a real out-of-range
    -- literal would trigger instead.
    SET @probe_rejected = 0;
    UPDATE voices SET speed = 9.99 WHERE id = @probe_voice_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: speed_range allowed speed=9.99';
    END IF;

    SET @probe_rejected = 0;
    UPDATE voices SET speed = 0.70 WHERE id = @probe_voice_id;
    IF @probe_rejected = 1 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: speed_range incorrectly rejected 0.70';
    END IF;

    SET @probe_rejected = 0;
    UPDATE voices SET speed = 1.30 WHERE id = @probe_voice_id;
    IF @probe_rejected = 1 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: speed_range incorrectly rejected 1.30';
    END IF;

    -- chk_logos_size_px_positive
    SET @probe_rejected = 0;
    UPDATE logos SET size_px = -1 WHERE id = @probe_logo_id;
    IF @probe_rejected = 0 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: size_px_positive allowed -1';
    END IF;

    SET @probe_rejected = 0;
    UPDATE logos SET size_px = NULL WHERE id = @probe_logo_id;
    IF @probe_rejected = 1 THEN
        ROLLBACK;
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'v4 check probe failed: size_px_positive incorrectly rejected NULL';
    END IF;

    ROLLBACK;
END$$

DELIMITER ;

CALL sp_v4_verify_before_backfill();

DROP PROCEDURE IF EXISTS sp_v4_verify_before_backfill;

-- ── Step 7: backfill — one row per existing channel, state-aware ───────────
-- Dynamic SQL (PREPARE/EXECUTE) is required here, not a plain IF/ELSE
-- around static statements: a static statement referencing
-- channels.voice_id would fail to even PARSE against State B, where that
-- column does not exist at all. This mirrors
-- docs/pipeline-migrate-existing-v1.sql's own SET @sql = IF(...); PREPARE
-- ... EXECUTE ... DEALLOCATE convention for the same reason.
--
-- State A: both legacy columns present - copy verbatim. Reaching this point
-- already proves (via Step 6's procedure) that no orphan reference exists.
SET @backfill_both = '
INSERT INTO channel_pipeline_configs
    (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id,
     logo_enabled, opening_hook_enabled, daily_video_limit)
SELECT c.id, 1, 1, c.voice_id, c.logo_id, 1, 0, NULL
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cpc WHERE cpc.channel_id = c.id
)';

-- State A, voice_id only (handled explicitly rather than assumed away,
-- even though historically v2 always preceded v3 in this repo's own
-- deployment order).
SET @backfill_voice_only = '
INSERT INTO channel_pipeline_configs
    (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id,
     logo_enabled, opening_hook_enabled, daily_video_limit)
SELECT c.id, 1, 1, c.voice_id, NULL, 1, 0, NULL
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cpc WHERE cpc.channel_id = c.id
)';

-- State A, logo_id only.
SET @backfill_logo_only = '
INSERT INTO channel_pipeline_configs
    (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id,
     logo_enabled, opening_hook_enabled, daily_video_limit)
SELECT c.id, 1, 1, NULL, c.logo_id, 1, 0, NULL
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cpc WHERE cpc.channel_id = c.id
)';

-- State B: neither legacy column present (clean v1-only install) - nothing
-- to migrate; every channel starts with voice_id=NULL/logo_id=NULL.
SET @backfill_neither = '
INSERT INTO channel_pipeline_configs
    (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id,
     logo_enabled, opening_hook_enabled, daily_video_limit)
SELECT c.id, 1, 1, NULL, NULL, 1, 0, NULL
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cpc WHERE cpc.channel_id = c.id
)';

SET @backfill_sql = CASE
    WHEN @has_legacy_voice_id > 0 AND @has_legacy_logo_id > 0 THEN @backfill_both
    WHEN @has_legacy_voice_id > 0 THEN @backfill_voice_only
    WHEN @has_legacy_logo_id > 0 THEN @backfill_logo_only
    ELSE @backfill_neither
END;

PREPARE stmt FROM @backfill_sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ── Rollback (manual - NOT executed by this script) ─────────────────────────
--   DROP TABLE IF EXISTS channel_pipeline_configs;
--   -- aweme.voice_id: no automated rollback to NOT NULL DEFAULT 1 is
--   -- provided - re-tightening a column to NOT NULL after rows may have
--   -- been inserted with NULL requires a data decision (what value to
--   -- backfill NULLs with) this migration deliberately does not make.
--   -- If rollback is genuinely required, first decide that policy, then:
--   --   ALTER TABLE aweme MODIFY COLUMN voice_id INT NOT NULL DEFAULT 1;
--   --   -- (fails if any row is currently NULL - resolve those first)
--   -- Do NOT drop voices/logos: docs/voices-seed-data.sql /
--   -- docs/logos-seed-data.sql rows, and any channel/aweme row still
--   -- referencing them via the (kept-intact) legacy columns, depend on
--   -- them.
--
-- Legacy channels.voice_id / channels.logo_id are intentionally NOT dropped
-- by this migration (see D13 in docs/channel-pipeline-config-plan.md) -
-- dropping them is deferred to a later, separate forward migration once
-- dub_worker's runtime dual-read adoption has shipped and been verified
-- against a real database (docs/pipeline-verify-channel-pipeline-config-v4.sql).
