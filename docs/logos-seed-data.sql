-- logos-seed-data.sql — seed rows in `logos`, one per physical logo file
-- known to exist under logo/ in this repository.
-- Ref: docs/pipeline-migrate-channel-logo-v3.sql (run that first — this
-- assumes the `logos` table already exists), docs/channel-logo-config.md.
-- Usage: mysql -u douyin -p douyin_downloader < docs/logos-seed-data.sql
--
-- filename is the path RELATIVE to DUB_WORKER_LOGO_ROOT, matching this
-- repo's own logo/ layout (e.g. logo/binh/brian_on_the_go.png -> stored as
-- 'binh/brian_on_the_go.png') - see dub_worker/logo_resolver.py for how
-- this is safely joined back onto the configured root at resolution time.
-- This is NOT the same directory tree as voices/ (a flat directory of
-- .wav files resolved by generate_segments.py via its own __file__, no env
-- var) - logo files are resolved via the separately-configured
-- DUB_WORKER_LOGO_ROOT precisely because they may live under nested
-- per-operator/per-brand subdirectories, per the task's own filename rules.
--
-- size_px is left NULL for every row below (no per-logo size override yet)
-- so render_subtitle_pipeline.py's own existing per-layout default size
-- keeps applying - see docs/channel-logo-config.md "Logo size
-- configuration". Set an explicit UPDATE ... SET size_px = <n> once a real
-- per-logo size preference is known, e.g.:
--   UPDATE logos SET size_px = 160 WHERE filename = 'binh/brian_on_the_go.png';
--
-- No channel is assigned a logo by this file - assigning
-- channels.logo_id is a separate, per-deployment operator action (see
-- docs/channel-logo-config.md "Administration"), not part of seeding the
-- reusable logo catalog itself.
--
-- Idempotent: INSERT IGNORE + uq_logos_name (name) / uq_logos_filename
-- (filename) makes this safe to re-run.

INSERT IGNORE INTO logos
    (name, filename, size_px, enabled, description)
VALUES
    ('Brian On The Go', 'binh/brian_on_the_go.png', NULL, 1,
     'Seeded from logo/binh/brian_on_the_go.png. No size_px override yet - uses the pipeline''s own default logo size.');
