-- Migration v1.1 — bổ sung schema pipeline (dự án nghiên cứu)
-- Ref: docs/SRS-douyin-download-pipeline.md
-- Usage: mysql -u douyin -p douyin_downloader < docs/sql/pipeline-migrate-existing-v1.sql

USE douyin_downloader;

CREATE TABLE IF NOT EXISTS channels (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    douyin_url      TEXT NOT NULL,
    sec_uid         VARCHAR(128) NULL,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    sync_mode       ENUM('full', 'incremental') NOT NULL DEFAULT 'incremental',
    last_sync_at    DATETIME NULL,
    notes           TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_channels_sec_uid (sec_uid),
    KEY idx_channels_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @col_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'channel_id'
);
SET @sql = IF(@col_exists = 0,
    'ALTER TABLE aweme ADD COLUMN channel_id INT NULL AFTER aweme_type',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'download_status'
);
SET @sql = IF(@col_exists = 0,
    'ALTER TABLE aweme ADD COLUMN download_status ENUM(''pending'',''downloading'',''success'',''failed'',''skipped'') NOT NULL DEFAULT ''success'' AFTER download_time',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'created_at'
);
SET @sql = IF(@col_exists = 0,
    'ALTER TABLE aweme ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'updated_at'
);
SET @sql = IF(@col_exists = 0,
    'ALTER TABLE aweme ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE aweme SET download_status = 'success' WHERE download_time IS NOT NULL;

CREATE TABLE IF NOT EXISTS video_assets (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    aweme_id        VARCHAR(64) NOT NULL,
    asset_type      ENUM(
        'source_mp4', 'cover', 'music', 'metadata_json',
        'transcript_zh', 'transcript_vi', 'dubbed_mp4', 'subtitle_vi'
    ) NOT NULL,
    file_path       TEXT NOT NULL,
    file_size       BIGINT NULL,
    checksum        VARCHAR(64) NULL,
    mime_type       VARCHAR(128) NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_video_assets_aweme_type (aweme_id, asset_type),
    KEY idx_video_assets_aweme_id (aweme_id),
    KEY idx_video_assets_type (asset_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    aweme_id        VARCHAR(64) NOT NULL,
    channel_id      INT NULL,
    stage           ENUM(
        'download', 'metadata_translate', 'dub',
        'upload_facebook', 'upload_youtube'
    ) NOT NULL,
    status          ENUM(
        'pending', 'processing', 'success', 'failed', 'skipped'
    ) NOT NULL DEFAULT 'pending',
    priority        INT NOT NULL DEFAULT 0,
    attempt_count   INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    locked_by       VARCHAR(64) NULL,
    locked_at       DATETIME NULL,
    started_at      DATETIME NULL,
    finished_at     DATETIME NULL,
    error_message   TEXT NULL,
    result_json     JSON NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_pipeline_jobs_aweme_stage (aweme_id, stage),
    KEY idx_pipeline_claim (stage, status, priority, created_at),
    KEY idx_pipeline_aweme_id (aweme_id),
    KEY idx_pipeline_channel_id (channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS upload_accounts (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    platform            ENUM('facebook', 'youtube') NOT NULL,
    account_name        VARCHAR(255) NOT NULL,
    page_id             VARCHAR(128) NULL,
    youtube_channel_id  VARCHAR(128) NULL,
    access_token        TEXT NULL,
    refresh_token       TEXT NULL,
    token_expires_at    DATETIME NULL,
    app_id              VARCHAR(128) NULL,
    app_secret          VARCHAR(512) NULL,
    client_id           VARCHAR(256) NULL,
    client_secret       VARCHAR(512) NULL,
    api_key             TEXT NULL,
    enabled             TINYINT(1) NOT NULL DEFAULT 1,
    daily_quota         INT NULL,
    notes               TEXT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_upload_accounts_platform_name (platform, account_name),
    KEY idx_upload_accounts_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS upload_records (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    aweme_id            VARCHAR(64) NOT NULL,
    platform            ENUM('facebook', 'youtube') NOT NULL,
    account_id          INT NOT NULL,
    platform_video_id   VARCHAR(128) NULL,
    platform_url        TEXT NULL,
    title_used          TEXT NULL,
    description_used    TEXT NULL,
    tags_used           JSON NULL,
    status              ENUM('pending', 'uploading', 'success', 'failed') NOT NULL DEFAULT 'pending',
    error_message       TEXT NULL,
    uploaded_at         DATETIME NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_upload_records_aweme_platform_account (aweme_id, platform, account_id),
    KEY idx_upload_records_status (status),
    KEY idx_upload_records_platform (platform),
    CONSTRAINT fk_upload_records_account
        FOREIGN KEY (account_id) REFERENCES upload_accounts(id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @fk_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'aweme'
      AND CONSTRAINT_NAME = 'fk_aweme_channel'
);
SET @sql = IF(@fk_exists = 0,
    'ALTER TABLE aweme ADD CONSTRAINT fk_aweme_channel FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE SET NULL ON UPDATE CASCADE',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @fk_exists = (
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'pipeline_jobs'
      AND CONSTRAINT_NAME = 'fk_pipeline_jobs_channel'
);
SET @sql = IF(@fk_exists = 0,
    'ALTER TABLE pipeline_jobs ADD CONSTRAINT fk_pipeline_jobs_channel FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE SET NULL ON UPDATE CASCADE',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
