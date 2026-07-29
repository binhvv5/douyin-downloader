-- Add per-channel download_pinned / number_like overrides.
-- Both columns are nullable: NULL means "no channel-level override, use the
-- existing file/global config value or hard-coded default" (see
-- cli/channel_scheduler.py::resolve_channel_config_overrides). This mirrors
-- the file-based `download_pinned` / `number.like` YAML settings exactly —
-- it does not change their meaning.
-- Safe to re-run: skips each column if it already exists.

SET @db := DATABASE();

SET @pinned_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = @db
      AND table_name = 'channels'
      AND column_name = 'download_pinned'
);

SET @sql := IF(
    @pinned_exists = 0,
    'ALTER TABLE channels ADD COLUMN download_pinned TINYINT(1) NULL COMMENT ''NULL=use file/global default; 0/1=explicit channel override''',
    'SELECT ''channels.download_pinned already exists'''
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @number_like_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = @db
      AND table_name = 'channels'
      AND column_name = 'number_like'
);

SET @sql := IF(
    @number_like_exists = 0,
    'ALTER TABLE channels ADD COLUMN number_like INT NULL COMMENT ''NULL=use file/global default; >=0=explicit channel override, same semantics as YAML number.like''',
    'SELECT ''channels.number_like already exists'''
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
