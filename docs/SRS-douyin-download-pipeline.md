# SRS — Pipeline xử lý video Douyin (dự án nghiên cứu)

> Bản sao của [SRS.md](../SRS.md) tại root repo (để đọc trên Git).

> **Bổ sung destination mapping (2026-07-26):** Các mô tả cũ bên dưới về việc Dub luôn
> tạo cả hai upload stage và uploader chỉ tạo `upload_records` sau khi upload đã được thay
> thế bởi `docs/pipeline-upload-feature.md`.
>
> **Ownership (cập nhật 2026-07-26 — xem `docs/pipeline-upload-feature.md`):**
> - **Dub worker** (dự án `dubbing`): resolve active mappings lúc finalize thành công; tạo
>   `upload_records(status='pending')` theo từng destination; tạo/wake
>   `pipeline_jobs` theo platform; không gọi Facebook/YouTube API; không đọc token.
> - **Facebook upload worker** (`channel_admin.workers.facebook_upload_worker`, dự án
>   `douyin-channel-admin` — KHÔNG PHẢI toolhay-service): consume `upload_records`
>   (`platform='facebook'`) đã có; load credentials chỉ lúc upload; cập nhật từng
>   record; không recreate record từ mapping hiện tại; không đánh dấu platform job
>   success khi mới xử lý xong một Page trong khi còn record khác.
> - **YouTube upload worker** (toolhay-service `YouTubeUploadJob`): không đổi, vẫn
>   giữ nguyên phạm vi `platform='youtube'` như trước — ngoài phạm vi thay đổi này.
>
> **Cardinality:** N Facebook destinations active → N `upload_records` → 1
> `pipeline_jobs(stage='upload_facebook')` (wake-up) → worker xử lý độc lập cả N
> records. Idempotency: `(aweme_id, platform, account_id)`. Token đổi phải update
> cùng `tbl_social_account_token` row (stable id), không xoá/tạo lại.

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
                  resolve active publish destinations
                  INSERT upload_records(pending) per destination
                  INSERT pipeline_jobs(upload_*) per platform with work
                  pipeline_jobs(dub) = success
                  (no upload jobs if channel has no active destinations)
```

### 4.4 Luồng chi tiết — Upload Services

Hai upload **độc lập**, chạy song song sau khi dub xong. Dub đã tạo sẵn
destination-specific `upload_records` + wake-up `pipeline_jobs`; upload
worker **consume** records, không recreate từ mapping hiện tại:

```
FB Worker (channel_admin.workers.facebook_upload_worker — NOT toolhay-service):
  Claim stage='upload_facebook'
  → SELECT upload_records WHERE aweme_id=? AND platform='facebook'
      AND status requires processing (pending / retryable)
  → For EACH record independently:
      resolve upload_accounts by account_id → social_token_id → token
      upload dubbed_mp4 + caption (title_vi, description_vi, tags_vi)
      UPDATE that upload_records row (remote id/url/status/attempts/error)
  → pipeline_jobs(upload_facebook) = success ONLY when every record
      covered by this job has reached the required successful terminal state
  → if any record remains retryable, leave/requeue the platform job per
      existing retry policy; do not erase other destinations' success

YT Worker (toolhay-service YouTubeUploadJob):
  Claim stage='upload_youtube'
  → same pattern for platform='youtube'
```

Lỗi upload FB **không** chặn upload YouTube. Lỗi một Facebook Page **không**
xoá kết quả success của Page khác trên cùng video.

### 4.5 State machine — pipeline_jobs.status

```
pending ──claim──▶ processing ──ok──▶ success
                      │
                      └──error──▶ failed ──retry──▶ pending
```

`failed ──retry──▶ pending` is never done by the worker itself (it only ever
claims `status='pending'`). Two things can perform this transition:

- **Reaper retry sweep** (automatic): only when `attempt_count < max_attempts`
  on the job, and the effective failure timestamp
  (`COALESCE(finished_at, updated_at)`) is old enough per the configured
  retry delay. See §5.4. There is no dedicated "is this retryable" column -
  `worker.py` classifies at the original failure call site and, for a
  deterministic failure that a retry cannot fix (missing asset, bad input,
  misconfiguration), writes `attempt_count = max_attempts` **in the same
  UPDATE that records the failure**, so the sweep's own
  `attempt_count < max_attempts` filter permanently excludes it - this is the
  v1 mechanism for "terminal, do not auto-retry", reusing the existing
  attempt-count columns instead of adding a new one.
- **Operator** (manual): once the underlying issue has been fixed, resets a
  `failed` job back to `pending` - see §5.4 for the exact reset (it must also
  reset `attempt_count` back down, e.g. to 0, or the job remains permanently
  terminal).

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
| `source_video_path` | `video_assets.file_path` (asset_type=`source_mp4`) | Đường dẫn `.mp4` gốc |
| `audio_path` | `video_assets.file_path` (asset_type=`music`) | **Bắt buộc.** File âm thanh gốc (`.mp3`/`.wav`) dùng làm input cho bước tách vocal/nhạc nền của Dub Service (`separate_audio.py`) - script này không có cách nào tự trích xuất audio từ `.mp4`, nó chỉ tìm file audio đã có sẵn trong `video_folder`. Xem ghi chú bên dưới. |
| `title` | `aweme.title` | Caption tiếng Trung gốc |
| `title_vi` | `aweme.title_vi` | Tiêu đề tiếng Việt |
| `description_vi` | `aweme.description_vi` | Mô tả tiếng Việt |
| `tags_vi` | `aweme.tags_vi` | JSON array hashtag VI |
| `create_time` | `aweme.create_time` | Unix timestamp đăng bài |
| `metadata` | `aweme.metadata` | JSON đầy đủ từ API |

Signal: `pipeline_jobs (aweme_id, stage='dub', status='pending')`.

> **Ghi chú (phát hiện khi tích hợp Dub Worker, chưa có trong bản SRS gốc):**
> Mục §2.1 và ví dụ §8.1 hiện chỉ mô tả Download Service tải `.mp4` + `cover` +
> `metadata_json`, không đề cập việc tải/trích xuất riêng một file audio. Tuy
> nhiên, `asset_type='music'` đã tồn tại sẵn trong enum của `video_assets`, và
> `separate_audio.py` (bước tách vocal/nhạc nền trong pipeline Dub) bắt buộc
> phải có một file audio có sẵn trong `video_folder` (ưu tiên tìm theo thứ tự
> `*_music.mp3` > `*music*.mp3` > `*.mp3` > `*.wav`) - không có phương án nào
> để tự trích xuất từ `.mp4`. Vì vậy Dub Worker coi `music` là **input bắt
> buộc**, đọc trực tiếp từ `video_assets`, không quét thư mục. Nếu Download
> Service hiện chưa ghi asset này, mọi job `dub` sẽ fail ngay ở bước pre-flight
> cho đến khi Download Service được cập nhật để ghi `video_assets(asset_type='music')`
> cho mỗi video đã tải.

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

Worker claim logic is unchanged by the retry mechanism in §5.4 below - it
still only ever selects `status='pending'`, and `attempt_count` is still only
ever incremented here, at claim time.

### 5.4 Reaper — stale reclaim & failed-job retry sweep (Dub)

The reaper (`python -m dub_worker.main reaper`) runs two independent sweeps
on their own configurable intervals. Neither sweep is the worker's claim
query - both only ever *convert eligible rows to `pending`* (or, for the
stale sweep, to `failed` once attempts are exhausted); the worker's own claim
query in §5.3 is what actually picks the row up afterward.

**1. Stale-processing sweep** (unchanged by this feature - heartbeat-based):

```sql
UPDATE pipeline_jobs
SET status = IF(attempt_count >= max_attempts, 'failed', 'pending'),
    locked_by = NULL,
    locked_at = NULL,
    error_message = ...
WHERE stage = 'dub'
  AND status = 'processing'
  AND locked_at < (NOW() - INTERVAL <stale_seconds> SECOND)
```

Detects an **abandoned worker** (crash / reboot / hang past the heartbeat
window). Keyed on `locked_at`, which only a live heartbeat refreshes - a job
that's still actively heartbeating is never touched here, no matter how long
it's been running. Total subprocess runtime is bounded separately by
`DUB_WORKER_JOB_TIMEOUT_SECONDS` inside the worker itself, not by this sweep.

**2. Failed-job retry sweep** (new):

```sql
UPDATE pipeline_jobs
SET status = 'pending',
    locked_by = NULL,
    locked_at = NULL,
    started_at = NULL,
    finished_at = NULL,
    error_message = NULL
WHERE stage = 'dub'
  AND status = 'failed'
  AND attempt_count < max_attempts
  AND COALESCE(finished_at, updated_at) < (NOW() - INTERVAL <retry_delay_seconds> SECOND)
```

Recovers a **completed failure** that is safe to automatically retry. There
is no dedicated "is this retryable" column - `attempt_count < max_attempts`
is the *only* eligibility signal, reusing the existing retry-count columns.
This works because `worker.py` (see §5.4.1 below) writes
`attempt_count = max_attempts` **at the moment it records a deterministic,
non-retryable failure** - in the same `UPDATE` that sets `status='failed'`.
That immediately and permanently excludes the row from this sweep's
`attempt_count < max_attempts` filter, without needing any other schema
change. `COALESCE(finished_at, updated_at)` is the effective failure
timestamp - some failure paths may leave `finished_at` NULL, in which case
`updated_at` (bumped by the same `UPDATE` that set `status='failed'`) is an
equally valid stand-in. `attempt_count` is never *changed* by this sweep
itself - only the worker's own claim (§5.3) increments it, so a retried job
doesn't silently burn an extra attempt just for having been requeued. Jobs
already at `attempt_count >= max_attempts` (whether from ordinary exhaustion
or from a deterministic failure forcing it there) are never touched by this
sweep and stay `failed` for manual investigation.

Both sweeps run inside their own single `UPDATE ... WHERE ...` statement (no
separate `SELECT ... FOR UPDATE` step needed, unlike the claim pattern). This
is what makes them safe under concurrent reaper processes: MySQL's row-level
locking serializes two `UPDATE`s that target the same row, and because the
`WHERE status = 'failed'` predicate is re-checked as part of that same
locked `UPDATE`, a second sweep that acquires the row lock after the first
one has already committed (and moved the row to `pending`) simply matches
zero rows instead of re-applying the transition - it can never double-requeue
or reset a row that's no longer `failed`.

#### 5.4.1 Classifying a failure as terminal (`attempt_count = max_attempts`)

`worker.py` decides `terminal` (whether to force `attempt_count =
max_attempts`) at the exact call site where a failure is recorded - never by
parsing `error_message` after the fact:

| Failure | `terminal` | Why |
|---|---|---|
| No `aweme` row found for the aweme_id | Yes | Deterministic - the DB record itself doesn't exist |
| `source_mp4` video_assets row missing/unreadable/empty | Yes | Deterministic - required input isn't there |
| `music` video_assets row missing/unreadable/empty | Yes | Deterministic - required input isn't there |
| `video_folder` does not exist on disk | Yes | Deterministic - the resolved path is unusable |
| Pipeline subprocess timed out | No | Operational - may be transient load/environment |
| Pipeline subprocess exited non-zero | No | No structured exit-code convention exists yet to tell content/config errors apart from transient ones - stays retryable until `max_attempts` |
| Mandatory output missing/empty despite exit code 0 | No | Could be a transient disk/race condition |
| Unhandled exception in the worker | No | Could be transient infra; `max_attempts` is the backstop |

#### 5.4.2 Manual reset (operator)

To make a terminal `failed` job (or one that's simply exhausted its
attempts) eligible again after fixing the underlying issue:

```sql
UPDATE pipeline_jobs
SET status = 'pending',
    attempt_count = 0,
    locked_by = NULL,
    locked_at = NULL,
    started_at = NULL,
    finished_at = NULL,
    error_message = NULL
WHERE id = :job_id;
```

Resetting `status` alone is not enough - `attempt_count` must also be reset
(e.g. to 0), otherwise the row is still excluded by both the worker's claim
query (§5.3) and the retry sweep's own `attempt_count < max_attempts` filter.

---

## 6. Thiết kế database

Database: `douyin_downloader`, charset `utf8mb4`.

### 6.1 Sơ đồ quan hệ (ER)

```
channels (1) ──────< (N) aweme
    │                    │
    │                    ├──────< (N) video_assets
    │                    │
    │                    ├──────< (N) pipeline_jobs
    │                    │
    │                    └──────< (N) upload_records
    │                                 │
    │                    upload_accounts (1) ───────────────────┘
    │
    └────── (1:1) channel_pipeline_configs  (Database V4, xem §6.2)
                        │
                        ├── voice_id  ──> voices  (nullable FK)
                        └── logo_id   ──> logos   (nullable FK)
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
| download_pinned | TINYINT(1) | YES | Ghi đè cấp kênh cho YAML `download_pinned`. `NULL` = dùng giá trị file/global config hiện có (không phải "tắt"); `0`/`1` = giá trị tường minh, luôn thắng file/global config |
| number_like | INT | YES | Ghi đè cấp kênh cho YAML `number.like` — **giữ nguyên ngữ nghĩa hiện tại** của `number.like` (xem `core/user_modes/base_strategy.py::_collect_paged_aweme`). `NULL` = dùng giá trị file/global config; `0` = giữ đúng ý nghĩa hiện tại của `number.like: 0`, không bị thay bằng default; giá trị âm bị từ chối khi resolve (`ChannelConfigError`) |
| last_sync_at | DATETIME | YES | Thời điểm chạy sync batch cuối |
| notes | TEXT | YES | Ghi chú |
| created_at | DATETIME | NO | Thời điểm tạo bản ghi |
| updated_at | DATETIME | NO | Thời điểm cập nhật cuối |
| voice_id *(Database V2, legacy)* | INT | NO, default 1 | Voice mặc định cho kênh - không có FK ("by request"). **Giữ nguyên**, không xóa - xem `channel_pipeline_configs.voice_id` bên dưới cho lựa chọn per-channel mới (Database V4), và `docs/channel-pipeline-config-plan.md` D13 cho thời điểm có thể xóa cột này. |
| logo_id *(Database V3, legacy)* | INT | YES | Logo overlay cho kênh (nullable FK → `logos.id`). **Giữ nguyên**, cùng lý do như trên. |

**`download_pinned` / `number_like` — fallback order và ví dụ cấu hình.**
Migration: `docs/sql/pipeline-migrate-channels-pinned-and-like.sql` (áp dụng
runtime, idempotent, qua `storage/database.py::_ensure_channels_pinned_and_like_columns`).
Resolved bởi `cli/channel_scheduler.py::resolve_channel_config_overrides` +
`_apply_channel_sync_config`, chạy mỗi tick trước khi gọi `download_url`
(tái sử dụng nguyên vẹn `core/user_downloader.py`/`core/user_modes/*`, không
có code path thứ hai cho pinned-video hay like-threshold).

Fallback order (áp dụng độc lập cho từng field):
1. Giá trị `channels.download_pinned` / `channels.number_like` khác `NULL`.
2. Giá trị hiện có trong file/global config (`download_pinned`, `number.like`
   trong `config.yml`).
3. Hard-coded application default (`download_pinned: false`, `number.like: 0`
   trong `config/default_config.py`).

`config.yml` (file-based, không dùng database polling — không thay đổi):

```yaml
download_pinned: true
number:
  like: 1000
```

Ghi đè cấp kênh qua database (chỉ ảnh hưởng channel đó, các kênh khác vẫn
dùng file/global config ở trên):

```sql
-- Kênh 1: luôn bỏ qua video ghim (pinned), giới hạn 500 video "liked"
UPDATE channels SET download_pinned = 0, number_like = 500 WHERE id = 1;

-- Kênh 2: không ghi đè gì — NULL nghĩa là dùng file/global config, KHÔNG
-- phải "tắt tính năng"
UPDATE channels SET download_pinned = NULL, number_like = NULL WHERE id = 2;

-- number_like = 0 được giữ nguyên ý nghĩa hiện tại của number.like: 0
-- (không bị thay bằng default) — KHÔNG dùng giá trị âm, sẽ bị resolve
-- reject với ChannelConfigError kèm channel_id rõ ràng.
UPDATE channels SET number_like = 0 WHERE id = 3;
```

> Cả `voice_id` và `logo_id` ở trên là các cột được thêm bởi migration
> Database V2/V3 trước đây (nay đã hợp nhất vào
> `docs/pipeline-migrate-channel-pipeline-config-v4.sql` cho một database
> mới hoàn toàn - xem §7 và `docs/channel-pipeline-config-plan.md`). Kể
> từ Database V4, `dub_worker` ưu tiên đọc `channel_pipeline_configs`
> (xem bảng đó bên dưới) và chỉ dùng hai cột trên làm fallback khi kênh đó
> chưa có dòng `channel_pipeline_configs` tương ứng.

#### `channel_pipeline_configs` — Dub Service (Database V4)

Cấu hình pipeline theo từng kênh (1 kênh = tối đa 1 dòng). Xem
`docs/channel-pipeline-config.md` và `docs/channel-pipeline-config-plan.md`
để biết đầy đủ thiết kế, quy tắc, và trạng thái triển khai hiện tại.

| Cột | Kiểu | Null | Mô tả |
|-----|------|------|-------|
| id | INT | NO | PK, auto increment |
| channel_id | INT | NO | FK → `channels.id`, `ON DELETE CASCADE`. Unique - đảm bảo quan hệ 1:1. |
| translation_enabled | TINYINT(1) | NO, default 1 | Bật/tắt ASR + dịch + tạo phụ đề tiếng Việt |
| dubbing_enabled | TINYINT(1) | NO, default 1 | Bật/tắt TTS + dub audio. Yêu cầu `translation_enabled=1` (xem CHECK bên dưới) |
| voice_id | INT | YES | FK → `voices.id`, `ON DELETE SET NULL`. `NULL` = dùng voice mặc định hệ thống |
| logo_id | INT | YES | FK → `logos.id`, `ON DELETE SET NULL`. `NULL` = không có logo |
| logo_enabled | TINYINT(1) | NO, default 1 | Công tắc bật/tắt độc lập, khác với việc `logo_id` có được set hay không |
| opening_hook_enabled | TINYINT(1) | NO, default 0 | Bật/tắt opening hook intro. Mặc định 0, khớp hành vi hiện tại (tắt toàn bộ) |
| daily_video_limit | INT | YES | `NULL` = không giới hạn. **Chỉ là cột schema hiện tại** - chưa có code nào đọc/enforce (xem `docs/channel-pipeline-config-plan.md` §6 cho các quyết định còn treo) |
| created_at | DATETIME | NO | |
| updated_at | DATETIME | NO | |

**Index / ràng buộc:** `UNIQUE(channel_id)`,
`CHECK(translation_enabled=1 OR dubbing_enabled=0)` (tổ hợp
`translation_enabled=0, dubbing_enabled=1` không hợp lệ - không thể dub mà
không dịch trước), `CHECK` cho cả 4 cột boolean chỉ nhận `{0,1}`,
`CHECK(daily_video_limit IS NULL OR daily_video_limit >= 0)`, cùng 3 FK
liệt kê ở trên.

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
| voice_id | INT | YES | Per-video dub voice override (Database V4, `docs/channel-pipeline-config.md` §2.3). `NULL` = không override, dùng `channel_pipeline_configs.voice_id` rồi đến voice mặc định hệ thống. Chỉ có hiệu lực khi `aweme_type='video'`. Không có FK tới `voices.id` (by design, giống `channels.voice_id`/`channels.logo_id`) |
| created_at | DATETIME | NO | Thời điểm tạo bản ghi |
| updated_at | DATETIME | NO | Thời điểm cập nhật cuối |

**Index / ràng buộc:**

- UNIQUE (`aweme_id`)
- INDEX (`channel_id`, `author_id`, `download_time`, `download_status`, `create_time`)
- FK `channel_id` → `channels(id)`

**`voice_id` — lịch sử và tình trạng hiện tại:** migration Database V2 cũ
(`pipeline-migrate-voice-config-v2.sql`, đã xóa) tạo cột này là
`NOT NULL DEFAULT 1`, khiến mọi video — kể cả video không ai chủ động
chọn voice — đều có giá trị khác NULL và luôn override. Database V4
(`docs/pipeline-migrate-channel-pipeline-config-v4.sql`) sửa lại cột
thành `NULL DEFAULT NULL` đúng như thiết kế ban đầu; trên database đã
chạy V2 trước đó, các giá trị hiện có (kể cả `voice_id=1`) được giữ
nguyên, không tự động chuyển thành `NULL` — vì không thể phân biệt "người
dùng chủ động chọn voice 1" với "chưa ai từng set giá trị này".

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
| attempt_count | INT | NO | Số lần đã thử. Cũng đóng vai trò cờ "không tự động retry" - xem §5.4: một lỗi xác định (deterministic) được ghi thẳng `attempt_count = max_attempts` ngay tại lần fail đó, thay vì cần thêm cột riêng. |
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

> **Đây là snapshot gốc v1.1** (bootstrap cho một database hoàn toàn mới) -
> giữ nguyên không sửa theo đúng quy ước "không sửa lịch sử migration, chỉ
> thêm migration mới" của dự án. **Schema hiện tại** = DDL dưới đây **cộng**
> `docs/pipeline-migrate-channel-pipeline-config-v4.sql`, migration duy
> nhất còn cần chạy sau DDL gốc - file này tự tạo `voices`/`logos` (trước
> đây nằm trong hai file riêng, `pipeline-migrate-voice-config-v2.sql` và
> `pipeline-migrate-channel-logo-v3.sql`, cả hai đã bị xóa vì được hợp
> nhất vào v4) **và** `channel_pipeline_configs` (xem §6.2,
> `docs/channel-pipeline-config-plan.md`).
>
> Trên một database mới hoàn toàn: chạy DDL này trước, rồi chạy
> `pipeline-migrate-channel-pipeline-config-v4.sql`. Trên một database
> Download Service đã có sẵn `channels`/`aweme`: dùng
> `pipeline-migrate-existing-v1.sql` thay cho khối DDL này, rồi cũng chạy
> `pipeline-migrate-channel-pipeline-config-v4.sql` (chạy
> `docs/pipeline-preflight-channel-pipeline-config-v4.sql` trước nếu
> database đã từng chạy migration v2/v3 cũ - xem file đó và
> `docs/channel-pipeline-config-plan.md` §4).

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
    download_pinned TINYINT(1) NULL,
    number_like     INT NULL,
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

| aweme_id | stage | status | attempt_count | max_attempts | error_message |
|----------|-------|--------|---------------|--------------|---------------|
| 7659010071993612026 | dub | failed | 1 | 3 | OpenAI TTS timeout |

**video_assets:** chỉ có `source_mp4`, **chưa có** `dubbed_mp4`.

TTS timeout là lỗi tạm thời (operational, xem bảng phân loại §5.4.1) nên
`worker.py` **không** ghi đè `attempt_count` (giữ nguyên = 1, vẫn <
`max_attempts`). Sau `DUB_WORKER_RETRY_DELAY_SECONDS` tính từ
`COALESCE(finished_at, updated_at)`, reaper's failed-job retry sweep (§5.4)
chuyển job này `failed → pending` (vẫn không đổi `attempt_count`); worker
claim lại như bình thường ở lần poll kế tiếp, và chính claim đó mới tăng
`attempt_count`.

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
