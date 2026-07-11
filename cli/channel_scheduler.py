import asyncio
import copy
import logging
import os
from typing import Any, Dict, Optional

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


def _apply_channel_sync_mode(config: ConfigLoader, channel: Dict[str, Any]) -> Dict[str, Any]:
    sync_mode = str(channel.get("sync_mode") or "incremental").strip().lower()
    original_increase = copy.deepcopy(config.get("increase") or {})
    patched = dict(original_increase)
    if sync_mode == "incremental":
        patched["post"] = True
    elif sync_mode == "full":
        patched["post"] = False
    config.update(increase=patched)
    return original_increase


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

    original_increase = _apply_channel_sync_mode(config, channel)
    try:
        display.print_info(
            f"Scheduler: syncing channel [{channel_id}] {channel_name} ({channel.get('sync_mode')})"
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
        config.update(increase=original_increase)
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
