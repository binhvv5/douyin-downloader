-- voices-seed-data.sql — seed rows in `voices`, one per file under voices/
-- Ref: docs/pipeline-migrate-voice-config-v2.sql (run that first — this
-- assumes the `voices` table already exists).
-- Usage: mysql -u douyin -p douyin_downloader < docs/voices-seed-data.sql
--
-- filename is the FULL filename including its .wav extension (not the bare
-- name), so it can be passed as-is to run_pipeline.py's --voice argument
-- later (generate_segments.py::resolve_voice_path() accepts a name with
-- .wav already appended, so no code change is needed to consume this).
--
-- Regenerate this file whenever voices/ gains, loses, or renames a file —
-- it is a straight listing of `voices/*.wav` as of this writing:
--   cau_ca.wav, default.wav, fb1.wav, fb2.wav, manh_dung.wav, minh_quan.wav
--
-- target_wps/min_wps/max_wps are left at the table's own defaults
-- (4.30/3.90/4.70, matching run_pipeline.py's current hardcoded values) for
-- every voice below — none of these have been tuned per-voice yet. Adjust
-- with an UPDATE once real per-voice pacing is known, e.g.:
--   UPDATE voices SET target_wps = 4.5, min_wps = 4.1, max_wps = 4.9 WHERE filename = 'fb1.wav';
--
-- speed (native OmniVoice TTS generation-time speed, NOT derived from WPS -
-- see docs/pipeline-migrate-voice-config-v2.sql) defaults every voice to
-- 1.00 (normal speed) per the current requirement; tune per-voice with an
-- UPDATE once real per-voice pacing preferences are known, e.g.:
--   UPDATE voices SET speed = 1.05 WHERE filename = 'fb1.wav';
--
-- Idempotent: INSERT IGNORE + uq_voices_name (name) makes this safe to re-run.
-- Only 'default.wav' is marked is_default = 1 (the system fallback voice) —
-- see the Database V2 design discussion for why exactly one row should ever
-- carry that flag.

INSERT IGNORE INTO voices
    (name, filename, target_wps, min_wps, max_wps, speed, enabled, is_default, description)
VALUES
    ('Default',   'default.wav',    4.4, 4.0, 4.7, 1.00, 1, 1,
     'System fallback voice, seeded to match run_pipeline.py''s pre-existing hardcoded defaults.'),
    ('Cau Ca',    'cau_ca.wav',     4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/cau_ca.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 1',       'fb1.wav',        4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb1.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 2',       'fb2.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb2.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Manh Dung', 'manh_dung.wav', 4.3, 3.9, 4.6, 1.00, 1, 0,
     'Seeded from voices/manh_dung.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Minh Quan', 'minh_quan.wav', 4.3, 3.9, 4.6, 1.00, 1, 0,
     'Seeded from voices/minh_quan.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Anh Khoi', 'anh_khoi.wav', 4.3, 3.9, 4.6, 1.00, 1, 0,
     'Seeded from voices/anh_khoi.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Nguyet Nga', 'nguyet_nga.wav', 4.3, 3.9, 4.6, 1.00, 1, 0,
     'Seeded from voices/nguyet_nga.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Thien Tam', 'thien_tam.wav', 4.1, 3.7, 4.4, 1.00, 1, 0,
     'Seeded from voices/thien_tam.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Phong vien nam', 'phong_vien_nam.wav', 4.6, 4.2, 4.9, 1.00, 1, 0,
     'Seeded from voices/phong_vien_nam.wav. WPS not yet tuned per-voice - uses table defaults.'),
    -- NOTE: (target_wps=3.7, min_wps=3.9) below violates min_wps <= target_wps
    -- (3.9 > 3.7) - this predates the speed column and is unrelated to this
    -- change; left AS-IS (not "fixed" by guessing intended values), but this
    -- row will be REJECTED by chk_voices_wps_order if CHECK constraints are
    -- enforced (MySQL 8.0.16+, confirmed the case for this project) - please
    -- correct target_wps/min_wps/max_wps for 'ngoc_ngan.wav' before running
    -- this file against such a database.
    ('Ngoc Ngan', 'ngoc_ngan.wav', 3.8, 3.5, 4.2, 0.85, 1, 0,
     'Seeded from voices/ngoc_ngan.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Bao Trung', 'bao_trung.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/bao_trung.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 3', 'fb3.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb3.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 4', 'fb4.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb4.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 5', 'fb5.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb5.wav. WPS not yet tuned per-voice - uses table defaults.'),
    ('Voice Meta AI 6', 'fb6.wav', 4.4, 4.0, 4.7, 1.00, 1, 0,
     'Seeded from voices/fb6.wav. WPS not yet tuned per-voice - uses table defaults.');

