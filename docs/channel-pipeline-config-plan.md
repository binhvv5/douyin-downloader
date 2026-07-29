# Implementation plan: `channel_pipeline_configs` (consolidated database migration)

> **Status: PHASE 1/2/3A/3B all IMPLEMENTED on `feature/channel-config`
> (not yet merged).** Phase 3A (flow routing — D9/D10, translation_enabled/
> dubbing_enabled/opening_hook_enabled step routing, worker music/voice
> gating) and Phase 3B (daily-quota enforcement — D3–D6, D12, resolved by
> the Phase 3B task's own requirements rather than left open) are both
> shipped — see the end of §4 Phase 3 below and `dub_worker/README.md`'s
> "Daily translation quota" section for exact current behavior. The
> `video_assets`/mandatory-output label question (D7) was resolved as
> "keep `dubbed_mp4` unconditionally" and did not block either phase.
> Database V4 migration (`docs/pipeline-migrate-channel-pipeline-config-v4.sql`),
> its preflight/verification scripts, backfill, `channel_pipeline_configs`
> schema, application-level validation (`PipelineConfigError` /
> `validate_pipeline_config_row`), compatibility resolution
> (`resolve_pipeline_config`), voice/logo config loading
> (`voice_resolver.py`/`logo_resolver.py`), and both migration integration
> test scenarios (State A: legacy v2/v3 upgrade; State B: clean v1 install)
> are implemented, unit-tested, AND (Revision 5 - this update) actually
> executed against a real MySQL 8.0 server end-to-end (preflight → migrate
> → verify, both states, plus a battery of adversarial incomplete-schema
> cases) — see `docs/channel-pipeline-config.md` and `dub_worker/README.md`
> for current runtime behavior and §13 below for the exact, honest
> executed/skipped test-status breakdown. Revision 5 also replaced the
> integration suite's naive `text.split(";")` SQL splitter with real
> `mysql`-CLI execution, made the preflight script State A/State B safe
> (no more expected `Unknown column` error in State B), strengthened the
> migration's own pre-backfill schema gate to also validate `voices`/
> `logos`/`aweme.voice_id`, and made the integration suite create/drop its
> own disposable per-run database instead of taking a destructive target
> name from an environment variable — none of this changes Phase 1/2's
> approved design, only its correctness and how honestly its test status is
> reported. **Phase 3 (translation/dubbing flow routing, opening-hook
> execution behavior, daily-quota reservation/enforcement, and mode-specific
> `video_assets` output contracts) is now fully implemented** — D3–D6, D9,
> D10, and D12 below are all resolved (see their Status columns). Revision 4
> replaced the separate v2/v3 schema scripts with one consolidated
> migration, which is what actually shipped.

Goal: introduce a single per-channel pipeline configuration table that
controls how every video from a channel is processed (translation on/off,
dubbing on/off, channel-level voice/logo selection, logo/opening-hook
enable flags, and a daily per-channel video limit), without changing the
structure or lifecycle/status semantics of `pipeline_jobs`. Enforcing a
daily limit will necessarily change claim eligibility, so this plan does
not claim that polling behavior remains unchanged.

### Migration-file policy added in Revision 4

- Delete `docs/pipeline-migrate-voice-config-v2.sql`.
- Delete `docs/pipeline-migrate-channel-logo-v3.sql`.
- The new consolidated migration must take over the still-required
  `voices` and `logos` catalog DDL, so deleting v2/v3 does not break clean
  database installation.
- On an existing database, migrate `channels.voice_id` and
  `channels.logo_id` into `channel_pipeline_configs`.
- Preserve every existing `data.sql` file exactly as-is: do not edit,
  rename, merge, regenerate, reorder internally, or delete it.
- Do not modify `docs/pipeline-migrate-existing-v1.sql` or the
  `pipeline_jobs` schema.

---

## 0. Decisions required before implementation (read this first)

This section collects every open decision raised in the rest of the
document, so approval can happen in one pass instead of hunting through
prose.

| # | Decision | Where discussed | Recommendation (not an assumption) | Status |
|---|---|---|---|---|
| D1 | Orphan `channels.voice_id` remediation policy | §4 Phase 1 preflight | Reassign orphan `voice_id` to the current system-default voice's id, then backfill | **Resolved — differently from the recommendation.** Implemented as a hard block, not auto-remediation: the migration's pre-backfill verification procedure (`sp_v4_verify_before_backfill`, `docs/pipeline-migrate-channel-pipeline-config-v4.sql`) `SIGNAL`s and aborts before any backfill if an orphan `voice_id` exists, and the preflight/verify scripts provide diagnostic queries. Nothing is silently reassigned. |
| D2 | Orphan `channels.logo_id` remediation policy | §4 Phase 1 preflight | Set orphan `logo_id` to `NULL` in `channels` (already nullable there), then backfill | **Resolved — same fail-fast policy as D1.** Orphan `logo_id` also blocks the migration via the same verification procedure; no automatic `NULL`-ing occurs. |
| D3 | Meaning of `daily_video_limit = 0` | §6 | Recommend `NULL`=unlimited, `0`=disabled, `>0`=daily max — but this is a **carried-over convention from a different, unrelated, rolled-back feature**, not something the original requirement text defined. Needs explicit sign-off. | **Resolved (Phase 3B).** The Phase 3B task's own requirement text settled this exactly as recommended: `NULL`=unlimited, `0`=no translation jobs that day, `>0`=daily max. |
| D4 | Which event consumes `daily_video_limit` | §6 | Recommend counting a video once when it is first admitted for processing; do not mix `finished_at` for success rows with current in-flight state | **Resolved (Phase 3B).** `count_channel_translation_jobs_today` counts a `'processing'` row and an already-`'success'` row, **both anchored on the same `started_at` timestamp** (never `finished_at`, per this row's own recommendation) — never double-counts the same `pipeline_jobs` row, and a `'failed'`/reclaimed row stops counting immediately (quota is released, not permanently consumed). |
| D5 | Timezone for "daily" | §6 | Recommend reintroducing a `PIPELINE_TIMEZONE`-style env var (default `Asia/Ho_Chi_Minh`) | **Resolved (Phase 3B).** Reintroduced as `DUB_WORKER_TIMEZONE` (default `Asia/Ho_Chi_Minh`) — see `dub_worker/quota.py::compute_business_day_bounds`. |
| D6 | Do combo-3 (no-translate) videos consume the same quota as combo-1 (full dub) videos? | §6 | **Genuinely undecided** — no repo signal either way; flagged, not answered | **Resolved (Phase 3B) — no.** The task's own requirement: quota is applied only when `translation_enabled=true`; combo-3 jobs never check or consume it. |
| D7 | `video_assets.asset_type='dubbed_mp4'` label for non-dub flows | §8 | Recommend keeping the existing label for the first rollout (zero cross-repo impact); flag the semantic mismatch | **Resolved (Phase 3A) — kept as recommended.** `finalize_dub_success` still writes `asset_type='dubbed_mp4'` unconditionally for every combo; no cross-repo Upload-service contract change was made or needed. |
| D8 | New-channel config-row creation mechanism | §11 | Prefer an explicit insert in the service that creates `channels`, in the same transaction; use a DB trigger only if that service cannot be changed | **Resolved as Option A (external dependency), Option C active as temporary rollout protection.** Channel creation is confirmed (by repo-wide search) to be owned entirely by the separate Download Service repository — no local creation path or hidden trigger was added here. §11 documents the required transactional insert as an explicit external dependency; the verification script's reconciliation query (§12) detects channels missing a config row; `resolve_pipeline_config()`'s synthesized legacy fallback remains as temporary rollout protection only, per its own docstring, until that external change lands. |
| D9 | `run_pipeline.py` orchestration gap: `generate_publish_metadata` step currently hard-requires the translated transcript even though the underlying script does not | §9 | Must be fixed before Phase 3 (flow routing) — blocking, not optional | **Resolved (Phase 3A).** The `generate_publish_metadata` `PipelineStep` no longer declares any required `inputs` — it runs under every translation/dubbing combo, sourcing Douyin `*_data.json` (and the best available translated transcript, if any) exactly as `generate_publish_metadata.py` itself already supported. |
| D10 | `run_pipeline.py` orchestration gap: `mux_video` step cannot produce `final_dubbed.mp4` without TTS audio, so `render_subtitle`'s required input does not exist under `dubbing_enabled=False` | §9 | Must be fixed before Phase 3 — blocking, not optional | **Resolved (Phase 3A).** New `mux_passthrough.py`, invoked by `run_pipeline.py`'s `mux_video` step instead of `mux_video.py` whenever `dubbing_enabled=False`: stream-copies the original video+audio (unmodified) into the same `dubbing/output/final_dubbed.mp4` path, so `render_subtitle`'s declared input always exists regardless of combo. |
| D11 | Authoritative channel when `pipeline_jobs.channel_id` is `NULL` or disagrees with `aweme.channel_id` | §7 | Resolve from the job first, fall back to `aweme.channel_id`; reject/log a mismatch instead of silently applying the wrong channel config | **Resolved as recommended.** Implemented in `dub_worker/pipeline_config.py`'s `resolve_effective_channel_id()`: job `channel_id` wins when present, falls back to `aweme.channel_id`, raises `ValueError` on a non-null mismatch. Unit-tested in `dub_worker/tests/test_pipeline_config.py::ResolveEffectiveChannelIdTests`. |
| D12 | Whether quota needs durable admission state | §6 | First try to use an existing durable timestamp. If retries/cross-midnight behavior cannot be represented correctly, approve a small quota-ledger/schema change rather than implementing an unsafe count query | **Resolved (Phase 3B) — no schema change needed.** The existing `pipeline_jobs.started_at` timestamp (the SAME one for both `'processing'` and `'success'` rows - see D4), combined with the atomic per-channel `FOR UPDATE` lock on `channel_pipeline_configs` in `claim_next_dub_job` (run under READ COMMITTED so the count itself always sees the latest committed state - see §6 prose below), is sufficient for correct, race-free accounting. `aweme.channel_id` (v1, already existing) supplies the D11 effective-channel fallback when `pipeline_jobs.channel_id` is NULL. |
| D13 | When to drop legacy `channels.voice_id`/`channels.logo_id` | §4 Phase 4 | Keep them through runtime adoption if fast rollback is required; otherwise drop only after the same migration verifies complete, NULL-safe value parity | **Still open — deliberately deferred, not resolved.** The legacy columns are intentionally retained (not dropped) by the current v4 migration; the migration only ADDs/ALTERs alongside them. Dropping them is out of scope for this change and requires a separate, later migration once Phase 2 has run in production long enough to trust the value-parity verification (§12) with no fallback need remaining. |

D1–D12 are all now resolved as described above (D1/D2 diverge from their
original recommendation; D6 resolves to "no" rather than the earlier
"genuinely undecided" — see each Status column for why). D13 remains open
by deliberate choice, not oversight.

---

## 1. Current schema findings (re-verified against source for this revision)

### `channels` — current deployed state before consolidation

| Migration | Adds |
|---|---|
| `docs/pipeline-migrate-existing-v1.sql` (also `docs/SRS-douyin-download-pipeline.md` §7) | `id, name, douyin_url, sec_uid, enabled, sync_mode, last_sync_at, notes, created_at, updated_at` |
| `docs/pipeline-migrate-voice-config-v2.sql` | `voice_id INT NOT NULL DEFAULT 1` (no FK — "by request", see file header) |
| `docs/pipeline-migrate-channel-logo-v3.sql` | `logo_id INT NULL` + `idx_channels_logo_id` + `fk_channels_logo → logos(id) ON DELETE SET NULL ON UPDATE CASCADE` |

**Verified (not assumed) this revision**: `docs/pipeline-migrate-existing-v1.sql`
lines 160-184 use `INFORMATION_SCHEMA.TABLE_CONSTRAINTS` row-count checks
before adding `fk_aweme_channel`/`fk_pipeline_jobs_channel` — this is the
exact, already-established idempotency pattern this plan's own
post-migration verification (§10) follows, rather than inventing a new
style.

No v4 migration is currently applied or committed (`git log`/`docs/*.sql`
confirmed) — an earlier, differently-shaped, unrelated attempt
(channel-level `dub_enabled`/`daily_dub_limit` + `PIPELINE_TIMEZONE`) was
fully rolled back before being committed, so `v4` is free.

The v2/v3 files explain how the deployed schema reached its current
state, but Revision 4 no longer keeps them as standalone source files.
Their catalog-table DDL must be incorporated into v4 before they are
deleted. All existing `data.sql` files remain byte-for-byte unchanged.

`channels.enabled` (v1) only gates the **Download Service's sync loop**
(`docs/SRS-douyin-download-pipeline.md` §6.2: "1 = đang sync, 0 = tắt"),
unrelated to `translation_enabled`/`dubbing_enabled`.

### `voices` — `docs/pipeline-migrate-voice-config-v2.sql`

`id, name, filename, target_wps, min_wps, max_wps, speed, enabled,
is_default, description, created_at, updated_at`. `UNIQUE(name)`,
`KEY(enabled)`, three `CHECK`s.

### `logos` — `docs/pipeline-migrate-channel-logo-v3.sql`

`id, name, filename, size_px, enabled, description, created_at,
updated_at`. `UNIQUE(name)`, `UNIQUE(filename)`, `KEY(enabled)`,
`CHECK(size_px IS NULL OR size_px > 0)`.

### `video_assets` — `docs/pipeline-migrate-existing-v1.sql`

`asset_type ENUM('source_mp4','cover','music','metadata_json',
'transcript_zh','transcript_vi','dubbed_mp4','subtitle_vi')`. **Verified
this revision**: this is a closed enum — adding a new label requires an
`ALTER TABLE ... MODIFY COLUMN asset_type ENUM(...)`, not just an insert.
Relevant to D7/§8.

### `aweme.voice_id` — per-video override, restored and normalized by v4

**Updated per review item 1** (this is no longer out of scope — the
column's own definition needed to change). The former Database V2
migration (now deleted) created `aweme.voice_id INT NOT NULL DEFAULT 1`,
which meant every row — including ones nobody deliberately set — had a
non-NULL value and therefore always took the per-video-override branch in
`voice_resolver.py`, making the channel-level and system-default fallback
levels structurally unreachable for any video. Since v2 is deleted, v4 now
owns this column directly:

- **Clean install (State B)**: creates it as `INT NULL DEFAULT NULL`.
- **Existing database that ran the former v2 (State A)**: safely `ALTER`s
  the column to `NULL DEFAULT NULL`, state-aware via `INFORMATION_SCHEMA`
  (guarded dynamic SQL — never assumes the column already/never exists).
  **Existing non-NULL values, including rows that only ever had the old
  default of `1`, are left completely unchanged** — the migration cannot
  distinguish "an operator deliberately chose voice id 1" from "nobody
  ever set this," so it never auto-converts `voice_id=1` rows to `NULL`.
  A report query (`SELECT COUNT(*) FROM aweme WHERE voice_id = 1`) is
  included in the preflight/verify scripts so this population is visible
  and can be reviewed separately later.

Final semantics: `aweme.voice_id IS NULL` → no per-video override → fall
through to `channel_pipeline_configs.voice_id`, then the system default
voice. `aweme.voice_id IS NOT NULL` → explicit per-video override, highest
priority (this branch's own logic in `voice_resolver.py` was already
correct — only the column's nullability/default needed restoring). See
`dub_worker/voice_resolver.py`'s module docstring and
`dub_worker/tests/test_voice_resolver.py` for the resolution-order tests,
and `docs/pipeline-migrate-channel-pipeline-config-v4.sql` Step 4 for the
migration SQL itself.

### No ORM anywhere

Confirmed (grep for `sqlalchemy`, `declarative_base`, `class Channel`,
`Repository` — zero real hits). Only hand-written PyMySQL in
`dub_worker/db.py`.

### Pipeline architecture — re-verified this revision with exact line references

**`run_pipeline.py`'s `PipelineStep` declarations (`build_pipeline()`,
lines 373-485) are hard preconditions, not documentation.**
`validate_inputs()` (lines 716-739) raises `FileNotFoundError` before
invoking a step's script if any declared `inputs` path is missing,
distinguishing "producing step exists but was skipped this run" from
"no step produces this file at all". This matters for two concrete gaps:

1. **`generate_publish_metadata` step declares
   `inputs=[_translated_tts_repaired_json(vf)]`** (line 467-468) — i.e.
   `run_pipeline.py`'s orchestration layer will refuse to run this step at
   all if the translated/TTS-repaired transcript JSON does not exist.
   **However**, the underlying script itself (`generate_publish_metadata.py`)
   does **not** actually require it: `resolve_transcript_json()` (line 139)
   returns `Optional[Path]`, is treated as "extra/fallback context" (line
   141's own docstring), and the script's only hard failure condition
   (lines 350-355) is "no Douyin metadata JSON fields **and** no
   transcript" — i.e. it can generate publish metadata (including
   `opening_hook_text`, produced by the same GPT-4.1-mini call, per the
   module's own docstring line 3-4) from the Douyin `*_data.json` file
   alone. **This is a verified orchestration-layer gap (D9), not a script
   limitation** — see §9.
2. **`mux_video` step declares `inputs=[_dubbed_wav(vf),
   _separated_instruments_wav(vf)]`, `outputs=[..., _final_dubbed_mp4(vf)]`**
   (lines 454-461). `mux_video.py`'s own docstring (lines 7-15) confirms it
   mixes TTS-dubbed speech with separated instrumentals — it has no
   passthrough mode for "keep the original audio unchanged." Under
   `dubbing_enabled=False`, none of `tts_first/tts_repair/tts_second/
   merge_audio/separate_audio` would run, so neither `dubbed.wav` nor
   `instruments.wav` would exist, and `mux_video` cannot produce
   `final_dubbed.mp4` in its current form. The next step, `render_subtitle`,
   **declares `inputs=[_final_dubbed_mp4(vf)]`** (line 478) — so under the
   current code, there is **no path that produces the file `render_subtitle`
   requires** once dubbing is skipped. **This is a verified, concrete
   architecture gap (D10)**, not a hypothetical — see §9.

Both gaps block Phase 3 (flow routing) below, not Phase 1/2 of this plan,
but must be documented now per review feedback rather than glossed over.

---

## 2. Proposed final table definition

```sql
CREATE TABLE IF NOT EXISTS channel_pipeline_configs (
    id                     INT AUTO_INCREMENT PRIMARY KEY,
    channel_id             INT NOT NULL,

    translation_enabled    TINYINT(1) NOT NULL DEFAULT 1,
    dubbing_enabled        TINYINT(1) NOT NULL DEFAULT 1,

    voice_id               INT NULL,
    logo_id                INT NULL,
    logo_enabled           TINYINT(1) NOT NULL DEFAULT 1,
    opening_hook_enabled   TINYINT(1) NOT NULL DEFAULT 0,

    daily_video_limit      INT NULL,

    created_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_channel_pipeline_configs_channel_id (channel_id),
    KEY idx_channel_pipeline_configs_voice_id (voice_id),
    KEY idx_channel_pipeline_configs_logo_id (logo_id),

    CONSTRAINT chk_channel_pipeline_configs_valid_combo
        CHECK (translation_enabled = 1 OR dubbing_enabled = 0),

    CONSTRAINT chk_channel_pipeline_configs_boolean_values
        CHECK (
            translation_enabled IN (0, 1)
            AND dubbing_enabled IN (0, 1)
            AND logo_enabled IN (0, 1)
            AND opening_hook_enabled IN (0, 1)
        ),

    CONSTRAINT chk_channel_pipeline_configs_daily_limit
        CHECK (daily_video_limit IS NULL OR daily_video_limit >= 0),

    CONSTRAINT fk_channel_pipeline_configs_channel
        FOREIGN KEY (channel_id) REFERENCES channels(id)
        ON DELETE CASCADE ON UPDATE CASCADE,

    CONSTRAINT fk_channel_pipeline_configs_voice
        FOREIGN KEY (voice_id) REFERENCES voices(id)
        ON DELETE SET NULL ON UPDATE CASCADE,

    CONSTRAINT fk_channel_pipeline_configs_logo
        FOREIGN KEY (logo_id) REFERENCES logos(id)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

Design rationale (unchanged from Revision 1, still valid):

- Own `id` PK + separate `UNIQUE(channel_id)`: matches every other table
  in this schema (all use a surrogate `id` PK), and matches the task's own
  literal phrasing ("add a unique constraint on `channel_id`" only makes
  sense if it isn't already the PK).
- `ON DELETE CASCADE` on `channel_id`: this table is a 1:1 owned/composed
  entity of `channels`, unlike `voices`/`logos` which are shared,
  independently-referenced catalogs (hence their `SET NULL`).
- `voice_id`/`logo_id` nullable + real FK (`SET NULL`): matches `logos`'
  existing pattern. `voice_id` nullable is a deliberate improvement over
  today's `channels.voice_id` (`NOT NULL DEFAULT 1`, no FK, "by request")
  — safe now because a system-default-voice fallback already exists in
  `voice_resolver.py`. **Flagged for confirmation, not silently changed** —
  the backfill (§4) copies existing concrete values verbatim regardless,
  so no existing channel's resolved voice changes as a result.
- `logo_enabled` default `TRUE` / `opening_hook_enabled` default `FALSE`:
  grounded in verified current behavior — today, "has a logo" is
  equivalent to "`logo_id` is set" (no separate toggle exists), so
  `logo_enabled=1` is an inert no-op for every existing channel;
  opening-hook is off everywhere today with zero per-channel exceptions
  (`render_modules/opening_hook_intro.py` docstring: "OFF by default"),
  so `opening_hook_enabled=0` preserves that exactly.

---

## 3. Valid-combination enforcement (schema + application)

| # | translation | dubbing | Behavior | Schema-enforced? |
|---|---|---|---|---|
| 1 | 1 | 1 | Full pipeline | valid |
| 2 | 1 | 0 | Translate+subtitle, keep original audio | valid |
| 3 | 0 | 0 | Skip everything upstream, keep original video+audio, still logo/hook | valid |
| 4 | 0 | 1 | **Rejected** | `CHECK` fails on `INSERT`/`UPDATE` |

Application-level duplicate: `dub_worker/pipeline_config.py::
validate_pipeline_config_combo(translation_enabled, dubbing_enabled) ->
None`, raising `ValueError` for combo 4. This exists because CHECK
enforcement is MySQL-version-dependent (this project already relies on
8.0.16+, but nothing guarantees every environment matches), and because
**this repo has no admin API** — every write to `channels`-adjacent tables
documented so far is manual SQL (`docs/channel-logo-config.md` §10) — so
the CHECK constraint is realistically the *only* enforcement an operator
hand-writing SQL will ever hit. This is an accepted, pre-existing-pattern
risk (§12), not a new gap.

The application validator must also reject boolean values outside `0/1`
and negative `daily_video_limit` values. The valid-combination CHECK alone
does not stop MySQL from accepting values such as
`translation_enabled=2`.

---

## 4. Migration phases (deployment order)

Per review feedback, these are now four **distinct** phases with
materially different risk profiles — the second phase in particular is
**not** "persistence only" and must not be described that way.

### Phase 1 — Consolidated schema migration + legacy-column backfill

Create one canonical file:

`docs/pipeline-migrate-channel-pipeline-config-v4.sql`

It replaces the schema responsibilities of v2/v3 and must support both
starting states:

1. **Existing database:** `voices`, `logos`, `channels.voice_id`, and
   `channels.logo_id` already exist. Create the config table and migrate
   both channel selections without changing their effective values.
2. **Clean database after v1:** v2/v3 are no longer available. Create
   `voices` and `logos` from their former definitions, then create the
   config table directly. Do not add temporary legacy columns to
   `channels`.

Use guarded `INFORMATION_SCHEMA` checks consistent with v1. An unexpected
partial schema must fail loudly; the migration must not guess, overwrite
an incompatible table, or suppress errors.

**Step 1a — Preflight for the existing-database branch: detect orphan
references before they can hit the new FK constraints.** Run these
queries only after `INFORMATION_SCHEMA` confirms that the corresponding
legacy channel column and catalog table exist:

```sql
-- Orphan channels.voice_id (points at a voices.id that no longer exists).
-- channels.voice_id is currently NOT NULL DEFAULT 1 with no FK (v2), so
-- nothing today prevents this from drifting to a dangling id.
SELECT c.id AS channel_id, c.name, c.voice_id
FROM channels c
LEFT JOIN voices v ON v.id = c.voice_id
WHERE v.id IS NULL;

-- Orphan channels.logo_id (points at a logos.id that no longer exists).
-- This one already has a real FK (v3, ON DELETE SET NULL), so it should
-- be empty in a healthy database - but verify rather than assume, since
-- a raw UPDATE could still have bypassed it in principle.
SELECT c.id AS channel_id, c.name, c.logo_id
FROM channels c
LEFT JOIN logos l ON l.id = c.logo_id
WHERE c.logo_id IS NOT NULL AND l.id IS NULL;
```

**If either query returns rows, STOP — remediate before proceeding.**

**Single migration policy (no exception, no alternative mode): any orphan
legacy `channels.voice_id`/`channels.logo_id` reference blocks the ENTIRE
migration until the source data is manually remediated.** There is no
"leave it untouched and exclude that one channel from the backfill"
option — none is implemented anywhere in this repository (an earlier
revision of this document presented that as a selectable alternative; it
was never built, and describing it as available was itself a documentation
defect, since it contradicts the migration's actual all-or-nothing
`SIGNAL`-and-abort behavior — see Step 6 below). Remediate with:

| Orphan | Remediation (the only supported path, D1/D2) | Effect |
|---|---|---|
| `voice_id` | Reassign to the current system-default voice's id (`SELECT id FROM voices WHERE enabled=1 AND is_default=1 ORDER BY id LIMIT 1`), via `UPDATE channels SET voice_id = <default_id> WHERE id IN (<orphans>)` | Repairs a pre-existing data-integrity bug; channel starts resolving to the documented system default, which is what `voice_resolver.py`'s own fallback chain would have used anyway once it reached that priority level |
| `logo_id` | `UPDATE channels SET logo_id = NULL WHERE id IN (<orphans>)` | Already nullable there; matches the "no logo selected" state the FK's own `ON DELETE SET NULL` would have produced had it fired correctly |

Neither remediation is applied by this plan automatically — an operator
must run it explicitly before retrying. The backfill statement (and the
migration's own pre-backfill verification procedure, run before the
backfill even starts) **fails loudly and aborts the entire script** if any
orphan reaches the new FK — see below.

**Step 1b — Catalog and config schema migration** (inside v4):

- Create `voices` if absent, preserving the exact columns, defaults,
  indexes, and CHECK constraints formerly defined by v2.
- Create `logos` if absent, preserving the exact columns, defaults,
  indexes, and CHECK constraints formerly defined by v3.
- Create `channel_pipeline_configs` exactly as in §2.
- If any table exists, verify its complete expected shape;
  `CREATE TABLE IF NOT EXISTS` alone is insufficient.
- Do not alter `pipeline_jobs`, `aweme`, or any `data.sql`.

**Step 1c — Post-migration schema verification** (do not skip; do not
rely on `CREATE TABLE IF NOT EXISTS` alone — see §10 for why and the
exact queries). Run immediately after 1b, before 1d.

**Step 1d — Backfill** (inside the same v4 file, after schema
verification):

```sql
INSERT INTO channel_pipeline_configs
    (channel_id, translation_enabled, dubbing_enabled, voice_id, logo_id,
     logo_enabled, opening_hook_enabled, daily_video_limit)
SELECT
    c.id,
    1,             -- translation_enabled: full pipeline today, for every channel
    1,             -- dubbing_enabled:     full pipeline today, for every channel
    c.voice_id,    -- copy verbatim (remediated per D1 if orphaned)
    c.logo_id,     -- copy verbatim (remediated per D2 if orphaned)
    1,             -- logo_enabled: inert default, behavior still governed by logo_id above
    0,             -- opening_hook_enabled: matches today's universal "off" state
    NULL           -- daily_video_limit: unlimited, matches today's total absence of any limit
FROM channels c
WHERE NOT EXISTS (
    SELECT 1 FROM channel_pipeline_configs cpc WHERE cpc.channel_id = c.id
);
```

Execute that statement only when both legacy channel columns exist. For a
clean v1 database where they do not exist, create default config rows
with `voice_id=NULL` and `logo_id=NULL`; effective voice selection uses
the system-default voice. Use guarded dynamic SQL (or an equivalent
version-compatible mechanism), because static SQL must not reference a
column absent from the clean-install state.

**Changed from Revision 1 per review point 1**: this is a plain
`INSERT ... SELECT ... WHERE NOT EXISTS`, **not** `INSERT IGNORE`.

- `WHERE NOT EXISTS` makes it idempotent/rerunnable (a channel that
  already has a config row is skipped) without needing `IGNORE`'s
  blanket error-suppression.
- Critically, **`INSERT IGNORE` would silently convert a foreign-key
  violation, `CHECK` violation, data-truncation error, or any other
  constraint failure into a warning and skip the row** — meaning an
  orphaned `voice_id`/`logo_id` (§ Step 1a) would be silently dropped
  from the new table with no operator-visible signal, leaving that
  channel's config row simply missing (or worse, present with a
  different value than intended) with no error raised. A plain `INSERT`
  fails the **entire statement** (InnoDB rolls back the statement's
  effects) the moment it hits a constraint violation — forcing the
  orphan to be remediated (Step 1a) before the backfill can succeed at
  all. This is the intended, safer failure mode: loud and blocking, not
  silent and partial.

### Phase 2 — Runtime adoption / dual-read (CODE CHANGES — this is a real behavior change, not "persistence only")

**This phase changes what `dub_worker` actually does, and must not be
described as low-risk persistence work.** Concretely:

- New `dub_worker/pipeline_config.py::resolve_pipeline_config(conn,
  channel_id) -> PipelineConfig` — see §7 for the full design. This
  becomes the **single** place that decides voice/logo/enable-flag
  resolution, replacing today's scattered `db.py::fetch_channel_voice_id`
  / `db.py::fetch_channel_logo_id` call sites inside `voice_resolver.py`
  and `logo_resolver.py`.
- `voice_resolver.resolve_voice_for_aweme`'s step-2 (channel-level
  override) is **refactored** to read `voice_id` from the resolved
  `PipelineConfig` instead of issuing its own `fetch_channel_voice_id`
  query. This is a **control-flow change** inside an existing, tested
  function — not an additive, risk-free change.
- `logo_resolver.resolve_logo_for_channel` is **refactored** the same way
  for `logo_id`, **and gains a new gate**: a channel with `logo_id` set
  but the resolved `logo_enabled=0` will **newly stop applying its logo**
  — a genuine, user-visible behavior change for any channel an operator
  sets `logo_enabled=0` on (though a no-op for every channel until an
  operator does so, since backfilled default is `1`).
- Both resolvers keep their existing MySQL 1054/1146 "schema not present"
  catch (backward compatibility with a pre-V4 database), **and** gain a
  **new** "row not found for this `channel_id`" fallback (a *data* gap,
  not a *schema* gap) that synthesizes the same defaults `db.py` would
  have used before this feature existed — see §7 for the exact fallback
  contract (all seven fields, not just voice/logo).
- This phase must ship with its own regression tests proving the
  **existing** `test_logo_resolver.py`/`test_runner_logo_argv.py` suites
  still pass unmodified, plus new tests for the dual-read paths (§13).

### Phase 3 — Flow routing + quota enforcement

**Phase 3A (flow routing) is now IMPLEMENTED** on `feature/channel-config`:
`translation_enabled`/`dubbing_enabled` are resolved by the worker and
passed explicitly to `run_pipeline.py` (`--translation-enabled`/
`--dubbing-enabled`, always forwarded one way or the other — never left to
that script's own CLI defaults), which routes its own step selection
accordingly (§3's three valid combos), with D9/D10 fixed as described in
§0's table above. `dub_worker/worker.py` no longer requires/resolves the
music asset or a voice when `dubbing_enabled=False`, and the mandatory
final-output path selection accounts for combo 3 (translation disabled, no
logo/opening-hook) never producing a new `render_subtitle` output — see
`mux_passthrough.py` and `dub_worker/worker.py`'s own comments for the
exact mechanics. `video_assets.asset_type='dubbed_mp4'` is kept unconditionally
for every combo (D7's recommendation, applied as-is — no cross-repo Upload
contract change was needed).

**Phase 3B (daily-quota enforcement) is now IMPLEMENTED** on
`feature/channel-config`, with D3–D6 and D12 resolved by that task's own
requirement text (see §0/§6's tables above for each). `daily_video_limit`
now actually gates `db.py::claim_next_dub_job`:

- Quota applies only when `translation_enabled=1` (D6); `NULL`=unlimited,
  `0`=blocks every translation job that day, `>0`=a real daily max (D3).
- "Day" is the local calendar day in `DUB_WORKER_TIMEZONE` (default
  `Asia/Ho_Chi_Minh`, D5), computed in pure Python
  (`quota.compute_business_day_bounds`, no MySQL timezone-table
  dependency).
- Admission is counted once per `pipeline_jobs` row: `'processing'` AND
  already-`'success'` rows are counted **together, both anchored on the
  SAME `started_at` timestamp** (set once per claim/retry, never mutated by
  heartbeat, and never touched again once a row succeeds - deliberately
  NOT `finished_at` for `'success'`, since that would let a job admitted
  just before a local-day boundary silently vanish from the day it was
  actually admitted under the instant it finishes after that boundary,
  incorrectly freeing up an already-spent slot). A `'failed'`/reclaimed row
  stops counting the instant its status moves off `'processing'` (D4) — no
  new ledger/column was needed (D12): the existing `started_at` timestamp
  alone is sufficient.
- Effective-channel accounting (D11): a candidate is matched by the same
  rule `pipeline_config.resolve_effective_channel_id` uses — its own
  `pipeline_jobs.channel_id` when present, else `aweme.channel_id` (see
  `db.fetch_aweme_channel_id`) — never treated as quota-unconstrained just
  because `pipeline_jobs.channel_id` happens to be NULL. A
  resolved-via-`aweme` channel is persisted into `pipeline_jobs.channel_id`
  at admission time (same `UPDATE` that claims the job), and
  `count_channel_translation_jobs_today` additionally matches via
  `COALESCE(pipeline_jobs.channel_id, aweme.channel_id)` as a safety net
  for any row that predates that persist — without both halves, two
  different NULL-`channel_id` jobs resolving to the same `aweme`-level
  channel could together exceed its `daily_video_limit`.
- Concurrency: `fetch_channel_quota_config_for_update` locks the candidate's
  effective channel's `channel_pipeline_configs` row (`FOR UPDATE`) before
  counting, serializing two concurrent claims for the same channel so the
  second always sees the first's already-committed admission — implemented
  exactly as this section previously specified. This whole transaction runs
  under **READ COMMITTED** (set explicitly for just this one transaction),
  so the `aweme.channel_id` fallback lookup and the quota count — both
  plain, non-locking reads — always see the latest committed state at the
  moment each runs, rather than a snapshot MySQL's default REPEATABLE READ
  would otherwise pin them to from before the `FOR UPDATE` lock wait even
  began; a locking read (`FOR SHARE`) was tried for this instead and
  rejected, since on a table small enough that the optimizer skips its
  indexes for a full scan it can deadlock against a concurrent
  `SELECT ... FOR UPDATE SKIP LOCKED` candidate-select (see `db.py`'s own
  docstrings). A quota-blocked candidate is left untouched (still
  `pending`, `attempt_count` unchanged, `channel_id` not persisted either)
  and the next-highest-priority candidate is tried instead, in the same
  transaction. **`pipeline_jobs`'s own schema, `status` enum, and state
  machine (`pending → processing → success/failed`) did not change at
  all** — only the **set of channels/jobs considered eligible to be
  claimed** changed, exactly as this section anticipated.

See `dub_worker/quota.py`, `dub_worker/db.py`, and `dub_worker/README.md`'s
"Daily translation quota" section for the full implementation and
operator-facing behavior.

### Phase 4 — Legacy-column and migration-file cleanup

Repository cleanup is explicitly part of this change:

- Delete `docs/pipeline-migrate-voice-config-v2.sql` after v4 has taken
  over its required `voices` DDL.
- Delete `docs/pipeline-migrate-channel-logo-v3.sql` after v4 has taken
  over its required `logos` DDL.
- Keep every existing `data.sql` file unchanged.

Dropping the old channel columns is controlled by D13:

- If a maintenance-window migration is acceptable, v4 may backfill,
  verify parity, remove the old logo FK/index, and finally drop
  `channels.voice_id`/`channels.logo_id` in one migration.
- If runtime dual-read must ship first, retain the columns temporarily
  and drop them in a small forward cleanup migration after Phase 2 is
  verified. The v2/v3 files are still removed because v4 owns catalog
  creation for clean installations.

Never drop either legacy column before orphan preflight succeeds, every
channel has exactly one config row, and NULL-safe value-parity checks
return zero mismatches.

---

## 5. Constraints, FKs, indexes, deletion behavior — summary table

| Object | Type | Behavior | Rationale |
|---|---|---|---|
| `uq_channel_pipeline_configs_channel_id` | UNIQUE | enforces 1:1 | explicit requirement |
| `fk_channel_pipeline_configs_channel` | FK, `ON DELETE CASCADE` | config deleted when channel deleted | owned/composed entity |
| `fk_channel_pipeline_configs_voice` | FK, `ON DELETE SET NULL` | reference cleared, row survives | matches `logos`' pattern; fallback chain exists |
| `fk_channel_pipeline_configs_logo` | FK, `ON DELETE SET NULL` | reference cleared, row survives | matches `logos`' pattern exactly |
| `chk_channel_pipeline_configs_valid_combo` | CHECK | rejects combo 4 | explicit requirement; version-dependent, app-level duplicate required |
| `idx_channel_pipeline_configs_voice_id` / `_logo_id` | KEY | FK support, explicit | matches v3's explicit-index style |

No index proposed for `daily_video_limit`/`translation_enabled`/
`dubbing_enabled` alone — table size equals channel count, and every
access pattern is a single-row point lookup by `channel_id` (already
covered).

---

## 6. Daily-limit semantics — every field requiring explicit approval

**Resolved by the Phase 3B task's own requirement text** (superseding this
section's earlier recommendations where they differ) — see
`dub_worker/quota.py` and `dub_worker/db.py::claim_next_dub_job`/
`fetch_channel_quota_config_for_update`/`count_channel_translation_jobs_today`
for the implementation, and `dub_worker/README.md`'s "Daily translation
quota" section for the operator-facing summary.

| Field | Decision needed | Recommendation | Status |
|---|---|---|---|
| `daily_video_limit = 0` meaning | Does `0` mean "processing disabled today" or something else? | `0` = disabled, `NULL` = unlimited, `>0` = daily max | **Resolved (D3)** — implemented exactly as recommended: `NULL` unlimited, `0` blocks every translation job that day, `>0` a real daily max (`quota.decide_translation_quota`). |
| Timezone | Which timezone defines "a day"? | Reintroduce a `PIPELINE_TIMEZONE`-style setting (default `Asia/Ho_Chi_Minh`) | **Resolved (D5)** — reintroduced as `DUB_WORKER_TIMEZONE` (default `Asia/Ho_Chi_Minh`), an IANA zone name; day boundaries computed in pure Python (`zoneinfo`, no MySQL timezone-table dependency) via `quota.compute_business_day_bounds`. |
| Counting event | Which event counts? | Count once at first admission/claim, using one durable timestamp or ledger entry consistently for every later status | **Resolved (D4/D12)** — counted via `pipeline_jobs.status`: a `'processing'` row AND an already-`'success'` row, **both anchored on the SAME `started_at` timestamp** (set once per claim/retry, never mutated by heartbeat, never touched again once a job succeeds) — deliberately not `finished_at` for `'success'` rows, exactly as this row's own recommendation specifies ("do not mix `finished_at` for success rows with current in-flight state"). No new ledger/column needed — see D12 row below for why the existing schema is sufficient. |
| Do `processing` jobs reserve quota? | Yes/no | Yes — reserve atomically before/while claiming | **Resolved (D4)** — yes; `fetch_channel_quota_config_for_update`'s `FOR UPDATE` lock on the channel's `channel_pipeline_configs` row serializes concurrent claims for that channel, so a fresh count is always read before each admission decision. |
| Do `failed` jobs consume quota? | Yes/no | Recommend yes if the limit controls processing/API cost; recommend no if it controls successful output volume. This is a business decision, not derivable from the repository. | **Resolved (D4) — no.** The Phase 3B task requires "failed or interrupted jobs must not permanently consume quota": a `'failed'` row (or one the reaper reclaims back to `'pending'`) stops being counted the instant its status moves off `'processing'`, releasing its reservation with no extra bookkeeping. |
| How are retries counted? | Same `pipeline_jobs` row across attempts, or per-attempt? | Per-**row**, not per-attempt: `pipeline_jobs` already enforces `UNIQUE(aweme_id, stage)` (verified, v1 DDL) — a retried job is the *same* row (its `status`/`attempt_count` change in place), never a new row, so counting rows rather than attempts already prevents double-counting a retried job | **Verified structural guarantee, not a new decision** — see next row |
| How is one video prevented from being counted twice? | — | Structurally guaranteed today by `pipeline_jobs`'s existing `UNIQUE KEY uq_pipeline_jobs_aweme_stage (aweme_id, stage)` (v1 DDL) — a given `aweme_id` can only ever have one `stage='dub'` row, so any counting query keyed on distinct `pipeline_jobs` rows (not distinct `aweme_id` values, which would be redundant here) is automatically video-unique | **Verified, not open** |
| Do combo-3 (no-translate, no-dub) videos consume the same quota as combo-1 videos? | Yes/no/different limit | **No recommendation** — a case can be made either way (combo 3 does far less GPU/API work than combo 1, arguably shouldn't compete for the same quota; but it still occupies exactly one `stage='dub'` `pipeline_jobs` row, which is what any straightforward counting query would key on) | **Resolved (D6)** — by the Phase 3B task's own requirement: quota applies ONLY when `translation_enabled=1`, so combo-3 (translation disabled) videos never consume or check quota, regardless of `daily_video_limit`. |
| Does `daily_video_limit` belong on this table vs. a separate scheduling table? | — | Keep it here — per-channel, avoids a second 1:1 table for one column | Recommendation, low-risk, easy to split out later if needed |

**Concurrency mechanism (D4/D12) — implemented as recommended**: a
candidate's effective channel's `channel_pipeline_configs` row is locked
(`FOR UPDATE`) before the day's count is read and the admission decision
made, all inside the same transaction as the job claim itself
(`db.claim_next_dub_job`), run under READ COMMITTED so that lock's freshness
guarantee actually reaches the (plain-read) quota count too — a
quota-blocked candidate is left untouched (still `pending`, `attempt_count`
unchanged, `channel_id` not persisted either) and the next-highest-priority
candidate is tried instead. Fast, mocked coverage of a two-worker "final
remaining slot" race lives in `dub_worker/tests/test_quota_claim.py::
TwoWorkersCompetingForFinalSlotTests` (two sequential `claim_next_dub_job`
calls with the second reflecting the first's already-committed admission);
`dub_worker/tests/test_quota_claim_mysql.py` additionally proves this for
real, with genuinely concurrent threads/connections against a real MySQL
server (both with an ordinary non-NULL `pipeline_jobs.channel_id` and with
NULL `pipeline_jobs.channel_id` on both candidates, resolved only via
`aweme.channel_id`) — the project's fast-test convention otherwise avoids
requiring a live MySQL server for *routine* test runs (see
`dub_worker/tests/test_db_pipeline_config_integration.py` for the separate,
larger migration-integration suite), but this one property — that MySQL's
own row locking and read-visibility rules, not just Python call order,
are what prevent the race — genuinely cannot be proven any other way.

No new migration was required for any of this — `channel_pipeline_configs
.translation_enabled`/`.daily_video_limit` (Database V4) and
`pipeline_jobs.status`/`.started_at`/`.channel_id` and `aweme.channel_id`
(v1) were
already sufficient for correct, atomic accounting.

---

## 7. Single config resolver (replaces scattered reads)

**Changed from Revision 1 per review point 6**: rather than adding one
narrow `fetch_channel_pipeline_config` and leaving `voice_resolver.py`/
`logo_resolver.py` to separately patch in dual-read logic for just
`voice_id`/`logo_id`, this plan specifies **one** resolver that produces a
**complete** `PipelineConfig` per job, with an explicit, complete fallback
contract:

```python
@dataclasses.dataclass(frozen=True)
class PipelineConfig:
    channel_id:           int
    source:                str   # "channel_pipeline_configs" | "legacy_fallback"
    translation_enabled:   bool
    dubbing_enabled:       bool
    voice_id:              Optional[int]
    logo_id:               Optional[int]
    logo_enabled:           bool
    opening_hook_enabled:   bool
    daily_video_limit:      Optional[int]


def resolve_pipeline_config(conn, channel_id: Optional[int]) -> PipelineConfig:
    """
    ONE call site for every field this feature introduces. Never returns a
    partial object - if channel_pipeline_configs has no row for this
    channel (missing data, not missing schema), EVERY field below is
    synthesized from legacy behavior, not just voice_id/logo_id:

        translation_enabled  = True     (full pipeline, today's only behavior)
        dubbing_enabled       = True     (full pipeline, today's only behavior)
        voice_id               = channels.voice_id   (legacy column, read directly)
        logo_id                 = channels.logo_id     (legacy column, read directly)
        logo_enabled             = True     (today, "has logo_id" IS the enable flag)
        opening_hook_enabled     = False    (off everywhere today, no exceptions)
        daily_video_limit        = None     (no limit exists anywhere today)

    Same MySQL 1054/1146 "schema not present" catch as voice_resolver.py/
    logo_resolver.py for a pre-V4 database (source="legacy_fallback" either way).
    """
```

Before calling the resolver, the worker must determine the effective
channel. `pipeline_jobs.channel_id` is nullable in v1, while
`fetch_aweme_context()` independently returns `aweme.channel_id`. Revision
2 used only the job value and would synthesize a no-channel fallback even
when the aweme row had a valid channel. Required rule:

```python
effective_channel_id = job.channel_id or context.channel_id
```

If both values are non-null and different, treat this as a data-integrity
problem: log both and fail deterministically unless another policy is
explicitly approved. Do not silently apply one channel's configuration to
another channel's video. Load `PipelineConfig` once with the effective id
and pass it to voice/logo resolution. The existing per-aweme voice override
remains highest priority.

`voice_resolver.py`/`logo_resolver.py` are refactored to call this once
(already true today that both are called from the same connection/phase
in `worker.py::_handle_job`, so this is not an extra round trip — it
replaces two narrower queries with one broader one) and read their
respective fields off the returned `PipelineConfig`, rather than issuing
their own `channels`-scoped queries. **This is a Phase 2 code change (§4),
not a Phase 1 schema-only change** — it is explicitly not described as
"persistence only" per review feedback, since it alters control flow
inside two existing, previously-verified functions.

---

## 8. Downstream impact: success validation and `video_assets`

**`dubbed_mp4` cannot remain universally mandatory — three distinct,
separable problems, verified against source:**

1. **File-production problem (blocking, D10 — see §1/§9)**: under
   `dubbing_enabled=False`, the current `mux_video` step cannot run (no
   `dubbed.wav`/`instruments.wav` to mix), so `final_dubbed.mp4` — the
   file `render_subtitle` requires as input, and the file
   `discovery.resolve_mandatory_output()` currently validates as the
   job's mandatory final output — would not exist at all. This is a
   `run_pipeline.py`-level gap, not a database-level one, but it directly
   determines whether `worker.py`'s existing "mandatory output must
   exist and be non-empty" check (`discovery.check_file`) has anything to
   check.
2. **Storage-label problem (D7)**: `dub_worker/db.py::finalize_dub_success`
   currently **unconditionally** writes `video_assets(asset_type=
   'dubbed_mp4')` on every successful dub job. Under combo 2/3, the audio
   was never re-dubbed (original audio kept), so labeling the resulting
   file `'dubbed_mp4'` is semantically misleading, even once problem 1 is
   solved by some future passthrough mechanism. `video_assets.asset_type`
   is a closed `ENUM` (verified §1) — adding a new value (e.g.
   `'processed_mp4'`) is itself a schema change, and per
   `docs/SRS-douyin-download-pipeline.md` §5.2, the separate Upload
   Facebook/YouTube services (different repositories) read
   `video_assets WHERE asset_type='dubbed_mp4'` to find what to upload —
   so introducing a new label requires **cross-repo coordination**, out
   of this repo's control. **Recommendation (not decided)**: keep writing
   `'dubbed_mp4'` regardless of combo for the first rollout (zero
   cross-repo impact, preserves the existing Upload contract unchanged),
   accepting the semantic mismatch, and revisit the enum/contract change
   later if it becomes a real problem for operators reading the table.
3. **Mandatory-ness problem**: even once problem 1 is solved, is a final
   video file still mandatory for combo 3 (which does nothing but
   logo/hook + original audio/video)? Recommendation: yes — some file
   still needs to reach the Upload services, so "a final video must exist
   and be non-empty" stays mandatory in all three valid combos; only its
   *provenance* (freshly muxed vs. a passthrough copy of `source_mp4`)
   differs. This does not change `worker.py`'s existing validation
   *logic*, only what upstream step produces the file it validates —
   again a `run_pipeline.py`-level concern, not this plan's.

None of these three problems are solved by this plan — they are
identified, precisely separated, and flagged as blocking Phase 3 (§4),
per review feedback that they must not be left undiscussed.

---

## 9. `opening_hook_text` dependency in no-translate mode (D9)

**Verified, not assumed, this revision** (see §1's line-by-line citations):

- The underlying `generate_publish_metadata.py` script **can** produce
  `opening_hook_text` (and the rest of publish metadata) from the Douyin
  `*_data.json` file alone — the translated transcript is optional
  fallback/extra context, not a hard requirement (`resolve_transcript_json`
  returns `Optional[Path]`; the script's only hard failure is "neither
  source available"). Since the Douyin metadata JSON is written by the
  separate Download Service independent of any Dub-side translation step
  (per `docs/SRS-douyin-download-pipeline.md` §2.1/§8.1), it should
  already exist for every video regardless of a channel's
  `translation_enabled` setting.
- **However**, `run_pipeline.py`'s own orchestration wrapper currently
  hard-blocks this: the `generate_publish_metadata` `PipelineStep`
  declares `inputs=[_translated_tts_repaired_json(vf)]` (line 467-468),
  and `validate_inputs()` (lines 716-739) raises `FileNotFoundError`
  before ever invoking the script if that file is missing — which it
  would be, entirely, under `translation_enabled=False` (translate/
  repair_words/tts_first/tts_repair/tts_second all skipped).

**Conclusion**: no-translate mode's "still apply the configured opening
hook" requirement is **achievable in principle** (the script supports
it), but is **currently blocked by a specific, identified line in
`run_pipeline.py`'s step declaration**, not by any fundamental limitation.
This is a required, scoped code fix for Phase 3 (§4) — declared here as an
explicit dependency per review feedback, not left as an unstated
assumption.

---

## 10. Schema verification (do not rely on `CREATE TABLE IF NOT EXISTS` alone)

**Changed from Revision 1 per review point 10.** `CREATE TABLE IF NOT
EXISTS` only guarantees the table exists with *some* shape — it does
**not** detect a partial prior run that created the table with a
different column set, missing constraints, or a different default (e.g.
an interrupted migration, or a manually-created table with the same
name). Run the following immediately after Phase 1b, before Phase 1d's
backfill:

```sql
-- 1. Columns, types, nullability, defaults
SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
ORDER BY ORDINAL_POSITION;
-- Expect exactly: id, channel_id, translation_enabled, dubbing_enabled,
-- voice_id, logo_id, logo_enabled, opening_hook_enabled, daily_video_limit,
-- created_at, updated_at - in that order, with defaults matching §2.

-- 2. Indexes (PK, unique, plain)
SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE, SEQ_IN_INDEX
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs'
ORDER BY INDEX_NAME, SEQ_IN_INDEX;
-- Expect: PRIMARY (id), uq_channel_pipeline_configs_channel_id (channel_id,
-- NON_UNIQUE=0), idx_channel_pipeline_configs_voice_id,
-- idx_channel_pipeline_configs_logo_id.

-- 3. CHECK constraints present and attached to this table
SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
  ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
 AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA = DATABASE()
  AND tc.TABLE_NAME = 'channel_pipeline_configs';
-- Expect valid-combo, boolean-domain, and non-negative-limit checks.

-- 4. Foreign keys present, pointing at the right tables, right ON DELETE rule
SELECT
    kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME,
    kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME,
    rc.DELETE_RULE, rc.UPDATE_RULE
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
 AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = 'channel_pipeline_configs'
  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL;
-- Expect exactly 3 rows:
--   fk_channel_pipeline_configs_channel -> channels.id,  DELETE_RULE=CASCADE
--   fk_channel_pipeline_configs_voice   -> voices.id,    DELETE_RULE=SET NULL
--   fk_channel_pipeline_configs_logo    -> logos.id,     DELETE_RULE=SET NULL

-- 5. Engine/charset (matches project convention)
SELECT ENGINE, TABLE_COLLATION
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'channel_pipeline_configs';
-- Expect: InnoDB, utf8mb4_unicode_ci.
```

Any deviation from the expected rows above means Phase 1b did not
complete as intended — stop and reconcile before running the backfill,
rather than discovering the gap later via a data problem.

---

## 11. New-channel config-row creation (D8)

`channels` rows are created by the **separate Download Service repository**
(per `docs/SRS-douyin-download-pipeline.md` §2.1/§3) — this repo does not
own that code and "update the channel-creation transaction" cannot be
literally executed from here. Two options, presented for approval rather
than one silently chosen:

| Option | Mechanism | Pros | Cons |
|---|---|---|---|
| A (**preferred**) | Download Service inserts the channel and config row in one transaction | Explicit, testable behavior; no hidden trigger or missing-row window | Requires a coordinated change in the repository that owns channel creation |
| B (fallback only) | `AFTER INSERT` trigger on `channels` | Covers every writer without deployment ordering | Hidden DB behavior; harder to test/version; can conflict with an application insert |
| C (rollout safety only) | Rerunnable `WHERE NOT EXISTS` backfill plus synthesized legacy fallback | Safe during phased deployment | Does not guarantee every new channel immediately has a persisted row |

**Resolved (D8): Option A, documented as an external dependency — not
implemented in this repo.** No local channel-creation path and no hidden
database trigger were added here: channel creation is confirmed (by a
repo-wide search for `INSERT INTO channels`, which finds only this repo's
own tests) to be entirely owned by the separate Download Service
repository. This document records the required change as an explicit,
clearly-labeled **external dependency**: that service must insert the
`channels` row and a default `channel_pipeline_configs` row
(`translation_enabled=1`, `dubbing_enabled=1`, `voice_id`/`logo_id` =
migrated/selected legacy voice/logo when available, `logo_enabled=1`,
`opening_hook_enabled=0`, `daily_video_limit=NULL`) in the same database
transaction, rolling back channel creation if the config insert fails.
Until that lands, Option C is the active mitigation: the verify script's
reconciliation query (§12) diagnoses any channel missing a config row, and
`resolve_pipeline_config()`'s synthesized legacy fallback (§7) keeps such
a channel's videos processing with correct behavior in the meantime. That
fallback is explicitly **not** the permanent creation strategy and should
be removed once the Download Service change lands and the reconciliation
query returns zero rows in production for a full rollout window.

---

## 12. Verification queries and rollback / forward-fix strategy

### Post-backfill verification (Phase 1, after Step 1d)

```sql
-- Row-count parity
SELECT
    (SELECT COUNT(*) FROM channels) AS channel_count,
    (SELECT COUNT(*) FROM channel_pipeline_configs) AS config_count;
-- Expect equal (or config_count lower only if some channels were
-- deliberately excluded per an orphan-remediation decision in §4 Step 1a).

-- Value-parity spot check: every backfilled row's voice_id/logo_id must
-- match the (possibly just-remediated) legacy channels columns exactly
SELECT c.id, c.voice_id AS legacy_voice_id, cpc.voice_id AS new_voice_id,
       c.logo_id AS legacy_logo_id, cpc.logo_id AS new_logo_id
FROM channels c
JOIN channel_pipeline_configs cpc ON cpc.channel_id = c.id
WHERE c.voice_id <> cpc.voice_id
   OR NOT (c.logo_id <=> cpc.logo_id);   -- <=> is MySQL's NULL-safe equality
-- Expect zero rows.

-- Every existing channel's *effective* combo is the zero-behavior-change default
SELECT COUNT(*) FROM channel_pipeline_configs
WHERE NOT (translation_enabled = 1 AND dubbing_enabled = 1
           AND logo_enabled = 1 AND opening_hook_enabled = 0
           AND daily_video_limit IS NULL);
-- Expect 0 immediately after backfill (before any operator customizes a row).
```

### Rollback strategy (Phase 1 only — schema/data, no code deployed yet)

If D13 retains the legacy columns through runtime adoption, rollback is
simple because their original selections remain available:

```sql
-- Manual rollback, NOT auto-executed by any migration file, matching this
-- repo's existing convention (see v2/v3's own trailing rollback comments).
DROP TRIGGER IF EXISTS trg_channels_after_insert_pipeline_config;  -- if Option A (§11) was implemented
DROP TABLE IF EXISTS channel_pipeline_configs;
-- Never drop voices/logos: unchanged data.sql rows may depend on them.
```

If D13 drops the legacy columns in v4, rollback must first recreate the
columns, copy values back from `channel_pipeline_configs`, restore the
former logo index/FK where required, verify parity, and only then drop
the config table. Retaining the columns through the runtime-adoption
deployment is therefore safer when rapid rollback matters.

### Forward-fix strategy (Phase 2 onward — once code is deployed)

Once Phase 2's dual-read code ships, a **schema rollback is the wrong
tool** for fixing a bug found in production — the safer path is a
**code-level forward-fix or revert-deploy** of the Phase 2 change,
leaving the table and its data in place (the table is provably a
superset of legacy behavior when every row matches the zero-behavior-
change defaults, so its mere existence is never itself the source of a
regression). Recommended approach once implemented: gate Phase 2's new
resolution logic behind a simple feature check (e.g. an environment
variable or a one-line code toggle) so it can be disabled without a full
deploy/rollback cycle, falling back to the exact pre-Phase-2 code path
(direct `channels.voice_id`/`channels.logo_id` reads) while the root
cause is investigated. This mirrors how this repo already treats a
missing/invalid config as non-fatal everywhere else (voice/logo
resolution never fails a job outright) — extending that same philosophy
to "the new resolution code path itself misbehaving" is a natural fit
that avoids ever needing to revert Phase 1's schema.

---

## 13. Test plan — implemented (Phase 1/2/3A/3B)

Following this repo's existing `unittest`-based, no-pytest convention
(`dub_worker/tests/`). Everything below, including the Phase 3A/3B items,
is implemented and passing as of this revision — see `dub_worker/tests/
test_quota.py`, `test_quota_claim.py`, `test_quota_claim_midnight_sqlite.py`,
and `test_quota_claim_mysql.py` for the Phase 3B quota suites specifically.

**Pure/DB-free (`dub_worker/tests/test_pipeline_config.py`):**

- `validate_pipeline_config_combo`: all 4 combos — 1/2/3 pass, combo 4
  raises `ValueError`.
- `PipelineConfig` legacy-fallback defaults match §7's contract exactly,
  for **all seven** fields (not just voice/logo) — encoded as constants
  any resolver/backfill code can import and assert against.
- `validate_pipeline_config_row` (review item 7): every boolean field
  rejects non-`{0,1}` values (including `2`, `-1`, `None`) before any
  `bool(...)` conversion happens; invalid translation/dubbing combinations
  rejected; negative `daily_video_limit` rejected; non-positive
  `voice_id`/`logo_id` rejected; a `channel_id` mismatch between the row
  and the caller-supplied id rejected.
- `resolve_pipeline_config` schema-error handling (review item 6): missing
  `channel_pipeline_configs` table (MySQL 1146) falls back to legacy;
  present-but-missing-a-column (1054) raises `PipelineConfigError` and
  proves it does **not** fall back to legacy; an unrelated DB error
  propagates unchanged regardless of which pymysql exception class carries
  it.

**Pure/DB-free (`dub_worker/tests/test_voice_resolver.py`, new this
revision — review item 1's explicit test-coverage requirement):**

- Resolution order: `aweme.voice_id` (video-type only) beats
  `channel_pipeline_configs.voice_id` beats `voices.is_default=1` beats the
  hardcoded fallback, at every fall-through combination (NULL override,
  missing aweme row, non-`video` `aweme_type`, disabled/invalid candidate
  at each level).
- The additive `pipeline_config` parameter is used instead of an extra
  `db.fetch_channel_voice_id` call when supplied, without changing
  resolution order or precedence versus the aweme-level override.

**Real-MySQL integration (`dub_worker/tests/test_db_pipeline_config_integration.py`),
gated behind `DUB_WORKER_TEST_DB_*` env vars, split into isolated scenarios
that never mix contradictory assertions against one schema. As of this
revision, this suite has actually been EXECUTED against a real MySQL 8.0
server (see this change's own verification report for the exact command
and result) - not merely written and inspected:**

- `StateAMigrationTests` (legacy upgrade: v1 → legacy v2/v3-shaped schema,
  simulated in `setUpClass` → v4): existing `channels.voice_id`/`logo_id`
  copied correctly into `channel_pipeline_configs`; existing
  `voices`/`logos` rows preserved; `aweme.voice_id` becomes nullable with
  `DEFAULT NULL`; an existing non-NULL per-video override is unchanged;
  an existing `voice_id=1` row (indistinguishable from "never set") is
  left unchanged, not converted to `NULL`; a **new** `aweme` row inserted
  *after* migrating defaults to `NULL`; re-running the migration does not
  duplicate config rows or clobber a since-customized row; an orphan
  `voice_id`/`logo_id` blocks the **entire** migration with a clear SQL
  error and diagnostic text (never silently excludes the channel), and the
  standalone preflight script blocks on the same condition too; a full
  preflight → migrate → verify workflow test; plus the state-agnostic
  schema-shape/FK/CHECK/cascade/`resolve_pipeline_config` tests (valid in
  either final state, so not duplicated in State B).
- `StateBMigrationTests` (clean install: v1-only schema, no legacy
  columns/tables at all, simulated in `setUpClass` → v4 directly): `voices`,
  `logos`, `aweme.voice_id`, and `channel_pipeline_configs` all created
  correctly with no dependency on the now-deleted v2/v3 files (asserted by
  confirming those files don't exist and the migration still succeeds);
  backfill succeeds with no legacy columns present; a new `aweme` row
  defaults to `voice_id=NULL`; defaults/constraints match the approved
  design; the migration is idempotent; a full preflight → migrate → verify
  workflow test, including confirming the preflight script produces no
  `Unknown column` error in this state.
- `SchemaVerificationFailureTests` (adversarial): missing required column,
  incorrect nullability, incorrect default, an index with the correct name
  but the wrong column, a CHECK constraint with the correct name but the
  wrong expression, a foreign key with an incorrect delete rule, a partial
  `voices` schema, a partial `logos` schema, and an incorrectly-defined
  `aweme.voice_id` (nullable but with the wrong default) each independently
  make the migration's own pre-backfill verification procedure
  (`sp_v4_verify_before_backfill`) `SIGNAL` and abort **before any row is
  backfilled** — proves "fail loudly on a broken schema" is real, not just
  claimed, for every one of these cases, not just a single missing-column
  example.
- Real `mysql`-CLI execution (`dub_worker/tests/sql_runner.py`): every
  migration/preflight/verify `.sql` file is executed through the real
  `mysql` client (never a naive `text.split(";")` or a hand-rolled
  splitter that doesn't understand comments/strings/procedures) - see
  `dub_worker/tests/test_sql_runner.py` for pure/DB-free unit tests
  proving semicolons inside comments, strings, and a `DELIMITER`-switched
  stored-procedure body do not incorrectly split a statement.
- Database safety: `setUpModule()` connects to the server with NO database
  selected, generates a fresh `dub_worker_test_<32 hex chars>` name itself
  (never taken from an environment variable), and creates/drops exactly
  that one database for the whole run - it is structurally impossible to
  point this suite at `douyin_downloader` or any other pre-existing
  database. Missing `DUB_WORKER_TEST_DB_HOST` is an ordinary skip; a
  configured user lacking `CREATE DATABASE` privilege is also a skip, with
  a precise, actionable reason - never a fallback to some other database.

**Phase 3 items, now implemented and executed against real MySQL:**

- With two concurrent workers and one remaining daily slot, exactly one
  claim succeeds — `test_quota_claim_mysql.py::TwoConnectionFinalQuotaSlotTests
  ::test_two_workers_racing_for_the_final_slot_exactly_one_admits` (plus its
  NULL-`pipeline_jobs.channel_id` variant), against a real MySQL 8.0 server.
- Full flow-routing tests for `translation_enabled=0`/`dubbing_enabled=0`
  combinations actually changing `run_pipeline.py`'s behavior — see
  `dub_worker/README.md`'s Phase 3A section for the specific suites.
- Admission-time quota-consumption freezing (a job's `translation_enabled`
  snapshot at claim time is never re-derived from a later config change,
  same day): `test_quota_claim.py::QuotaTranslationEnabledMarkerAtAdmissionTests`,
  `test_quota_claim_midnight_sqlite.py::AdmissionTimeQuotaMarkerTests`, and
  `test_quota_claim_mysql.py`'s
  `test_translation_disabled_admission_is_never_counted_after_enabling_translation_same_day`/
  `test_translation_enabled_admission_still_counts_after_config_changes`.
- One authoritative post-lock admission timestamp (business-day bounds and
  `pipeline_jobs.started_at` from the same value, never a pre-lock Python
  clock read): `test_quota_claim.py::MidnightLockWaitTests`.

**Still not written (out of scope for this repo):**

- If Option A (§11/D8) lands in the Download Service: an integration test
  there (not in this repo) that channel creation inserts the config row in
  the same transaction.

### Verification report (Revision 5 - this update)

Executed, not merely inspected:

- `python -m unittest dub_worker.tests.test_sql_runner -v` → **20 tests,
  0 failures.**
- `python -m unittest dub_worker.tests.test_pipeline_config dub_worker.tests.test_voice_resolver dub_worker.tests.test_logo_resolver dub_worker.tests.test_runner_logo_argv -v` →
  **102 tests, 0 failures.**
- `python -m unittest dub_worker.tests.test_worker_pipeline_config_failures -v` →
  **9 tests, 0 failures.**
- `python -m unittest discover -s dub_worker/tests -p "test_*.py"` (full
  package, real-MySQL suite skipped - no test DB configured in that
  environment) → **131 tests, 0 failures, 1 skipped** - no regressions in
  any pre-existing suite.
- `DUB_WORKER_TEST_DB_HOST=127.0.0.1 DUB_WORKER_TEST_DB_PORT=3306 DUB_WORKER_TEST_DB_USER=root DUB_WORKER_TEST_DB_PASSWORD=*** python -m unittest dub_worker.tests.test_db_pipeline_config_integration -v`,
  against a real MySQL 8.0.46 server, using a freshly generated/dropped
  disposable database → **42 tests, 0 failures, 0 errors** (315.5s). This
  single run covers `StateAMigrationTests` (25), `StateBMigrationTests` (8),
  and `SchemaVerificationFailureTests` (9) - i.e. both migration states end
  to end (preflight → migrate → verify) plus the full adversarial
  incomplete-schema battery, all in one execution.
- Confirmed after the run: the disposable `dub_worker_test_<hex>` database
  created for the run no longer exists on the server (`tearDownModule`'s
  `DROP DATABASE` ran cleanly).

This same real run is what found and fixed several genuine defects a
never-executed test suite could not have caught (see
`dub_worker/README.md`'s verification section for the specific list) -
the fixes are in the test file itself, not in the migration's approved
design.

---

## 14. File-by-file change list

| File | Change | Phase | Status |
|---|---|---|---|
| `docs/pipeline-migrate-channel-pipeline-config-v4.sql` | Consolidate required `voices`/`logos` DDL; create config table; state-aware `aweme.voice_id` restoration; pre-backfill schema/data verification via a temporary stored procedure (now also validates `voices`/`logos`/`aweme.voice_id` shape, not just `channel_pipeline_configs`); guarded backfill | 1 | **Implemented, executed against real MySQL** (D13 column-drop deliberately deferred — see §0) |
| `docs/pipeline-preflight-channel-pipeline-config-v4.sql` | Pre-migration diagnostic queries (orphan detection, legacy-value report, `voice_id=1` report); state-aware (State B returns informational skip results instead of raising `Unknown column`); no "exclude orphan channel" option (none exists) | 1 | **Implemented, executed against real MySQL in both states** |
| `docs/pipeline-verify-channel-pipeline-config-v4.sql` | Post-migration reconciliation queries: exactly-one-config-per-channel, no dangling references, legacy/migrated value parity, `aweme.voice_id` check | 1 | **Implemented, executed against real MySQL in both states** |
| `docs/pipeline-migrate-voice-config-v2.sql` | **Deleted** — required `voices` DDL incorporated into v4 | 4 | Done |
| `docs/pipeline-migrate-channel-logo-v3.sql` | **Deleted** — required `logos` DDL incorporated into v4 | 4 | Done |
| Every existing `data.sql` | **No change to filename or content** | all | Confirmed unchanged (`git diff` shows none) |
| `dub_worker/pipeline_config.py` | `PipelineConfig` dataclass, `validate_pipeline_config_combo`, `validate_pipeline_config_row`, `PipelineConfigError`, `resolve_effective_channel_id`, `resolve_pipeline_config` | 2 | **Implemented** |
| `dub_worker/db.py` | Add `fetch_channel_pipeline_config` (and the `aweme`/voice/logo fetch helpers `voice_resolver.py`/`logo_resolver.py` consume) | 2 | **Implemented** |
| `dub_worker/voice_resolver.py` | Consumes the additive, optional `pipeline_config` parameter for the channel-level step; `aweme.voice_id` resolution-order logic unchanged (was already correct — only the column's own definition needed restoring, see §1) | 2 | **Implemented** |
| `dub_worker/logo_resolver.py` | Consumes `PipelineConfig`; `logo_enabled` gate | 2 | **Implemented** |
| `dub_worker/worker.py` | Catches `PipelineConfigError` and records it as a terminal (non-retryable) job failure without changing `pipeline_jobs` schema/lifecycle | 2 | **Implemented** |
| `dub_worker/tests/test_pipeline_config.py` | Pure/DB-free tests, including schema-error and validation coverage | 2 | **Implemented, executed, passing** |
| `dub_worker/tests/test_voice_resolver.py` | Pure/DB-free resolution-order tests | 2 | **Implemented, executed, passing** |
| `dub_worker/tests/sql_runner.py` (new) | Real `mysql`-CLI script executor + a robust (never naive) fallback multi-statement SQL splitter | 1 | **Implemented, executed, passing** |
| `dub_worker/tests/test_sql_runner.py` (new) | Pure/DB-free tests proving the splitter handles semicolons in comments/strings/procedures correctly | 1 | **Implemented, executed, passing** |
| `dub_worker/tests/test_db_pipeline_config_integration.py` | Real-MySQL tests, split into State A / State B / adversarial-schema classes; creates/drops its own disposable per-run database (no more destructive DB name from an env var) | 1, 2 | **Implemented, executed against a real MySQL 8.0 server** — see this change's own verification report for the exact command/result |
| `dub_worker/tests/test_worker_pipeline_config_failures.py` (new) | Pure/DB-free tests proving an invalid `channel_pipeline_configs` row (or incomplete schema) is a terminal, non-retryable job failure that never starts the dubbing subprocess, contrasted with retryable transient failures | 2 | **Implemented, executed, passing** |
| `docs/SRS-douyin-download-pipeline.md` | New table entry, ER diagram, `aweme.voice_id` semantics row | 1 | **Implemented** |
| `docs/channel-pipeline-config.md` | Full design doc, mirroring `docs/channel-logo-config.md` | 1, 2 | **Implemented** |
| `dub_worker/README.md` | Section covering Phase 1/2 behavior and the disposable-test-database setup | 2 | **Implemented** |
| `run_pipeline.py` | Fix D9 (`generate_publish_metadata` input requirement) and D10 (`mux_video`/`render_subtitle` passthrough) | 3 | **Implemented** |
| `dub_worker/config.py`, `dub_worker/worker.py`, `dub_worker/db.py` | Consume `translation_enabled`/`dubbing_enabled`/`daily_video_limit` for actual flow routing and quota enforcement; admission-time quota-consumption marker persisted in `pipeline_jobs.result_json`; single post-lock admission timestamp for both the business-day bounds and `started_at` | 3 | **Implemented, executed against real MySQL** |

Not applicable: no ORM/model files, no admin API/DTOs in this repo, and no
`pipeline_jobs` schema change. Existing seed `data.sql` files are retained
unchanged; `channel_pipeline_configs` population comes from the guarded
backfill/default-row branch in v4.

---

**Phase 1/2/3A/3B are all implemented on `feature/channel-config`** (D1–D12
resolved — see §0). D13 remains an open, deliberate deferral, not a
blocker.
