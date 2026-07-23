-- SRS Pipeline Schema v1.1 — Dự án nghiên cứu
-- Ref: docs/SRS-douyin-download-pipeline.md
-- Database: douyin_downloader

CREATE DATABASE IF NOT EXISTS douyin_downloader
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE douyin_downloader;

CREATE TABLE IF NOT EXISTS channels (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    douyin_url      TEXT NOT NULL,
    sec_uid         VARCHAR(128) NULL,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    sync_mode       ENUM('full', 'incremental') NOT NULL DEFAULT 'incremental',
    download_batch_size INT NOT NULL DEFAULT 10,
    last_sync_at    DATETIME NULL,
    notes           TEXT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_channels_sec_uid (sec_uid),
    KEY idx_channels_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS aweme (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    aweme_id            VARCHAR(64) NOT NULL,
    aweme_type          VARCHAR(32) NOT NULL DEFAULT 'video',
    channel_id          INT NULL,
    title               TEXT NULL,
    title_vi            TEXT NULL,
    description_vi      TEXT NULL,
    tags_vi             TEXT NULL,
    author_id           VARCHAR(64) NULL,
    author_name         VARCHAR(255) NULL,
    author_sec_uid      VARCHAR(128) NULL,
    create_time         BIGINT NULL,
    download_time       BIGINT NULL,
    download_status     ENUM(
        'pending', 'downloading', 'success', 'failed', 'skipped'
    ) NOT NULL DEFAULT 'pending',
    file_path           TEXT NULL,
    metadata            LONGTEXT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_aweme_id (aweme_id),
    KEY idx_aweme_channel_id (channel_id),
    KEY idx_aweme_author_id (author_id),
    KEY idx_aweme_download_time (download_time),
    KEY idx_aweme_download_status (download_status),
    KEY idx_aweme_create_time (create_time),
    CONSTRAINT fk_aweme_channel
        FOREIGN KEY (channel_id) REFERENCES channels(id)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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
    KEY idx_pipeline_channel_id (channel_id),
    CONSTRAINT fk_pipeline_jobs_channel
        FOREIGN KEY (channel_id) REFERENCES channels(id)
        ON DELETE SET NULL ON UPDATE CASCADE
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
