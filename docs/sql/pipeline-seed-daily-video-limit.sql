-- Seed / default daily_video_limit for channel_pipeline_configs (download quota per day).
-- Safe to re-run.

INSERT INTO channel_pipeline_configs (channel_id, daily_video_limit)
SELECT c.id, 5
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cfg WHERE cfg.channel_id = c.id
);

UPDATE channel_pipeline_configs
SET daily_video_limit = 5
WHERE daily_video_limit IS NULL;
