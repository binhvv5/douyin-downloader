-- Preflight v4.0 — orphan-reference detection before running
-- docs/pipeline-migrate-channel-pipeline-config-v4.sql
-- Ref: docs/channel-pipeline-config-plan.md
-- Usage: mysql -u douyin -p <your_database> < docs/pipeline-preflight-channel-pipeline-config-v4.sql
--
-- This file does NOT hard-code `USE douyin_downloader` (review item 2) -
-- it operates on whatever database the calling session already has
-- selected. See docs/pipeline-migrate-channel-pipeline-config-v4.sql's own
-- header for why, and dub_worker/tests/test_db_pipeline_config_integration.py
-- for the test-side database-name safety checks.
--
-- ─────────────────────────────────────────────────────────────────────────
-- State A/State B aware (this revision fixes a prior defect): this script
-- runs successfully, with no error, against EITHER starting state:
--
--   State A - an existing database that already ran the former
--   docs/pipeline-migrate-voice-config-v2.sql /
--   pipeline-migrate-channel-logo-v3.sql (channels.voice_id/channels.logo_id
--   exist): runs the real orphan-detection queries below and reports
--   legacy values that will be migrated.
--
--   State B - a clean database bootstrapped from v1 only (those legacy
--   columns were never added): every check that would otherwise reference
--   a missing column returns an INFORMATION_SCHEMA-driven informational
--   result explicitly saying that check was skipped for this reason, and
--   every other, still-applicable check still runs. This script never
--   raises "Unknown column" in State B, and this file contains no
--   instruction telling an operator to expect, ignore, or treat any SQL
--   error as an expected outcome - a clean run always means clean output,
--   in both states.
--
-- HOW: every check below lives inside a temporary stored procedure
-- (sp_v4_preflight, created/called/dropped by this same script) and is
-- guarded by an INFORMATION_SCHEMA existence check. MySQL does not resolve
-- table/column references inside a stored routine's body until the
-- containing branch actually EXECUTES (verified against a real MySQL 8.0
-- server for this revision - a static `SELECT c.voice_id FROM channels c`
-- inside an untaken `IF` branch does not raise "Unknown column" even when
-- `channels.voice_id` does not exist), so a plain guarded IF/ELSE is
-- sufficient here - no PREPARE/EXECUTE dynamic SQL is required for this
-- file's diagnostic queries.
--
-- WHY ORPHANS MATTER: channels.voice_id is currently NOT NULL DEFAULT 1
-- with NO foreign key (legacy "by request" design) - nothing today prevents
-- it from drifting to a dangling id if a `voices` row is ever deleted
-- directly. channels.logo_id already has a real FK (ON DELETE SET NULL),
-- so it should be empty in a healthy database, but this verifies rather
-- than assumes that, since a raw UPDATE could in principle have bypassed
-- it.
--
-- SINGLE MIGRATION POLICY: there is no "exclude the orphaned channel from
-- the backfill and continue for everyone else" option, and none is
-- implemented anywhere in this repository. Any orphan legacy voice/logo
-- reference blocks the ENTIRE migration until the source data is manually
-- remediated (see the remediation SQL in each section below). This script
-- SIGNALs a clear, blocking error if it finds any orphan, so running it
-- first surfaces that stop before you even attempt the migration - which
-- also independently re-checks the same condition itself (see
-- docs/pipeline-migrate-channel-pipeline-config-v4.sql Step 6) and refuses
-- to backfill if any orphan reference exists; this preflight script is a
-- diagnostic aid, not the only safety net.

DROP PROCEDURE IF EXISTS sp_v4_preflight;

DELIMITER $$

CREATE PROCEDURE sp_v4_preflight()
BEGIN
    DECLARE has_legacy_voice_id INT DEFAULT 0;
    DECLARE has_legacy_logo_id INT DEFAULT 0;
    DECLARE has_aweme_voice_id INT DEFAULT 0;
    DECLARE orphan_voice_count INT DEFAULT 0;
    DECLARE orphan_logo_count INT DEFAULT 0;

    SELECT COUNT(*) INTO has_legacy_voice_id FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'voice_id';
    SELECT COUNT(*) INTO has_legacy_logo_id FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channels' AND COLUMN_NAME = 'logo_id';
    SELECT COUNT(*) INTO has_aweme_voice_id FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'voice_id';

    -- ── 1. Orphan channels.voice_id (points at a voices.id that no longer
    -- exists) - diagnostic rows identify the affected channel_id/name and
    -- the invalid voice_id itself. ───────────────────────────────────────
    IF has_legacy_voice_id > 0 THEN
        SELECT COUNT(*) INTO orphan_voice_count
        FROM channels c LEFT JOIN voices v ON v.id = c.voice_id
        WHERE v.id IS NULL;

        SELECT c.id AS channel_id, c.name, c.voice_id
        FROM channels c LEFT JOIN voices v ON v.id = c.voice_id
        WHERE v.id IS NULL;
        -- Expect zero rows. If any row is returned, remediate (D1) with:
        --   SELECT id FROM voices WHERE enabled = 1 AND is_default = 1 ORDER BY id LIMIT 1;
        --   UPDATE channels SET voice_id = <default_id_from_above> WHERE id IN (<channel_ids_from_above>);
        -- This reassigns the orphaned channel(s) to the current
        -- system-default voice - the same voice voice_resolver.py's own
        -- fallback chain would have resolved to anyway once it reached
        -- that priority level.
    ELSE
        SELECT 'channels.voice_id column not present (State B / clean v1-only install) - legacy voice_id orphan check skipped, nothing to remediate here' AS info;
    END IF;

    -- ── 2. Orphan channels.logo_id (points at a logos.id that no longer
    -- exists) - diagnostic rows identify the affected channel_id/name and
    -- the invalid logo_id itself. ────────────────────────────────────────
    IF has_legacy_logo_id > 0 THEN
        SELECT COUNT(*) INTO orphan_logo_count
        FROM channels c LEFT JOIN logos l ON l.id = c.logo_id
        WHERE c.logo_id IS NOT NULL AND l.id IS NULL;

        SELECT c.id AS channel_id, c.name, c.logo_id
        FROM channels c LEFT JOIN logos l ON l.id = c.logo_id
        WHERE c.logo_id IS NOT NULL AND l.id IS NULL;
        -- Expect zero rows (channels.logo_id already has a real FK with
        -- ON DELETE SET NULL - this should be structurally impossible in a
        -- healthy database). If any row is returned, remediate (D2) with:
        --   UPDATE channels SET logo_id = NULL WHERE id IN (<channel_ids_from_above>);
    ELSE
        SELECT 'channels.logo_id column not present (State B / clean v1-only install) - legacy logo_id orphan check skipped, nothing to remediate here' AS info;
    END IF;

    -- ── 3. Legacy values that will be migrated (report, not a gate) ──────
    IF has_legacy_voice_id > 0 AND has_legacy_logo_id > 0 THEN
        SELECT c.id AS channel_id, c.name, c.voice_id AS legacy_voice_id, c.logo_id AS legacy_logo_id
        FROM channels c
        ORDER BY c.id;
    ELSEIF has_legacy_voice_id > 0 THEN
        SELECT c.id AS channel_id, c.name, c.voice_id AS legacy_voice_id
        FROM channels c
        ORDER BY c.id;
    ELSEIF has_legacy_logo_id > 0 THEN
        SELECT c.id AS channel_id, c.name, c.logo_id AS legacy_logo_id
        FROM channels c
        ORDER BY c.id;
    ELSE
        SELECT 'no legacy channels.voice_id/channels.logo_id columns present - nothing to migrate (State B / clean v1-only install)' AS info;
    END IF;

    -- ── 4. Informational: how many aweme rows currently have voice_id = 1
    -- (review item 1). NOT a blocking check - these rows are existing,
    -- explicit-or-default values the migration deliberately leaves
    -- UNCHANGED (see docs/pipeline-migrate-channel-pipeline-config-v4.sql
    -- Step 4's comment for why: the database cannot distinguish "nobody
    -- ever set this" from "an operator explicitly chose voice id 1"). ────
    IF has_aweme_voice_id > 0 THEN
        SELECT COUNT(*) AS aweme_rows_with_voice_id_1
        FROM aweme WHERE voice_id = 1;
    ELSE
        SELECT 'aweme.voice_id column not present yet - it will be created (nullable, DEFAULT NULL) by the migration; nothing to report here' AS info;
    END IF;

    -- ── 5. Block: any orphan reference found above stops this preflight
    -- with a clear, blocking error - surfaced BEFORE attempting the
    -- migration (which independently re-checks and blocks the same
    -- condition itself; see this file's header). Raised last, after every
    -- diagnostic result set above has already been returned to the
    -- operator, so nothing above is hidden by an early abort. ───────────
    IF orphan_voice_count > 0 AND orphan_logo_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 preflight failed: orphan voice_id AND logo_id found - remediate both above; no exclude-channel option exists';
    ELSEIF orphan_voice_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 preflight failed: orphan channels.voice_id found - remediate above; no exclude-channel option exists';
    ELSEIF orphan_logo_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'v4 preflight failed: orphan channels.logo_id found - remediate above; no exclude-channel option exists';
    END IF;
END$$

DELIMITER ;

CALL sp_v4_preflight();

DROP PROCEDURE IF EXISTS sp_v4_preflight;
