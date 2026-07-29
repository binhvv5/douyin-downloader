import asyncio
import copy
import logging
import os
from typing import Any, Dict, Optional, Tuple

from auth import CookieManager
from cli.main import _dispatch_notifications, _run_with_relogin, download_url
from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from control.channel_download_lock import ChannelDownloadLock
from storage import Database, create_database
from utils.logger import set_console_log_level, setup_logger

logger = setup_logger("ChannelScheduler")
display = ProgressDisplay()


def _scheduler_interval_seconds(config: ConfigLoader, args) -> int:
    scheduler_cfg = config.get("scheduler") or {}
    if getattr(args, "scheduler_interval", None):
        return max(60, int(args.scheduler_interval))
    return max(60, int(scheduler_cfg.get("interval_seconds") or 600))


def _scheduler_lock_timeout(config: ConfigLoader) -> int:
    scheduler_cfg = config.get("scheduler") or {}
    return max(0, int(scheduler_cfg.get("lock_timeout_seconds") or 0))


class ChannelConfigError(ValueError):
    """Raised when a channel's database-backed configuration is invalid."""


def resolve_channel_config_overrides(channel: Dict[str, Any]) -> Dict[str, Any]:
    """Map a channel DB row's ``download_pinned``/``number_like`` columns into
    the same in-memory config shape the core downloader already consumes
    (``{"download_pinned": ..., "number": {"like": ...}}``).

    Only non-NULL database values are included — a missing key here means
    "no channel-level override", so the caller must keep whatever value is
    already in the config (which already reflects the file/global default).
    A present key means the database value is explicit (including
    ``False``/``0``) and must win over the file/global default.
    """
    channel_id = channel.get("id")
    overrides: Dict[str, Any] = {}

    db_pinned = channel.get("download_pinned")
    if db_pinned is not None:
        overrides["download_pinned"] = bool(db_pinned)

    db_like = channel.get("number_like")
    if db_like is not None:
        try:
            like_value = int(db_like)
        except (TypeError, ValueError):
            raise ChannelConfigError(
                f"channel id={channel_id}: number_like must be an integer, got {db_like!r}"
            )
        if like_value < 0:
            raise ChannelConfigError(
                f"channel id={channel_id}: number_like must be a non-negative integer, "
                f"got {like_value}"
            )
        overrides["number"] = {"like": like_value}

    return overrides


def _apply_channel_sync_config(
    config: ConfigLoader, channel: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Any]:
    sync_mode = str(channel.get("sync_mode") or "incremental").strip().lower()
    original_increase = copy.deepcopy(config.get("increase") or {})
    original_number = copy.deepcopy(config.get("number") or {})
    original_download_pinned = config.get("download_pinned")

    patched_increase = dict(original_increase)
    if sync_mode == "incremental":
        patched_increase["post"] = True
    elif sync_mode == "full":
        patched_increase["post"] = False

    patched_number = dict(original_number)
    if sync_mode == "full":
        patched_number["post"] = 0
    else:
        batch_raw = channel.get("download_batch_size")
        if batch_raw is None:
            batch_raw = original_number.get("post", 10)
        patched_number["post"] = max(0, int(batch_raw))

    # Precedence: non-null channel DB value > existing global/file config value
    # > hard-coded application default. ``resolve_channel_config_overrides``
    # only returns keys the channel explicitly overrides in the DB, so a
    # missing key here naturally falls through to what ConfigLoader already
    # resolved from file/env/hard-coded defaults.
    overrides = resolve_channel_config_overrides(channel)
    patched_download_pinned = overrides.get("download_pinned", original_download_pinned)
    if "number" in overrides:
        patched_number["like"] = overrides["number"]["like"]

    config.update(
        increase=patched_increase,
        number=patched_number,
        download_pinned=patched_download_pinned,
    )

    pinned_source = "database" if "download_pinned" in overrides else "file_default"
    like_source = "database" if "number" in overrides else "file_default"
    logger.info(
        "Resolved channel config: channel_id=%s channel_name=%s "
        "config_source=download_pinned:%s;number_like:%s "
        "download_pinned=%s number_like=%s",
        channel.get("id"),
        channel.get("name"),
        pinned_source,
        like_source,
        patched_download_pinned,
        patched_number.get("like"),
    )

    return original_increase, original_number, original_download_pinned


async def run_scheduler_tick(
    *,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database,
    lock_timeout_seconds: int,
    progress_reporter: Optional[ProgressDisplay] = None,
) -> bool:
    await database.sync_channels_from_urls(config.get_links())
    channel = await database.pick_next_channel_for_sync()
    if not channel:
        logger.info("Scheduler tick: no enabled channels")
        display.print_info("No enabled channels in the channels table")
        return False

    url = str(channel.get("douyin_url") or "").strip()
    if not url:
        logger.warning("Scheduler tick: channel id=%s missing douyin_url", channel.get("id"))
        return False

    channel_id = int(channel["id"])
    channel_name = channel.get("name") or url
    lock = ChannelDownloadLock(database, lock_dir=config.get("path") or "./Downloaded/")
    holder = f"scheduler-{os.getpid()}-channel-{channel_id}"
    acquired = await lock.try_acquire(holder=holder, timeout_seconds=lock_timeout_seconds)
    if not acquired:
        logger.info(
            "Scheduler tick: download lock busy, skip channel id=%s (%s)",
            channel_id,
            channel_name,
        )
        display.print_warning("Another channel download is in progress — bỏ qua lượt quét này")
        return False

    original_increase = original_number = original_download_pinned = None
    try:
        try:
            (
                original_increase,
                original_number,
                original_download_pinned,
            ) = _apply_channel_sync_config(config, channel)
        except ChannelConfigError as exc:
            logger.error(
                "Scheduler tick: invalid channel config id=%s (%s): %s",
                channel_id,
                channel_name,
                exc,
            )
            display.print_error(f"Channel [{channel_id}] config error: {exc}")
            return False

        batch_size = int((config.get("number") or {}).get("post") or 0)
        display.print_info(
            f"Scheduler: syncing channel [{channel_id}] {channel_name} "
            f"(sync_mode={channel.get('sync_mode')}, download_batch_size={batch_size or 'unlimited'})"
        )
        result = await _run_with_relogin(
            lambda: download_url(
                url,
                config,
                cookie_manager,
                database,
                progress_reporter=progress_reporter,
                channel_id=channel_id,
                acquire_channel_lock=False,
            ),
            cookie_manager,
            serve=True,
        )
        if result:
            display.print_success(
                f"Channel [{channel_id}] done — success {result.success}, "
                f"failed {result.failed}, skipped {result.skipped}"
            )
            await _dispatch_notifications(config, result, 1)
            return True
        display.print_warning(f"Channel [{channel_id}] produced no results")
        return False
    finally:
        if original_increase is not None:
            config.update(
                increase=original_increase,
                number=original_number,
                download_pinned=original_download_pinned,
            )
        await lock.release()


async def run_channel_scheduler(args, config: ConfigLoader) -> None:
    if not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    if not config.get("database"):
        display.print_error("Scheduler requires database: true in config")
        return

    interval = _scheduler_interval_seconds(config, args)
    lock_timeout = _scheduler_lock_timeout(config)

    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    database = create_database(config.config)
    await database.initialize()
    await database.sync_channels_from_urls(config.get_links())

    scheduler_cfg = config.get("scheduler") or {}
    quiet_logs = bool((config.get("progress") or {}).get("quiet_logs", True))
    if quiet_logs and not (args.verbose or args.show_warnings):
        set_console_log_level(logging.INFO)

    display.show_banner()
    display.print_success("Channel scheduler started")
    display.print_info(
        f"Interval: {interval}s | Lock timeout: {lock_timeout}s | "
        f"Mode: 1 channel per tick, 1 download project-wide"
    )

    run_once = bool(getattr(args, "scheduler_once", False) or scheduler_cfg.get("run_once"))
    try:
        while True:
            try:
                await run_scheduler_tick(
                    config=config,
                    cookie_manager=cookie_manager,
                    database=database,
                    lock_timeout_seconds=lock_timeout,
                    progress_reporter=display,
                )
            except Exception as exc:
                logger.exception("Scheduler tick failed: %s", exc)
                display.print_error(f"Scheduler tick error: {exc}")
            if run_once:
                break
            display.print_info(f"Waiting {interval}s until next tick…")
            await asyncio.sleep(interval)
    finally:
        await database.close()
        display.print_warning("Channel scheduler stopped")
