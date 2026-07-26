-- Manual apply (MySQL): chuẩn hóa đường dẫn Windows khi INSERT video_assets
--
-- Sai (bản trước): copy file_path2 -> file_path TRƯỚC, rồi mới REPLACE slash
--   → file_path vẫn còn '/', chỉ file_path2 thành '\'
-- Đúng: REPLACE '/' -> '\' trên file_path2 TRƯỚC, rồi copy sang file_path
--   → cả hai cột đều là đường dẫn Windows dùng '\'
--
-- Dùng CHAR(92) thay vì '\\' để tránh lệch escape giữa các client / sql_mode.

USE douyin_downloader;

DROP TRIGGER IF EXISTS trg_video_assets_bi_sync_paths;

DELIMITER $$

CREATE TRIGGER trg_video_assets_bi_sync_paths
BEFORE INSERT ON video_assets
FOR EACH ROW
BEGIN
    -- 1) Chuẩn hóa Windows separator trên file_path2
    IF NEW.file_path2 IS NOT NULL AND NEW.file_path2 LIKE '%/%' THEN
        SET NEW.file_path2 = REPLACE(NEW.file_path2, '/', CHAR(92));
    END IF;

    -- 2) Đồng bộ file_path = file_path2 (đã là '\')
    IF NEW.file_path2 IS NOT NULL AND NEW.file_path2 <> '' THEN
        SET NEW.file_path = NEW.file_path2;
    END IF;
END$$

DELIMITER ;
