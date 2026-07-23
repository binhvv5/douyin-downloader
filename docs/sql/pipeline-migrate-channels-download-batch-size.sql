-- Add per-channel download batch size (undownloaded posts per sync tick).
-- Safe to re-run: skips if column already exists.

SET @db := DATABASE();
SET @exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = @db
      AND table_name = 'channels'
      AND column_name = 'download_batch_size'
);

SET @sql := IF(
    @exists = 0,
    'ALTER TABLE channels ADD COLUMN download_batch_size INT NOT NULL DEFAULT 10 COMMENT ''Max undownloaded posts per sync tick (0=unlimited)''',
    'SELECT ''channels.download_batch_size already exists'''
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
