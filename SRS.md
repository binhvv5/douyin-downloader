# SRS — Pipeline xử lý video Douyin (dự án nghiên cứu)

> Đọc trực tiếp trên Git. File SQL tách riêng:  
> [docs/sql/pipeline-schema-v1.sql](docs/sql/pipeline-schema-v1.sql) · [docs/sql/pipeline-migrate-existing-v1.sql](docs/sql/pipeline-migrate-existing-v1.sql)

| Thuộc tính | Giá trị |
|------------|---------|
| Phiên bản | 1.2 |
| Ngày | 2026-07-10 |
| Trạng thái | Draft |
| Loại dự án | Nghiên cứu / thử nghiệm giữa vài thành viên |
| Phạm vi tài liệu | Luồng hệ thống, thiết kế database, DDL, hợp đồng dữ liệu giữa các service |
| Thành viên — Download | Project `douyin-downloader` |
| Thành viên — Dub | Project riêng |
| Thành viên — Upload Facebook | Project riêng |
| Thành viên — Upload YouTube | Project riêng |

---

## 1. Mục đích

Tài liệu mô tả thiết kế chung cho dự án nghiên cứu xử lý video Douyin, gồm 4 service do các thành viên phụ trách:

1. **Download Service** — tải video từ nhiều kênh Douyin, lưu file và metadata, dịch caption (title, mô tả, hashtag) sang tiếng Việt.
2. **Dub Service** — dịch lồng tiếng nội dung video sang tiếng Việt, xuất file video đã dub.
3. **Facebook Upload Service** — upload video đã dub lên Facebook Page/Reels.
4. **YouTube Upload Service** — upload video đã dub lên YouTube.

Các service dùng chung **MySQL** và bảng điều phối `pipeline_jobs`. Đây là dự án nội bộ, ưu tiên triển khai nhanh; token Facebook/YouTube có thể lưu trực tiếp trong database.

---

## 2. Phạm vi

### 2.1 Download Service (`douyin-downloader`)

- Quản lý danh sách kênh Douyin (`channels`).
- Đồng bộ video từ kênh (full hoặc incremental).
- Tải file video gốc (`.mp4`), cover, metadata JSON.
- Dịch metadata caption sang tiếng Việt (`title_vi`, `description_vi`, `tags_vi`).
- Ghi catalog vào `aweme`, đường dẫn file vào `video_assets`.
- Tạo job `pipeline_jobs.stage = 'dub'` khi download thành công.

### 2.2 Các service khác (project riêng)

- **Dub:** transcribe, dịch lồng tiếng, TTS, ghép audio.
- **Upload FB / YT:** upload video, đọc metadata VI từ `aweme`, token lấy từ `upload_accounts`.

---

## 3. Phân công service & bảng dữ liệu

| Service | Input | Output | Bảng phụ trách |
|---------|-------|--------|----------------|
| Download | `channels` enabled | `aweme`, `video_assets`, job `dub=pending` | `channels`, `aweme`, `video_assets` |
| Dub | job `dub=pending`, `source_mp4` | `dubbed_mp4`, job `upload_*=pending` | `video_assets` (dubbed), `pipeline_jobs` |
| Upload FB | job `upload_facebook=pending`, `dubbed_mp4`, metadata VI | `upload_records` | `upload_accounts`, `upload_records` |
| Upload YT | job `upload_youtube=pending`, `dubbed_mp4`, metadata VI | `upload_records` | `upload_accounts`, `upload_records` |
| Chung | — | Điều phối pipeline | `pipeline_jobs` |

---

## 4. Luồng hệ thống

### 4.1 Luồng tổng quan

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌──────────────┐
│  channels   │────▶│ Download Service │────▶│ Dub Service │────▶│ Upload FB    │
│  (n kênh)   │     │                  │     │             │     │ Upload YT    │
└─────────────┘     └──────────────────┘     └─────────────┘     └──────────────┘
                              │                       │                    │
                              ▼                       ▼                    ▼
                     MySQL: aweme              dubbed_mp4           upload_records
                     video_assets             upload jobs
                     pipeline_jobs
                     (dub = pending)
```

### 4.2 Luồng chi tiết — Download Service

```
[Bắt đầu]
    │
    ▼
Đọc channels WHERE enabled = 1
    │
    ▼
Với mỗi channel:
    │
    ├─▶ Gọi Douyin API / browser fallback
    │       Lấy danh sách aweme_id (full hoặc incremental)
    │
    ├─▶ Với mỗi aweme_id:
    │       │
    │       ├─ Đã có file source_mp4 local? ──Yes──▶ Skip download
    │       │                                          (vẫn kiểm tra job dub)
    │       │
    │       └─ No ──▶ Tải .mp4 (+ cover, json)
    │                   │
    │                   ├─ Fail ──▶ aweme.download_status = failed
    │                   │
    │                   └─ Success ──▶ Dịch caption VI (OpenAI)
    │                                   UPSERT aweme
    │                                   UPSERT video_assets (source_mp4, ...)
    │                                   pipeline_jobs(download) = success
    │                                   pipeline_jobs(dub) = pending  ← handoff
    │
    └─▶ Cập nhật channels.last_sync_at
    │
[Kết thúc batch]
```

**Quy tắc:**

- Ghi DB **sau khi tải file video thành công** (không insert trước khi tải).
- Video đã có file local: skip tải; nếu chưa có job `dub` thì vẫn tạo `pipeline_jobs(dub=pending)`.

### 4.3 Luồng chi tiết — Dub Service

```
Claim pipeline_jobs WHERE stage='dub' AND status='pending'
    │
    ▼
Đọc video_assets.source_mp4 + aweme metadata
    │
    ▼
Transcribe (ZH) → Dịch script (VI) → TTS → ffmpeg mux
    │
    ├─ Fail ──▶ pipeline_jobs(dub) = failed
    │
    └─ Success ──▶ INSERT video_assets(dubbed_mp4)
                  pipeline_jobs(dub) = success
                  pipeline_jobs(upload_facebook) = pending
                  pipeline_jobs(upload_youtube) = pending
```

### 4.4 Luồng chi tiết — Upload Services

Hai upload **độc lập**, chạy song song sau khi dub xong:

```
FB Worker:
  Claim stage='upload_facebook'
  → Đọc access_token từ upload_accounts (platform=facebook)
  → Upload dubbed_mp4 + caption từ aweme (title_vi, description_vi, tags_vi)
  → INSERT upload_records (platform=facebook)
  → pipeline_jobs(upload_facebook) = success

YT Worker:
  Claim stage='upload_youtube'
  → Đọc access_token / refresh_token từ upload_accounts (platform=youtube)
  → Upload dubbed_mp4 + metadata VI
  → INSERT upload_records (platform=youtube)
  → pipeline_jobs(upload_youtube) = success
```

Lỗi upload FB **không** chặn upload YouTube.

### 4.5 State machine — pipeline_jobs.status

```
pending ──claim──▶ processing ──ok──▶ success
                      │
                      └──error──▶ failed ──retry──▶ pending
```

### 4.6 State machine — aweme.download_status

```
pending → downloading → success | failed | skipped
```

---

## 5. Hợp đồng dữ liệu giữa các service

### 5.1 Download → Dub

| Trường | Nguồn | Mô tả |
|--------|-------|-------|
| `aweme_id` | `aweme.aweme_id` | ID unique video Douyin |
| `channel_id` | `aweme.channel_id` | FK kênh nguồn |
| `source_video_path` | `video_assets.file_path` | Đường dẫn `.mp4` gốc |
| `title` | `aweme.title` | Caption tiếng Trung gốc |
| `title_vi` | `aweme.title_vi` | Tiêu đề tiếng Việt |
| `description_vi` | `aweme.description_vi` | Mô tả tiếng Việt |
| `tags_vi` | `aweme.tags_vi` | JSON array hashtag VI |
| `create_time` | `aweme.create_time` | Unix timestamp đăng bài |
| `metadata` | `aweme.metadata` | JSON đầy đủ từ API |

Signal: `pipeline_jobs (aweme_id, stage='dub', status='pending')`.

### 5.2 Dub → Upload

| Trường | Mô tả |
|--------|-------|
| `dubbed_video_path` | `video_assets` where `asset_type='dubbed_mp4'` |
| Metadata VI | Đọc từ `aweme` |

Signal: `pipeline_jobs` có `upload_facebook` và `upload_youtube` = `pending`.

### 5.3 Claim job (pattern chung)

```sql
SELECT id, aweme_id, stage
FROM pipeline_jobs
WHERE stage = :stage
  AND status = 'pending'
  AND attempt_count < max_attempts
ORDER BY priority DESC, created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE pipeline_jobs
SET status = 'processing',
    locked_by = :worker_id,
    locked_at = NOW(),
    attempt_count = attempt_count + 1
WHERE id = :job_id;
```

---

## 6. Thiết kế database

Database: `douyin_downloader`, charset `utf8mb4`.

### 6.1 Sơ đồ quan hệ (ER)

```
channels (1) ──────< (N) aweme
                         │
                         ├──────< (N) video_assets
                         │
                         ├──────< (N) pipeline_jobs
                         │
                         └──────< (N) upload_records
                                      │
upload_accounts (1) ───────────────────┘
```

### 6.2 Mô tả bảng

#### `channels` — Download Service

Danh sách kênh Douyin cần đồng bộ.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | INT | NO | PK, auto increment |
| name | VARCHAR(255) | NO | Tên hiển thị nội bộ |
| douyin_url | TEXT | NO | URL trang kênh Douyin |
| sec_uid | VARCHAR(128) | YES | ID kênh Douyin (parse từ URL), unique |
| enabled | TINYINT(1) | NO | 1 = đang sync, 0 = tắt |
| sync_mode | ENUM | NO | `full` = tải toàn bộ; `incremental` = chỉ video mới |
| last_sync_at | DATETIME | YES | Thời điểm chạy sync batch cuối |
| notes | TEXT | YES | Ghi chú |
| created_at | DATETIME | NO | Thời điểm tạo bản ghi |
| updated_at | DATETIME | NO | Thời điểm cập nhật cuối |

#### `aweme` — Download Service

Catalog từng video Douyin đã biết / đã tải. **Mỗi video = 1 dòng**, khóa business là `aweme_id`.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | INT | NO | PK nội bộ, auto increment |
| aweme_id | VARCHAR(64) | NO | ID video Douyin (unique). VD: `7659944934170246757` |
| aweme_type | VARCHAR(32) | NO | Loại nội dung: `video`, `gallery`, ... Mặc định `video` |
| channel_id | INT | YES | FK → `channels.id`, kênh nguồn của video |
| title | TEXT | YES | Caption / mô tả gốc tiếng Trung từ Douyin (thường kèm hashtag) |
| title_vi | TEXT | YES | Tiêu đề tiếng Việt (dịch bởi Download Service, dùng khi upload) |
| description_vi | TEXT | YES | Mô tả / caption tiếng Việt đầy đủ, không hashtag |
| tags_vi | TEXT | YES | JSON array hashtag tiếng Việt. VD: `["Transformers","Optimus Prime"]` |
| author_id | VARCHAR(64) | YES | UID tác giả trên Douyin |
| author_name | VARCHAR(255) | YES | Tên hiển thị tác giả (nickname) |
| author_sec_uid | VARCHAR(128) | YES | sec_uid tác giả |
| create_time | BIGINT | YES | Unix timestamp thời điểm đăng video trên Douyin |
| download_time | BIGINT | YES | Unix timestamp lần ghi DB / cập nhật download cuối |
| download_status | ENUM | NO | `pending`, `downloading`, `success`, `failed`, `skipped` |
| file_path | TEXT | YES | Đường dẫn thư mục chứa file đã tải (folder của video) |
| metadata | LONGTEXT | YES | JSON metadata đầy đủ từ Douyin API (desc, text_extra, video, author, ...) |
| created_at | DATETIME | NO | Thời điểm tạo bản ghi |
| updated_at | DATETIME | NO | Thời điểm cập nhật cuối |

**Index / ràng buộc:**

- UNIQUE (`aweme_id`)
- INDEX (`channel_id`, `author_id`, `download_time`, `download_status`, `create_time`)
- FK `channel_id` → `channels(id)`

**Ví dụ 1 dòng:**

| aweme_id | title | title_vi | download_status |
|----------|-------|----------|-----------------|
| 7659944934170246757 | #变形金刚玩具 ... | Món đồ chơi đỉnh cao... | success |

#### `video_assets` — Download (source) / Dub (dubbed)

Lưu đường dẫn file vật lý theo từng loại asset.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | BIGINT | NO | PK |
| aweme_id | VARCHAR(64) | NO | FK logic → `aweme.aweme_id` |
| asset_type | ENUM | NO | `source_mp4`, `cover`, `music`, `metadata_json`, `transcript_zh`, `transcript_vi`, `dubbed_mp4`, `subtitle_vi` |
| file_path | TEXT | NO | Đường dẫn file tuyệt đối |
| file_size | BIGINT | YES | Kích thước bytes |
| checksum | VARCHAR(64) | YES | SHA256 (optional) |
| mime_type | VARCHAR(128) | YES | VD: `video/mp4` |
| created_at | DATETIME | NO | |
| updated_at | DATETIME | NO | |

Unique: (`aweme_id`, `asset_type`).

#### `pipeline_jobs` — Chung (điều phối)

Mỗi video × mỗi giai đoạn = 1 job.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | BIGINT | NO | PK |
| aweme_id | VARCHAR(64) | NO | Video cần xử lý |
| channel_id | INT | YES | FK → channels (optional) |
| stage | ENUM | NO | `download`, `metadata_translate`, `dub`, `upload_facebook`, `upload_youtube` |
| status | ENUM | NO | `pending`, `processing`, `success`, `failed`, `skipped` |
| priority | INT | NO | Số cao hơn = ưu tiên trước |
| attempt_count | INT | NO | Số lần đã thử |
| max_attempts | INT | NO | Giới hạn retry, mặc định 3 |
| locked_by | VARCHAR(64) | YES | ID worker đang xử lý |
| locked_at | DATETIME | YES | Thời điểm lock |
| started_at | DATETIME | YES | Bắt đầu xử lý |
| finished_at | DATETIME | YES | Kết thúc |
| error_message | TEXT | YES | Lỗi gần nhất |
| result_json | JSON | YES | Kết quả (platform video id, path, ...) |
| created_at | DATETIME | NO | |
| updated_at | DATETIME | NO | |

Unique: (`aweme_id`, `stage`).

#### `upload_accounts` — Upload FB / YT

Cấu hình tài khoản upload. **Token lưu trực tiếp trong DB** để các service đọc nhanh (dự án nghiên cứu nội bộ).

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | INT | NO | PK |
| platform | ENUM | NO | `facebook` hoặc `youtube` |
| account_name | VARCHAR(255) | NO | Tên gọi nội bộ |
| page_id | VARCHAR(128) | YES | Facebook Page ID (chỉ FB) |
| youtube_channel_id | VARCHAR(128) | YES | YouTube channel ID (chỉ YT) |
| access_token | TEXT | YES | Access token hiện tại |
| refresh_token | TEXT | YES | Refresh token (chủ yếu YouTube; FB nếu có) |
| token_expires_at | DATETIME | YES | Hết hạn access_token |
| app_id | VARCHAR(128) | YES | Facebook App ID |
| app_secret | VARCHAR(512) | YES | Facebook App Secret |
| client_id | VARCHAR(256) | YES | Google OAuth Client ID (YouTube) |
| client_secret | VARCHAR(512) | YES | Google OAuth Client Secret |
| api_key | TEXT | YES | API key bổ sung nếu cần |
| enabled | TINYINT(1) | NO | 1 = dùng được |
| daily_quota | INT | YES | Giới hạn upload/ngày, NULL = không giới hạn |
| notes | TEXT | YES | Ghi chú |
| created_at | DATETIME | NO | |
| updated_at | DATETIME | NO | |

Unique: (`platform`, `account_name`).

#### `upload_records` — Upload FB / YT

Log kết quả upload từng video lên từng nền tảng.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | BIGINT | NO | PK |
| aweme_id | VARCHAR(64) | NO | Video đã upload |
| platform | ENUM | NO | `facebook` hoặc `youtube` |
| account_id | INT | NO | FK → `upload_accounts.id` |
| platform_video_id | VARCHAR(128) | YES | ID video trên FB/YT sau upload |
| platform_url | TEXT | YES | URL public |
| title_used | TEXT | YES | Tiêu đề đã gửi lên nền tảng |
| description_used | TEXT | YES | Mô tả đã gửi |
| tags_used | JSON | YES | Hashtag đã gửi |
| status | ENUM | NO | `pending`, `uploading`, `success`, `failed` |
| error_message | TEXT | YES | Lỗi nếu failed |
| uploaded_at | DATETIME | YES | Thời điểm upload thành công |
| created_at | DATETIME | NO | |
| updated_at | DATETIME | NO | |

Unique: (`aweme_id`, `platform`, `account_id`).

### 6.3 Quy ước lưu file

```
{base_path}/channels/{sec_uid}/source/{aweme_id}.mp4
{base_path}/channels/{sec_uid}/source/{aweme_id}_data.json
{base_path}/channels/{sec_uid}/dubbed/{aweme_id}.vi.mp4
```

---

## 7. DDL

```sql
-- =============================================================================
-- SRS Pipeline Schema v1.1 — Dự án nghiên cứu
-- Database: douyin_downloader
-- =============================================================================

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
```

---

## 8. Phụ lục — Ví dụ dữ liệu theo từng giai đoạn

Giả định chung:

- Kênh: `channels.id = 1`, `sec_uid = MS4wLjABAAAA...`, tên **威武帅气的变形金刚**
- Video mẫu: `aweme_id = 7659944934170246757`
- Tài khoản upload: FB `upload_accounts.id = 1`, YouTube `upload_accounts.id = 2`

---

### 8.1 Luồng đầy đủ — Upload thành công cả Facebook và YouTube

**Timeline:**

```
Download OK → Dub OK → Upload FB OK → Upload YT OK  (pipeline hoàn tất)
```

#### Bước 1 — Sau Download Service

**channels:**

| id | name | enabled | last_sync_at |
|----|------|---------|--------------|
| 1 | Kênh Transformers | 1 | 2026-07-10 10:00:00 |

**aweme:**

| aweme_id | channel_id | title (rút gọn) | title_vi | download_status | file_path |
|----------|------------|-----------------|----------|-----------------|-----------|
| 7659944934170246757 | 1 | #变形金刚玩具... | Món đồ chơi đỉnh cao cho đàn ông trưởng thành! | success | /data/channels/MS4w.../source/765994.../ |

**video_assets:**

| aweme_id | asset_type | file_path |
|----------|------------|-----------|
| 7659944934170246757 | source_mp4 | /data/channels/MS4w.../7659944934170246757.mp4 |
| 7659944934170246757 | metadata_json | /data/channels/MS4w.../7659944934170246757_data.json |
| 7659944934170246757 | cover | /data/channels/MS4w.../7659944934170246757_cover.jpg |

**pipeline_jobs:**

| aweme_id | stage | status |
|----------|-------|--------|
| 7659944934170246757 | download | success |
| 7659944934170246757 | metadata_translate | success |
| 7659944934170246757 | dub | pending |

---

#### Bước 2 — Sau Dub Service

**video_assets** (bổ sung):

| aweme_id | asset_type | file_path |
|----------|------------|-----------|
| 7659944934170246757 | dubbed_mp4 | /data/channels/MS4w.../dubbed/7659944934170246757.vi.mp4 |
| 7659944934170246757 | transcript_zh | /data/channels/MS4w.../7659944934170246757.zh.json |
| 7659944934170246757 | transcript_vi | /data/channels/MS4w.../7659944934170246757.vi.json |

**pipeline_jobs:**

| aweme_id | stage | status |
|----------|-------|--------|
| 7659944934170246757 | download | success |
| 7659944934170246757 | metadata_translate | success |
| 7659944934170246757 | dub | success |
| 7659944934170246757 | upload_facebook | pending |
| 7659944934170246757 | upload_youtube | pending |

---

#### Bước 3 — Sau Upload Facebook

**upload_records:**

| aweme_id | platform | account_id | platform_video_id | platform_url | status | uploaded_at |
|----------|----------|------------|-------------------|--------------|--------|-------------|
| 7659944934170246757 | facebook | 1 | 1234567890123456 | https://facebook.com/reel/1234567890123456 | success | 2026-07-10 11:30:00 |

**pipeline_jobs:**

| aweme_id | stage | status |
|----------|-------|--------|
| 7659944934170246757 | upload_facebook | success |
| 7659944934170246757 | upload_youtube | pending |

---

#### Bước 4 — Sau Upload YouTube (pipeline hoàn tất)

**upload_records:**

| aweme_id | platform | account_id | platform_video_id | platform_url | status | uploaded_at |
|----------|----------|------------|-------------------|--------------|--------|-------------|
| 7659944934170246757 | facebook | 1 | 1234567890123456 | https://facebook.com/reel/1234567890123456 | success | 2026-07-10 11:30:00 |
| 7659944934170246757 | youtube | 2 | dQw4abc123xyz | https://youtube.com/watch?v=dQw4abc123xyz | success | 2026-07-10 11:45:00 |

**pipeline_jobs (trạng thái cuối):**

| aweme_id | stage | status |
|----------|-------|--------|
| 7659944934170246757 | download | success |
| 7659944934170246757 | metadata_translate | success |
| 7659944934170246757 | dub | success |
| 7659944934170246757 | upload_facebook | success |
| 7659944934170246757 | upload_youtube | success |

**Sơ đồ trạng thái cuối:**

```
7659944934170246757
  download          ✓ success
  metadata_translate ✓ success
  dub               ✓ success
  upload_facebook   ✓ success  → FB Reel live
  upload_youtube    ✓ success  → YT video live
```

---

### 8.2 Ngoại lệ — Download thất bại (mạng / cookie hết hạn)

**aweme:**

| aweme_id | channel_id | download_status | title_vi |
|----------|------------|-----------------|----------|
| 7659712477299409573 | 1 | failed | NULL |

**video_assets:** *(không có dòng nào)*

**pipeline_jobs:**

| aweme_id | stage | status | error_message |
|----------|-------|--------|---------------|
| 7659712477299409573 | download | failed | Douyin API login required |
| 7659712477299409573 | dub | *(chưa tạo)* | — |

**Xử lý:** Sửa cookie → chạy lại Download → `download` chuyển `pending` → retry.

---

### 8.3 Ngoại lệ — Video đã tải trước đó (skip download, vẫn cần dub)

Video đã có file `.mp4` local từ lần chạy trước, chưa qua dub.

**aweme:**

| aweme_id | download_status | file_path |
|----------|-----------------|-----------|
| 7659389906954803941 | skipped | /data/.../7659389906954803941/ |

**pipeline_jobs:**

| aweme_id | stage | status | Ghi chú |
|----------|-------|--------|---------|
| 7659389906954803941 | download | skipped | File local đã tồn tại |
| 7659389906954803941 | dub | pending | Vẫn tạo job cho Dub Service |

---

### 8.4 Ngoại lệ — Tắt máy giữa chừng khi đang download

Đã tải xong 9/10 video; video thứ 10 đang tải dở (chưa có `.mp4` hoàn chỉnh).

**Sau khi bật máy chạy lại:**

| aweme_id | download_status | Ghi chú |
|----------|-----------------|---------|
| ...001 ~ ...009 | success hoặc skipped | File local có → skip |
| ...010 | *(chưa có dòng aweme hoặc failed)* | Không có `.mp4` → tải lại |

**pipeline_jobs cho video 010:**

| aweme_id | stage | status |
|----------|-------|--------|
| ...010 | download | pending → success (lần chạy mới) |
| ...010 | dub | pending |

---

### 8.5 Ngoại lệ — Dub thất bại, retry thành công

**Lần 1 — Dub lỗi (TTS timeout):**

| aweme_id | stage | status | attempt_count | error_message |
|----------|-------|--------|---------------|---------------|
| 7659010071993612026 | dub | failed | 1 | OpenAI TTS timeout |

**video_assets:** chỉ có `source_mp4`, **chưa có** `dubbed_mp4`.

**Lần 2 — Worker retry (`attempt_count = 2`):**

| aweme_id | stage | status | attempt_count |
|----------|-------|--------|---------------|
| 7659010071993612026 | dub | success | 2 |

Sau đó tạo `upload_facebook` và `upload_youtube` = `pending` như luồng bình thường.

---

### 8.6 Ngoại lệ — Facebook upload OK, YouTube upload FAIL

Dub đã xong; FB thành công; YT lỗi token.

**upload_records:**

| aweme_id | platform | status | error_message | uploaded_at |
|----------|----------|--------|---------------|-------------|
| 7658860120780457125 | facebook | success | NULL | 2026-07-10 12:00:00 |
| 7658860120780457125 | youtube | failed | OAuth token expired | NULL |

**pipeline_jobs:**

| aweme_id | stage | status |
|----------|-------|--------|
| 7658860120780457125 | upload_facebook | success |
| 7658860120780457125 | upload_youtube | failed |

**Xử lý:**

1. Cập nhật `upload_accounts.access_token` / `refresh_token` cho YouTube.
2. Reset `pipeline_jobs` stage `upload_youtube` → `pending` (hoặc worker tự retry nếu `attempt_count < max_attempts`).
3. **Không** upload lại Facebook — `upload_records` FB đã `success`.

---

### 8.7 Ngoại lệ — Facebook FAIL, YouTube OK (độc lập)

| aweme_id | platform | status | error_message |
|----------|----------|--------|---------------|
| 7659343271659201467 | facebook | failed | Page quota exceeded |
| 7659343271659201467 | youtube | success | NULL |

**pipeline_jobs:**

| stage | status |
|-------|--------|
| upload_facebook | failed |
| upload_youtube | success |

YouTube đã live; FB retry sau khi hết quota hoặc sang ngày mới.

---

### 8.8 Ngoại lệ — Cả FB và YT đều fail

| aweme_id | stage | status |
|----------|-------|--------|
| 7658595488355467109 | dub | success |
| 7658595488355467109 | upload_facebook | failed |
| 7658595488355467109 | upload_youtube | failed |

**video_assets:** `dubbed_mp4` **vẫn còn** — không cần dub lại, chỉ retry upload.

---

### 8.9 Ngoại lệ — Chạy lại pipeline cho video đã upload thành công

Worker không nên upload trùng nếu `upload_records.status = success`.

**Kiểm tra trước khi upload:**

```sql
SELECT status FROM upload_records
WHERE aweme_id = '7659944934170246757'
  AND platform = 'facebook'
  AND account_id = 1;
-- Nếu status = 'success' → skip, đánh dấu pipeline_jobs upload_facebook = skipped
```

**pipeline_jobs sau khi skip:**

| aweme_id | stage | status |
|----------|-------|--------|
| 7659944934170246757 | upload_facebook | skipped |

---

### 8.10 Ngoại lệ — Dub xong nhưng thiếu metadata VI

Download lỗi dịch caption (OpenAI key thiếu) nhưng file video vẫn tải được.

**aweme:**

| aweme_id | title_vi | description_vi | download_status |
|----------|----------|--------------|-----------------|
| 7658243040759599035 | NULL | NULL | success |

**pipeline_jobs:**

| stage | status |
|-------|--------|
| metadata_translate | failed |
| dub | pending |

Dub Service vẫn có thể chạy (cần audio). Upload Service nên fallback `title_vi` = `title` hoặc chờ bổ sung metadata.

---

### 8.11 Bảng tóm tắt — Trạng thái pipeline theo scenario

| Scenario | download | dub | upload_fb | upload_yt | Ghi chú |
|----------|----------|-----|-----------|-----------|---------|
| Full success | success | success | success | success | Hoàn tất |
| Download fail | failed | — | — | — | Chưa tạo dub job |
| Skip (đã có file) | skipped | pending | — | — | Handoff dub vẫn OK |
| Dub fail → retry | success | failed→success | pending | pending | Retry dub only |
| FB OK, YT fail | success | success | success | failed | Retry YT only |
| FB fail, YT OK | success | success | failed | success | Retry FB only |
| Both upload fail | success | success | failed | failed | Retry upload, giữ dubbed_mp4 |
| Đã upload rồi | success | success | skipped | skipped | Không upload trùng |

---

### 8.12 Ví dụ nhiều video cùng kênh — trạng thái hỗn hợp

Kênh có 5 video sau một đợt sync:

| aweme_id | download | dub | upload_fb | upload_yt | Mô tả |
|----------|----------|-----|-----------|-----------|-------|
| V001 | success | success | success | success | Xong toàn pipeline |
| V002 | success | success | success | pending | Chờ upload YT |
| V003 | success | pending | — | — | Chờ dub |
| V004 | skipped | pending | — | — | Đã có file, chờ dub |
| V005 | failed | — | — | — | Lỗi download |

---

*Tài liệu baseline cho các thành viên Dub, Upload Facebook và Upload YouTube — đọc/ghi đúng bảng và stage đã thống nhất.*
