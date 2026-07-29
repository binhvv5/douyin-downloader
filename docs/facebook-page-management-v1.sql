-- Migration: Facebook Page management (channel-admin now owns this, not
-- toolhay-service) — adds verification bookkeeping to the EXISTING
-- destination/token catalog rather than creating a duplicate account model.
-- Ref: docs/pipeline-upload-feature.md, README.md "Facebook Page management"
-- Usage: mysql -u <user> -p <your_database> < docs/facebook-page-management-v1.sql
--   (or: mysql -u <user> -p --database=<your_database> < ...)
--
-- Does NOT hard-code `USE douyin_downloader` — operates on whatever database
-- the calling session already selected (same convention as the other
-- docs/pipeline-migrate-*.sql files in this repo).
--
-- Audited 2026-07-26: `tbl_social_account_token` and `upload_accounts` were
-- EMPTY in every environment checked before this migration was written —
-- these are additive-only, nullable/defaulted columns regardless, so this
-- is safe to run even if that changes before you apply it. No existing row
-- is modified or removed by this file.
--
-- Reused as-is (no schema change): `tbl_social_account_token.access_token`
-- (TEXT, plaintext — see README.md "Token storage" for why this project
-- deliberately does not encrypt it), `.expires_at` (token expiration),
-- `.active` (enable/disable), `.account_ref` (Facebook Page ID),
-- `.account_label` (Page name), `.platform` (2 = Facebook — see
-- channel_admin/platforms.py).
--
-- New columns — verification bookkeeping only; nothing here is a
-- credential, and no column here can ever hold a token. Plain ADD COLUMN
-- (not "IF NOT EXISTS" — that MariaDB-only extension is not valid syntax on
-- real MySQL 8.0): safe to run once against a database that has not yet
-- run this migration; re-running it on a database that already has these
-- columns will fail loudly (ER_DUP_FIELDNAME) rather than silently no-op,
-- which is the correct behavior for a one-time migration file.
ALTER TABLE tbl_social_account_token
    ADD COLUMN last_verified_at datetime DEFAULT NULL
        COMMENT 'Last time credentials were checked against the Meta Graph API (NULL = never verified)',
    ADD COLUMN verification_status varchar(32) NOT NULL DEFAULT 'unverified'
        COMMENT 'One of: unverified, verified, failed — see channel_admin/services/facebook_pages.py',
    ADD COLUMN verification_error text DEFAULT NULL
        COMMENT 'Sanitized reason for the last failed verification (NULL when verification_status != ''failed'')';
