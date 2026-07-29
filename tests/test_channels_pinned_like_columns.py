"""Tests for the `channels.download_pinned` / `channels.number_like` columns.

Covers:
1. Legacy `channels` table without the two columns -> `initialize()` adds
   them as nullable, additive, idempotent migration.
2. `get_enabled_channels()` / `pick_next_channel_for_sync()` surface the two
   new fields (and default to NULL/None for a channel that never set them),
   without introducing a second query (single SELECT already includes them).
"""

import aiosqlite

from storage.database import Database

_LEGACY_CHANNELS_DDL = """
    CREATE TABLE IF NOT EXISTS channels (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        douyin_url      TEXT NOT NULL,
        sec_uid         TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        sync_mode       TEXT NOT NULL DEFAULT 'incremental',
        download_batch_size INTEGER NOT NULL DEFAULT 10,
        last_sync_at    TEXT,
        notes           TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""


async def _table_columns(db_path: str, table: str):
    async with aiosqlite.connect(db_path) as conn:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def test_initialize_adds_pinned_and_like_columns_to_legacy_channels(tmp_path):
    db_path = tmp_path / "test.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(_LEGACY_CHANNELS_DDL)
        await raw.execute(
            """
            INSERT INTO channels (name, douyin_url, sec_uid)
            VALUES (?, ?, ?)
            """,
            ("Legacy Channel", "https://www.douyin.com/user/legacy", "sec_legacy"),
        )
        await raw.commit()

    pre_cols = await _table_columns(str(db_path), "channels")
    assert "download_pinned" not in pre_cols
    assert "number_like" not in pre_cols

    db = Database(db_path=str(db_path))
    await db.initialize()
    try:
        post_cols = await _table_columns(str(db_path), "channels")
        assert "download_pinned" in post_cols
        assert "number_like" in post_cols

        # Pre-existing row must survive, with both new columns defaulting to NULL.
        conn = await db._get_conn()
        cursor = await conn.execute(
            "SELECT sec_uid, download_pinned, number_like FROM channels WHERE sec_uid = ?",
            ("sec_legacy",),
        )
        row = await cursor.fetchone()
        assert row == ("sec_legacy", None, None)
    finally:
        await db.close()


async def test_initialize_is_idempotent_for_pinned_and_like_columns(tmp_path):
    db = Database(db_path=str(tmp_path / "test.db"))
    try:
        await db.initialize()
        await db.initialize()  # must not raise (no duplicate ALTER TABLE)
        cols = await _table_columns(db.db_path, "channels")
        assert "download_pinned" in cols
        assert "number_like" in cols
    finally:
        await db.close()


async def test_new_channel_defaults_pinned_and_like_to_none(tmp_path):
    db = Database(db_path=str(tmp_path / "test.db"))
    await db.initialize()
    try:
        await db.upsert_channel(
            name="New Channel",
            douyin_url="https://www.douyin.com/user/new",
            sec_uid="sec_new",
        )

        channels = await db.get_enabled_channels()
        assert len(channels) == 1
        assert channels[0]["download_pinned"] is None
        assert channels[0]["number_like"] is None

        nxt = await db.pick_next_channel_for_sync()
        assert nxt is not None
        assert nxt["download_pinned"] is None
        assert nxt["number_like"] is None
    finally:
        await db.close()


async def test_get_enabled_channels_surfaces_explicit_pinned_and_like_values(tmp_path):
    db = Database(db_path=str(tmp_path / "test.db"))
    await db.initialize()
    try:
        await db.upsert_channel(
            name="Configured Channel",
            douyin_url="https://www.douyin.com/user/configured",
            sec_uid="sec_configured",
        )
        conn = await db._get_conn()
        await conn.execute(
            "UPDATE channels SET download_pinned = 0, number_like = 250 WHERE sec_uid = ?",
            ("sec_configured",),
        )
        await conn.commit()

        channels = await db.get_enabled_channels()
        assert len(channels) == 1
        assert channels[0]["download_pinned"] == 0
        assert channels[0]["number_like"] == 250
    finally:
        await db.close()
