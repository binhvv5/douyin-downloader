-- DDL cho tính năng Upload FB/YT trong hệ sinh thái pipeline Douyin nhiều dự án
-- (xem D:\Code\GitHub\douyin-doc\SRS-douyin-download-pipeline.md).
-- Project dùng hibernate.ddl-auto: none, không có Flyway đang bật -> chạy tay script này
-- trên DB trước khi start app với nhánh code business/pipeline/*.
--
-- QUAN TRỌNG: `channels`, `aweme`, `video_assets`, `pipeline_jobs` do Download/Dub Service
-- (dự án khác) tạo và ghi trực tiếp - KHÔNG có DDL của các bảng đó ở đây, script này chỉ tạo
-- những gì ban đầu thuộc phạm vi toolhay-service (Upload FB/YT). `upload_accounts`/
-- `upload_records` là bảng dùng chung; DDL của 2 bảng này đã có sẵn trong sql/be_service.sql
-- (dump hiện tại), không lặp lại ở đây. Runtime ownership hiện tại (xem "Ghi chú ownership"
-- bên dưới): `upload_records` cho platform='facebook' nay do worker của
-- douyin-channel-admin đọc/ghi, KHÔNG PHẢI toolhay-service; platform='youtube' vẫn do
-- toolhay-service như trước.

-- Thứ tự DDL là bắt buộc: tạo destination/token trước, sau đó mới tạo mapping tham chiếu nó.
-- Platform converter đã xác nhận (sửa lại 2026-07-26 - bản ghi cũ ở đây bị NGƯỢC):
-- 1 = YouTube, 2 = Facebook. tbl_social_account_token.platform là giá trị authoritative;
-- mapping.platform chỉ là mirror tương thích và phải copy từ token. Cả hai bảng đều RỖNG
-- ở mọi environment đã audit tại thời điểm sửa (2026-07-26) nên không cần migrate dữ liệu -
-- chỉ cần sửa hằng số trong code (channel_admin/platforms.py và dub_worker/platforms.py).
CREATE TABLE IF NOT EXISTS tbl_social_account_token (
    id varchar(32) character set latin1 collate latin1_bin NOT NULL,
    created_by varchar(32) character set latin1 collate latin1_bin,
    created_date datetime,
    last_modified_by varchar(32),
    last_modified_date datetime,
    active tinyint(1),
    platform tinyint NOT NULL,
    account_ref varchar(255) NOT NULL,
    account_label varchar(255),
    avatar_url text,
    follower_count bigint,
    description text,
    access_token text NOT NULL,
    refresh_token text,
    expires_at datetime,
    PRIMARY KEY (id),
    UNIQUE KEY uk_tbl_social_account_token_platform_ref (platform, account_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 1 row = 1 lượt channel Douyin (bảng `channels`, dùng chung) được phép đăng lên 1 tài khoản
-- mạng xã hội. N-N: 1 channel có thể map nhiều tài khoản, 1 tài khoản có thể nhận video từ
-- nhiều channel. Không được đặt UNIQUE riêng trên social_account_token_id; chỉ cặp
-- (channel_id, social_account_token_id) là unique. Mapping không chứa access/refresh token.
-- Dùng `active` để bật/tắt mapping thay vì xoá cứng.
CREATE TABLE IF NOT EXISTS tbl_channel_social_account (
    id varchar(32) character set latin1 collate latin1_bin NOT NULL,
    created_by varchar(32) character set latin1 collate latin1_bin,
    created_date datetime,
    last_modified_by varchar(32),
    last_modified_date datetime,
    active tinyint(1),
    channel_id int NOT NULL,
    social_account_token_id varchar(32) character set latin1 collate latin1_bin NOT NULL,
    platform tinyint NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_channel_social_account (channel_id, social_account_token_id),
    KEY idx_channel_social_account_token (social_account_token_id),
    CONSTRAINT fk_channel_social_account_channel FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_channel_social_account_token FOREIGN KEY (social_account_token_id) REFERENCES tbl_social_account_token (id) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Schema thực tế đã có upload_accounts.social_token_id. Không ADD/ALTER/RENAME cột này ở đây.
-- UploadAccountSyncService phải update cùng tbl_social_account_token row khi token thay đổi,
-- không xoá rồi tạo row/id mới, để mọi mapping hiện có tiếp tục hợp lệ.
-- Quan hệ runtime:
--   tbl_channel_social_account.social_account_token_id
--     -> tbl_social_account_token.id
--     -> upload_accounts.social_token_id

-- Ghi chú ownership (cập nhật 2026-07-26 — Facebook upload đã chuyển sang
-- douyin-channel-admin; xem README.md "Running the Facebook upload worker"):
--   Dub worker (dự án `dubbing`) — producer, KHÔNG gọi Facebook/YouTube API,
--   KHÔNG đọc access_token:
--     - resolve mapping active + token active + upload_accounts.social_token_id
--     - INSERT upload_records(status='pending') per destination (idempotent via
--       unique (aweme_id, platform, account_id); only MySQL 1062 is "already exists")
--     - INSERT pipeline_jobs(stage=upload_facebook|upload_youtube) as wake-up
--       (unique aweme_id+stage; only 1062 is "already exists")
--     - Nếu 1 upload_records mới (pending) được tạo cho 1 stage mà pipeline_jobs
--       của stage đó đã ở trạng thái terminal (success/failed/skipped), job đó
--       được đánh thức lại về 'pending' (attempt_count reset) trong CÙNG
--       transaction finalize — xem dub_worker/db.py::_wake_terminal_platform_job.
--       Không đụng tới job đang 'processing'; không reset upload_records đã success.
--   Facebook upload worker — `channel_admin.workers.facebook_upload_worker`
--   (project `douyin-channel-admin`, KHÔNG PHẢI toolhay-service — xem cảnh báo
--   triển khai trong README.md):
--     - consume upload_records đã có theo aweme_id + platform='facebook'
--     - resolve account_id -> upload_accounts -> social_token_id ->
--       tbl_social_account_token chỉ lúc upload; không bao giờ đọc
--       tbl_channel_social_account (mapping hiện tại) để quyết định destination
--     - cập nhật từng record độc lập; không recreate từ mapping hiện tại
--     - 1 pipeline_jobs(stage='upload_facebook') có thể cover N records; chỉ
--       success khi mọi record thuộc job đó đã success
--     - `finish` chỉ báo hiệu bắt đầu assemble/encode, KHÔNG PHẢI publish
--       thành công — record chỉ success sau khi GET /{video_id}?fields=status
--       xác nhận publishing_phase đã complete và publish_status='published'
--       (xem services/facebook_client.py + README.md "Per-record state machine").
--     - upload_records.platform_video_id được ghi NGAY sau khi Facebook `start`
--       trả về, TRƯỚC khi upload binary/gọi finish (services/facebook_upload.py::
--       _start_new_session) — một record đã có video_id không bao giờ gọi
--       `start` lại; worker query status để resume/reconcile phiên đó, chỉ
--       xoá video_id (rõ ràng, có log) khi status xác nhận phiên đó đã chết.
--   YouTube upload worker (toolhay-service YouTubeUploadJob) — KHÔNG thuộc phạm
--   vi thay đổi này, vẫn giữ nguyên như trước cho stage='upload_youtube'.
--
-- QUAN TRỌNG (vận hành): consumer Facebook cũ trong toolhay-service
-- (`FacebookUploadJob`) KHÔNG được chạy song song với worker mới ở
-- douyin-channel-admin trong cùng môi trường — chỉ một trong hai được sở hữu
-- pipeline_jobs(stage='upload_facebook') tại một thời điểm. Đây là yêu cầu vận
-- hành (do người triển khai đảm bảo), không phải điều được enforce bằng code.
--
-- Cardinality:
--   N active Facebook destinations
--   → N upload_records
--   → 1 pipeline_jobs(stage='upload_facebook') wake-up (nếu architecture hiện tại)
--   → worker (channel_admin) xử lý độc lập cả N records
--
-- Token lifecycle: update cùng tbl_social_account_token row khi token thay đổi.
-- Không xoá rồi tạo id mới — mappings trỏ vào stable id.
--
-- Ghi chú: luồng upload thủ công cũ (tbl_video, tbl_video_upload_task - xem
-- sql/video_upload_feature.sql) đã bị BỎ (2026-07-21). KHÔNG còn đọc
-- tbl_video_upload_task nữa. 2 bảng cũ vẫn còn trong DB nhưng không còn code Java
-- nào tham chiếu tới. Không DROP tự động trong migration này; dữ liệu production
-- chỉ được xoá qua quy trình vận hành riêng có backup.
