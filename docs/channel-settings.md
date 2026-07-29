# Channel Settings — UI Specification

Source of truth: `channel_pipeline_configs` (1 row per channel, FK `channel_id → channels.id` `ON DELETE CASCADE`, unique on `channel_id`). Defined in `docs/pipeline-migrate-channel-pipeline-config-v4.sql`; resolved at runtime by `dub_worker/pipeline_config.py::resolve_pipeline_config`. Do not build against `channels.voice_id`/`channels.logo_id` — those are the deprecated pre‑v4 columns, superseded by this table and scheduled for removal.

There is currently no admin API — all edits today are manual SQL (see `docs/channel-pipeline-config.md` §7, `docs/channel-logo-config.md` §10). The UI needs a backing API that reads/writes this table (and creates the row on first edit if a channel predates the migration and has none yet — see "No config row yet" below).

## Settings

| Setting | Key | Type | UI control | Default | Nullable | Validation | Depends on |
|---|---|---|---|---|---|---|---|
| Translation | `translation_enabled` | boolean | Toggle | `true` (`1`) | No | Must be `0`/`1` | — |
| Dubbing | `dubbing_enabled` | boolean | Toggle | `true` (`1`) | No | Must be `0`/`1`; **cannot be `true` while Translation is `false`** (DB `CHECK` + app validation both reject it) | Translation must be on |
| Voice | `voice_id` | integer (FK → `voices.id`) | Select, **populated dynamically from the `voices` table** (`enabled = 1` rows only) — display `voices.name`, never `filename`/id | `NULL` | Yes — `NULL` means "use system default", not "no voice" | Must reference an existing `voices.id`; `ON DELETE SET NULL` | Only meaningful when Dubbing is on; disable/hide when Dubbing is off |
| Logo overlay | `logo_enabled` | boolean | Toggle | `true` (`1`) | No | Must be `0`/`1` | Independent kill-switch — separate from whether a logo is actually selected |
| Logo | `logo_id` | integer (FK → `logos.id`) | Select, **populated dynamically from the `logos` table** (`enabled = 1` rows only) — display `logos.name`, never `filename`/path/`size_px`/id | `NULL` | Yes — `NULL` means "no logo" (there is no system-default logo) | Must reference an existing `logos.id`; `ON DELETE SET NULL` | Grey out when Logo overlay is off |
| Opening hook | `opening_hook_enabled` | boolean | Toggle | `false` (`0`) | No | Must be `0`/`1` | — |
| Daily video limit | `daily_video_limit` | integer | Number input, empty = unlimited | `NULL` (unlimited) | Yes | `NULL` or integer `≥ 0` | None — a general dubbing-pipeline quota enforced in every mode, including no-translation processing |

## Conditional behavior

- **Translation/Dubbing combination.** Only 3 of the 4 combinations are valid: (on, on), (on, off), (off, off). (off, on) is rejected by the DB `CHECK` and by `pipeline_config.validate_pipeline_config_combo`. The UI must disable the Dubbing toggle (or force it off) whenever Translation is off, and should offer re-enabling Dubbing only after Translation is turned back on. Turning Translation off does not require touching Logo/Opening hook — those apply independently of Translation/Dubbing.
- **Voice fallback (no voice explicitly selected, i.e. `voice_id = NULL`).** Resolution order at runtime: per-video override (`aweme.voice_id`, not a channel setting) → this channel's `voice_id` → the catalog's `voices.is_default = 1` row → a hardcoded last-resort voice if even that is missing/invalid. A channel with `voice_id = NULL` is not "no voice" — it always resolves to a real voice. The UI should label the empty selection state as "System default" rather than "None".
- **Logo fallback (no logo explicitly selected, i.e. `logo_id = NULL`, or `logo_enabled = false`).** Unlike voice, there is no system-default logo — the video is simply rendered without a logo overlay. The same "no overlay" outcome also occurs if the selected `logos` row is disabled or its file is missing/unreadable/an unsupported format; the UI does not need to special-case those (they degrade silently and never fail the render), but should reflect `logos.enabled` in the select (e.g. by excluding disabled rows).
- **`daily_video_limit` semantics.** `daily_video_limit` is a general dubbing-pipeline processing quota, independent of `translation_enabled` — it is not a translation-only limit.
  - `NULL` → unlimited; the quota check never runs.
  - `0` → the pipeline processes no videos for that channel that day (every candidate is blocked once `count_today (0) >= 0`).
  - `N > 0` → blocked once that many videos have already been processed for the channel's local calendar day (timezone `DUB_WORKER_TIMEZONE`, default `Asia/Ho_Chi_Minh`).
  - The quota gate applies **regardless of `translation_enabled`** and to every dubbing pipeline mode, including no-translation processing; a channel with translation off still has its quota enforced. Disabling Translation must never clear, reset, or disable this value, and re-enabling Translation must preserve whatever quota was previously configured. The UI keeps the limit input enabled and editable at all times — it must never be hidden, cleared, or forced to `NULL` by the Translation toggle.
- **No config row yet.** A channel created before this table existed may have no `channel_pipeline_configs` row. Until one is created, the pipeline treats it as: Translation on, Dubbing on, Logo overlay on, Opening hook off, unlimited quota, and voice/logo read from the legacy `channels.voice_id`/`channels.logo_id` columns. The UI's first save for such a channel should create the row with these same values as the starting point (not silently default to something else).
- **Dynamic option loading (hard requirement).** Voice and logo dropdowns must be fetched live from the `voices`/`logos` tables at render time. Do not hardcode voice/logo names, filenames, paths, sizes, or ids anywhere in the UI — the catalogs are operator-managed and change independently of any UI release.
