import json
from pathlib import Path

import pytest

from storage import Database


@pytest.mark.asyncio
async def test_pipeline_handoff_after_download(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    channel_id = await database.ensure_channel_for_user(
        "https://www.douyin.com/user/MS4wLjABAAAAtest",
        sec_uid="MS4wLjABAAAAtest",
        name="Test Channel",
    )

    mp4 = tmp_path / "2024-01-01_1234567890123456789.mp4"
    mp4.write_bytes(b"video")
    meta = tmp_path / "2024-01-01_1234567890123456789_data.json"
    meta.write_text("{}", encoding="utf-8")

    path2_root = r"D:\share\out"
    await database.add_aweme(
        {
            "aweme_id": "1234567890123456789",
            "aweme_type": "video",
            "channel_id": channel_id,
            "title": "hello",
            "author_id": "1",
            "author_name": "Author",
            "create_time": 1700000000,
            "file_path": str(tmp_path),
            "file_path2": str(Path(path2_root) / tmp_path.name),
            "metadata": json.dumps({"a": 1}, ensure_ascii=False),
            "download_status": "success",
        }
    )
    await database.complete_download_handoff(
        aweme_id="1234567890123456789",
        channel_id=channel_id,
        downloaded_files=[mp4, meta],
        metadata_translate_ok=True,
        translation_enabled=True,
        download_status="success",
        base_path=tmp_path,
        path2=path2_root,
    )

    assert await database.get_pipeline_job_status("1234567890123456789", "download") == "success"
    assert (
        await database.get_pipeline_job_status("1234567890123456789", "metadata_translate")
        == "success"
    )
    assert await database.get_pipeline_job_status("1234567890123456789", "dub") == "pending"

    db = await database._get_conn()
    cursor = await db.execute(
        "SELECT asset_type, file_path, file_path2 FROM video_assets WHERE aweme_id = ?",
        ("1234567890123456789",),
    )
    assets = {row[0]: {"file_path": row[1], "file_path2": row[2]} for row in await cursor.fetchall()}
    assert "source_mp4" in assets
    assert "metadata_json" in assets
    assert assets["source_mp4"]["file_path2"] == str(Path(path2_root) / mp4.name)
    assert assets["metadata_json"]["file_path2"] == str(Path(path2_root) / meta.name)

    cursor = await db.execute(
        "SELECT file_path2 FROM aweme WHERE aweme_id = ?",
        ("1234567890123456789",),
    )
    aweme_row = await cursor.fetchone()
    assert aweme_row is not None
    assert aweme_row[0] == str(Path(path2_root) / tmp_path.name)

    await database.close()


@pytest.mark.asyncio
async def test_pipeline_skip_handoff_creates_dub_pending(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    channel_id = await database.ensure_channel_for_user(
        "https://www.douyin.com/user/MS4wLjABAAAAskip",
        sec_uid="MS4wLjABAAAAskip",
    )
    media_root = tmp_path / "Downloaded"
    media_root.mkdir()
    mp4 = media_root / "author" / "2024-01-01_9876543210987654321.mp4"
    mp4.parent.mkdir(parents=True)
    mp4.write_bytes(b"existing")

    await database.handoff_skipped_download(
        aweme_id="9876543210987654321",
        channel_id=channel_id,
        base_path=media_root,
    )

    assert await database.get_pipeline_job_status("9876543210987654321", "download") == "skipped"
    assert await database.get_pipeline_job_status("9876543210987654321", "dub") == "pending"

    db = await database._get_conn()
    cursor = await db.execute(
        "SELECT file_path FROM video_assets WHERE aweme_id = ? AND asset_type = ?",
        ("9876543210987654321", "source_mp4"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert Path(row[0]).exists()

    await database.close()


@pytest.mark.asyncio
async def test_metadata_translate_claim_only_once(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    aweme_id = "1111222233334444555"
    claimed_a = await database.try_claim_metadata_translate(aweme_id=aweme_id, channel_id=1)
    claimed_b = await database.try_claim_metadata_translate(aweme_id=aweme_id, channel_id=1)
    assert claimed_a is True
    assert claimed_b is False
    assert (
        await database.get_pipeline_job_status(aweme_id, "metadata_translate")
        == "processing"
    )

    await database.complete_download_handoff(
        aweme_id=aweme_id,
        channel_id=1,
        downloaded_files=[],
        metadata_translate_ok=None,
        translation_enabled=True,
        download_status="success",
        base_path=tmp_path,
    )
    assert (
        await database.get_pipeline_job_status(aweme_id, "metadata_translate")
        == "processing"
    )

    await database.complete_download_handoff(
        aweme_id=aweme_id,
        channel_id=1,
        downloaded_files=[],
        metadata_translate_ok=True,
        translation_enabled=True,
        download_status="success",
        base_path=tmp_path,
    )
    assert (
        await database.get_pipeline_job_status(aweme_id, "metadata_translate")
        == "success"
    )
    await database.close()


@pytest.mark.asyncio
async def test_resolve_download_urls_prefers_enabled_channels(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    await database.initialize()

    await database.upsert_channel(
        name="A",
        douyin_url="https://www.douyin.com/user/A",
        sec_uid="sec_a",
        enabled=1,
    )
    await database.upsert_channel(
        name="B",
        douyin_url="https://www.douyin.com/user/B",
        sec_uid="sec_b",
        enabled=0,
    )

    urls = await database.resolve_download_urls(["https://fallback.example/user"])
    assert urls == ["https://www.douyin.com/user/A"]

    await database.close()
