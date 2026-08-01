import asyncio
import os

import pytest

from control.channel_download_lock import ChannelDownloadLock, GLOBAL_LOCK_NAME
from storage import Database


@pytest.mark.asyncio
async def test_daily_download_quota_respects_limit(tmp_path):
    import time

    db_path = tmp_path / "quota.db"
    database = Database(str(db_path))
    await database.initialize()

    channel_id = await database.upsert_channel(
        name="Quota",
        douyin_url="https://www.douyin.com/user/quota",
        sec_uid="sec_quota",
    )

    db = await database._get_conn()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_pipeline_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL UNIQUE,
            daily_video_limit INTEGER
        )
        """
    )
    await db.execute(
        "INSERT INTO channel_pipeline_configs (channel_id, daily_video_limit) VALUES (?, ?)",
        (channel_id, 5),
    )
    now = int(time.time())
    for i in range(3):
        await db.execute(
            """
            INSERT INTO aweme (aweme_id, aweme_type, channel_id, download_status, download_time)
            VALUES (?, 'video', ?, 'success', ?)
            """,
            (f"a{i}", channel_id, now),
        )
    await db.commit()

    quota = await database.get_channel_daily_download_quota(channel_id)
    assert quota["limit"] == 5
    assert quota["used"] == 3
    assert quota["remaining"] == 2

    await database.close()


@pytest.mark.asyncio
async def test_pick_next_channel_prefers_never_synced(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    await database.upsert_channel(
        name="Old",
        douyin_url="https://www.douyin.com/user/old",
        sec_uid="sec_old",
    )
    await database.upsert_channel(
        name="New",
        douyin_url="https://www.douyin.com/user/new",
        sec_uid="sec_new",
    )

    db = await database._get_conn()
    await db.execute(
        "UPDATE channels SET last_sync_at = '2026-01-01 00:00:00' WHERE sec_uid = ?",
        ("sec_old",),
    )
    await db.commit()

    nxt = await database.pick_next_channel_for_sync()
    assert nxt is not None
    assert nxt["sec_uid"] == "sec_new"

    await database.close()


@pytest.mark.asyncio
async def test_sqlite_channel_download_lock_excludes_second_worker(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    lock_a = ChannelDownloadLock(database, lock_dir=lock_dir)
    lock_b = ChannelDownloadLock(database, lock_dir=lock_dir)

    assert await lock_a.try_acquire(holder="a", timeout_seconds=0) is True
    assert await lock_b.try_acquire(holder="b", timeout_seconds=0) is False

    await lock_a.release()
    assert await lock_b.try_acquire(holder="b", timeout_seconds=0) is True
    await lock_b.release()

    await database.close()


@pytest.mark.asyncio
async def test_mysql_get_lock_when_available():
    pytest.importorskip("aiomysql")
    database = Database(
        engine="mysql",
        mysql={
            "host": "127.0.0.1",
            "port": 3306,
            "user": "douyin",
            "password": "douyin",
            "database": "douyin_downloader",
        },
    )
    try:
        await database.initialize()
    except Exception as exc:
        pytest.skip(f"MySQL unavailable: {exc}")

    name = f"test_lock_{os.getpid()}"
    assert await database.mysql_get_lock(name, 0) is True
    assert await database.mysql_get_lock(name, 0) is False
    await database.mysql_release_lock(name)
    assert await database.mysql_get_lock(name, 0) is True
    await database.mysql_release_lock(name)
    await database.close()
