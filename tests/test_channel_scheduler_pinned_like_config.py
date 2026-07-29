"""Tests for the database-backed `download_pinned` / `number_like` channel
config overrides consumed by the database polling downloader.

Covers:
1. `resolve_channel_config_overrides` — precedence/validation for
   download_pinned (true/false/NULL) and number_like (positive/0/NULL/negative).
2. `_apply_channel_sync_config` — the channel-config mapper/adapter boundary:
   database value overrides file/global config, maps into the exact
   in-memory shape the core downloader already consumes, and restoring
   originals afterward leaves file-based (no-channel) behavior untouched.
3. The resolved config flows into the *existing* core download logic
   (`core/user_downloader.py`, `core/user_modes/base_strategy.py`) with no
   second implementation for pinned-video handling or the like threshold.
"""

import pytest

from cli.channel_scheduler import (
    ChannelConfigError,
    _apply_channel_sync_config,
    resolve_channel_config_overrides,
)
from config import ConfigLoader
from control.queue_manager import QueueManager
from core.user_downloader import UserDownloader
from core.user_modes.like_strategy import LikeUserModeStrategy
from storage.file_manager import FileManager


def _channel(**overrides):
    base = {
        "id": 1,
        "name": "Test Channel",
        "douyin_url": "https://www.douyin.com/user/test",
        "sec_uid": "sec_test",
        "sync_mode": "incremental",
        "download_batch_size": 10,
        "download_pinned": None,
        "number_like": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. resolve_channel_config_overrides
# ---------------------------------------------------------------------------
def test_resolve_overrides_download_pinned_true():
    overrides = resolve_channel_config_overrides(_channel(download_pinned=1))
    assert overrides["download_pinned"] is True


def test_resolve_overrides_download_pinned_false_is_explicit():
    """False is an explicit override, not "missing" -- must not fall back."""
    overrides = resolve_channel_config_overrides(_channel(download_pinned=0))
    assert "download_pinned" in overrides
    assert overrides["download_pinned"] is False


def test_resolve_overrides_download_pinned_null_is_absent():
    overrides = resolve_channel_config_overrides(_channel(download_pinned=None))
    assert "download_pinned" not in overrides


def test_resolve_overrides_positive_number_like():
    overrides = resolve_channel_config_overrides(_channel(number_like=500))
    assert overrides["number"] == {"like": 500}


def test_resolve_overrides_number_like_zero_is_preserved():
    """0 is explicit and must not be replaced by a default."""
    overrides = resolve_channel_config_overrides(_channel(number_like=0))
    assert overrides["number"] == {"like": 0}


def test_resolve_overrides_number_like_null_is_absent():
    overrides = resolve_channel_config_overrides(_channel(number_like=None))
    assert "number" not in overrides


def test_resolve_overrides_negative_number_like_rejected():
    with pytest.raises(ChannelConfigError, match="channel id=1"):
        resolve_channel_config_overrides(_channel(number_like=-1))


# ---------------------------------------------------------------------------
# 2. _apply_channel_sync_config -- precedence + in-memory shape
# ---------------------------------------------------------------------------
def test_apply_channel_sync_config_db_overrides_file_default():
    config = ConfigLoader(None)
    config.update(download_pinned=True)  # file/global value
    config.config["number"]["like"] = 999  # file/global value

    channel = _channel(download_pinned=False, number_like=42)
    _apply_channel_sync_config(config, channel)

    assert config.get("download_pinned") is False
    assert config.get("number")["like"] == 42


def test_apply_channel_sync_config_null_falls_back_to_existing_config():
    config = ConfigLoader(None)
    config.update(download_pinned=True)
    config.config["number"]["like"] = 777

    channel = _channel(download_pinned=None, number_like=None)
    _apply_channel_sync_config(config, channel)

    assert config.get("download_pinned") is True
    assert config.get("number")["like"] == 777


def test_apply_channel_sync_config_restores_originals(monkeypatch):
    config = ConfigLoader(None)
    config.update(download_pinned=True)
    config.config["number"]["like"] = 10

    channel = _channel(download_pinned=False, number_like=0)
    original_increase, original_number, original_download_pinned = _apply_channel_sync_config(
        config, channel
    )
    assert config.get("download_pinned") is False
    assert config.get("number")["like"] == 0

    config.update(
        increase=original_increase,
        number=original_number,
        download_pinned=original_download_pinned,
    )
    assert config.get("download_pinned") is True
    assert config.get("number")["like"] == 10


def test_apply_channel_sync_config_invalid_number_like_raises_with_channel_id():
    config = ConfigLoader(None)
    channel = _channel(id=7, number_like=-5)
    with pytest.raises(ChannelConfigError, match="channel id=7"):
        _apply_channel_sync_config(config, channel)


# ---------------------------------------------------------------------------
# 3. Resolved config flows through the EXISTING core download logic
#    (no second implementation for pinned-video handling or number.like).
# ---------------------------------------------------------------------------
class _NoopRateLimiter:
    async def acquire(self):
        return


def _build_downloader(tmp_path, config: ConfigLoader) -> UserDownloader:
    return UserDownloader(
        config=config,
        api_client=object(),
        file_manager=FileManager(str(tmp_path / "Downloaded")),
        cookie_manager=None,
        database=None,
        rate_limiter=_NoopRateLimiter(),
        retry_handler=None,
        queue_manager=QueueManager(max_workers=2),
    )


def test_db_download_pinned_true_reaches_existing_pinned_filter(tmp_path):
    config = ConfigLoader(None)
    channel = _channel(download_pinned=True)
    _apply_channel_sync_config(config, channel)

    downloader = _build_downloader(tmp_path, config)
    items = [{"aweme_id": "1", "is_top": True}, {"aweme_id": "2", "is_top": False}]
    assert downloader._filter_pinned_items(items) == items


def test_db_download_pinned_false_reaches_existing_pinned_filter(tmp_path):
    config = ConfigLoader(None)
    channel = _channel(download_pinned=False)
    _apply_channel_sync_config(config, channel)

    downloader = _build_downloader(tmp_path, config)
    pinned = {"aweme_id": "1", "is_top": True}
    unpinned = {"aweme_id": "2", "is_top": False}
    assert downloader._filter_pinned_items([pinned, unpinned]) == [unpinned]


def test_db_number_like_reaches_existing_like_strategy_limit(tmp_path):
    config = ConfigLoader(None)
    channel = _channel(number_like=1)
    _apply_channel_sync_config(config, channel)

    downloader = _build_downloader(tmp_path, config)
    strategy = LikeUserModeStrategy(downloader)
    # Same paging/limit code path as file-based config -- core/user_modes
    # was not touched by the database-config feature.
    assert downloader.config.get("number", {}).get(strategy.mode_name) == 1
