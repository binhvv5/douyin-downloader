import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from auth import CookieManager
from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core import DouyinAPIClient, DownloaderFactory, URLParser
from core import LoginRequiredError
from cli.login_flow import can_interactive_login, interactive_relogin
from storage import Database, FileManager, create_database
from utils.logger import set_console_log_level, setup_logger
from utils.notifier import build_notifier
from control.channel_download_lock import ChannelDownloadLock
from utils.validators import is_short_url, normalize_short_url

logger = setup_logger("CLI")
display = ProgressDisplay()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _run_with_relogin(make_coro, cookie_manager, *, serve=False):
    """Run make_coro(); on LoginRequiredError, relogin once and retry.

    make_coro is a zero-arg callable returning a fresh coroutine each call,
    so the retry re-creates its own DouyinAPIClient with refreshed cookies.
    Refreshed cookies propagate through ``cookie_manager`` as a clean replace
    (not a merge), and both call sites read their cookies from it on retry.
    """
    for attempt in range(2):
        try:
            return await make_coro()
        except LoginRequiredError as exc:
            interactive = can_interactive_login(serve=serve)
            if attempt == 1 or not interactive:
                display.print_error(
                    f"Session expired; re-login required (status {exc.status_code}):"
                    f"{exc.status_msg or 'Please log in first'}。"
                )
                if not interactive:
                    display.print_warning(
                        "Non-interactive environment; browser not opened. Manually update "
                        "config/cookies.json (or run python tools/cookie_fetcher.py to log in)."
                    )
                raise
            display.print_warning(
                f"Not logged in (status {exc.status_code}); starting re-login…"
            )
            new_cookies = await interactive_relogin()
            if not new_cookies:
                display.print_error("Re-login incomplete; aborted.")
                raise
            cookie_manager.set_cookies(new_cookies)
            display.print_success("Session updated; retrying…")


async def download_url(
    url: str,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
    *,
    channel_id: Optional[int] = None,
    acquire_channel_lock: bool = True,
    channel_lock_timeout: int = 3600,
):
    lock = None
    if acquire_channel_lock:
        lock = ChannelDownloadLock(database, lock_dir=config.get("path") or "./Downloaded/")
        acquired = await lock.try_acquire(
            holder=f"download-{os.getpid()}",
            timeout_seconds=int(channel_lock_timeout or 0),
        )
        if not acquired:
            if progress_reporter:
                progress_reporter.update_step("Downloading", "Another channel download is in progress")
            display.print_error(
                "Cannot download: another channel download is in progress "
                "(only one channel at a time)"
            )
            return None

    try:
        return await _download_url_inner(
            url,
            config,
            cookie_manager,
            database,
            progress_reporter,
            channel_id=channel_id,
        )
    finally:
        if lock is not None:
            await lock.release()


async def _download_url_inner(
    url: str,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
    *,
    channel_id: Optional[int] = None,
):
    if progress_reporter:
        progress_reporter.advance_step("Initializing", "Creating download components")
    file_manager = FileManager(config.get("path"))
    rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
    retry_handler = RetryHandler(max_retries=config.get("retry_times", 3))
    queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))

    original_url = url

    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if progress_reporter:
            progress_reporter.advance_step("Parsing link", "Resolving short URL")
        # 支持多种短链变体：v.douyin.com / v.iesdouyin.com / 无 scheme 的裸链接
        if is_short_url(url):
            resolved_url = await api_client.resolve_short_url(normalize_short_url(url))
            if resolved_url:
                url = resolved_url
            else:
                if progress_reporter:
                    progress_reporter.update_step("Parsing link", "Short URL resolution failed")
                display.print_error(f"Failed to resolve short URL: {url}")
                return None

        parsed = URLParser.parse(url)
        if not parsed:
            if progress_reporter:
                progress_reporter.update_step("Parsing link", "URL parsing failed")
            display.print_error(f"Failed to parse URL: {url}")
            return None

        if not progress_reporter:
            display.print_info(f"URL type: {parsed['type']}")
        if progress_reporter:
            progress_reporter.advance_step("Creating downloader", f"URL type: {parsed['type']}")

        downloader = DownloaderFactory.create(
            parsed["type"],
            config,
            api_client,
            file_manager,
            cookie_manager,
            database,
            rate_limiter,
            retry_handler,
            queue_manager,
            progress_reporter=progress_reporter,
        )

        if not downloader:
            if progress_reporter:
                progress_reporter.update_step("Creating downloader", "No matching downloader found")
            display.print_error(f"No downloader found for type: {parsed['type']}")
            return None

        if database and parsed.get("type") == "user":
            if channel_id is not None:
                downloader.channel_id = channel_id
            else:
                resolved_channel_id = await database.ensure_channel_for_user(
                    url,
                    sec_uid=parsed.get("sec_uid"),
                )
                downloader.channel_id = resolved_channel_id

        if progress_reporter:
            progress_reporter.advance_step("Downloading", "Fetching and downloading resources")
        try:
            result = await downloader.download(parsed)
        except Exception as exc:
            # Surface fatal downloader errors (e.g. user_info fetch failed
            # because cookies are invalid) as a per-URL failure instead of
            # crashing the whole batch. Keeps multi-URL CLI runs robust while
            # still telling the user why the URL was skipped.
            if progress_reporter:
                progress_reporter.update_step("Downloading", f"Failed：{exc}")
            display.print_error(f"Download failed for {url}: {exc}")
            return None

        if progress_reporter:
            progress_reporter.advance_step(
                "Recording history",
                "Writing download history" if (result and database) else "Database disabled; skipping",
            )
        if result and database:
            safe_config = {
                k: v
                for k, v in config.config.items()
                if k not in ("cookies", "cookie", "transcript")
            }
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": parsed["type"],
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": json.dumps(safe_config, ensure_ascii=False),
                }
            )
            if parsed.get("type") == "user" and getattr(downloader, "channel_id", None):
                await database.update_channel_last_sync(downloader.channel_id)

        if progress_reporter:
            if result:
                progress_reporter.advance_step(
                    "Finishing",
                    f"Success {result.success} / Failed {result.failed} / Skipped {result.skipped}",
                )
            else:
                progress_reporter.advance_step("Finishing", "No results to summarize")

        return result


async def main_async(args):
    if not args.serve:
        display.show_banner()

    if args.config:
        config_path = args.config
    else:
        config_path = "config.yml"

    # 若 config 不存在且使用了 --hot-board / --search / --serve 等独立子命令，
    # 允许以默认配置运行（只要命令行提供了 --path）。
    if not Path(config_path).exists():
        if not (args.hot_board is not None or args.search or args.serve):
            display.print_error(f"Config file not found: {config_path}")
            return
        # For ``--serve`` we still pass the (yet-missing) path so later
        # ``config.save()`` calls from the REST settings endpoint create
        # the file in the right place (e.g. Electron's userData dir).
        # Other subcommands keep the historical behaviour of in-memory
        # defaults.
        if args.serve and args.config:
            config = ConfigLoader(config_path)
        else:
            config = ConfigLoader(None)
    else:
        config = ConfigLoader(config_path)

    if args.path:
        config.update(path=args.path)

    # 独立子命令：热榜 / 搜索 / 服务
    if args.hot_board is not None or args.search:
        discovery_cm = CookieManager()
        discovery_cm.set_cookies(config.get_cookies())
        await _run_with_relogin(
            lambda: _run_discovery_subcommand(args, config, discovery_cm),
            discovery_cm,
            serve=False,
        )
        return
    if args.serve:
        await _run_serve_subcommand(args, config)
        return
    if args.scheduler:
        from cli.channel_scheduler import run_channel_scheduler

        await run_channel_scheduler(args, config)
        return

    if args.url:
        urls = args.url if isinstance(args.url, list) else [args.url]
        for url in urls:
            if url not in config.get("link", []):
                config.update(link=config.get("link", []) + [url])

    if args.thread:
        config.update(thread=args.thread)

    if not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    cookies = config.get_cookies()
    cookie_manager = CookieManager()
    cookie_manager.set_cookies(cookies)

    if not cookie_manager.validate_cookies():
        display.print_warning("Cookies may be invalid or incomplete")

    database = None
    if config.get("database"):
        database = create_database(config.config)
        await database.initialize()
        await database.sync_channels_from_urls(config.get_links())
        display.print_success("Database initialized")

    urls = config.get_links()
    if database:
        urls = await database.resolve_download_urls(urls)
    if not urls:
        display.print_error(
            "No URLs to download. Add links to config.yml or insert enabled=1 channels "
            "into the channels table (database)."
        )
        if database is not None:
            await database.close()
        return
    if len(urls) > 1:
        display.print_warning(
            f"{len(urls)} channel(s) — sequential download with global lock (one channel at a time)"
        )
    display.print_info(f"Found {len(urls)} URL(s) to process")

    all_results = []
    progress_config = config.get("progress", {}) or {}
    quiet_by_config = _as_bool(progress_config.get("quiet_logs", True), default=True)
    quiet_progress_logs = quiet_by_config and not (args.verbose or args.show_warnings)
    if quiet_progress_logs:
        # Progress 运行期间若有大量错误日志会触发 rich 反复重绘，导致屏幕出现重复块。
        # 默认静默控制台日志，after download completes。
        set_console_log_level(logging.CRITICAL)

    display.start_download_session(len(urls))
    try:
        for i, url in enumerate(urls, 1):
            display.start_url(i, len(urls), url)

            result = await _run_with_relogin(
                lambda u=url: download_url(
                    u,
                    config,
                    cookie_manager,
                    database,
                    progress_reporter=display,
                ),
                cookie_manager,
                serve=False,
            )
            if result:
                all_results.append(result)
                display.complete_url(result)
            else:
                display.fail_url("Download failed or invalid link")
    finally:
        display.stop_download_session()
        if database is not None:
            await database.close()
        if quiet_progress_logs:
            set_console_log_level(logging.ERROR)

    if all_results:
        from core.downloader_base import DownloadResult

        total_result = DownloadResult()
        for r in all_results:
            total_result.total += r.total
            total_result.success += r.success
            total_result.failed += r.failed
            total_result.skipped += r.skipped

        display.print_success("\n=== Overall Summary ===")
        display.show_result(total_result)

        await _dispatch_notifications(config, total_result, len(urls))
    else:
        # When all links fail，也发通知（若启用）
        await _dispatch_notifications(config, None, len(urls))


async def _run_discovery_subcommand(
    args, config: ConfigLoader, cookie_manager: CookieManager
) -> None:
    """处理 --hot-board 与 --search 子命令。"""
    from core.discovery import dump_hot_board, search_and_dump

    base_path = Path(config.get("path") or "./Downloaded/")

    async with DouyinAPIClient(cookie_manager.get_cookies()) as api_client:
        if args.hot_board is not None:
            display.print_info("Fetching Douyin hot search board…")
            result = await dump_hot_board(api_client, base_path, limit=int(args.hot_board or 0))
            display.print_success(f"Hot board saved: {result['count']} entries -> {result['path']}")
        if args.search:
            display.print_info(f"Search keyword: {args.search}")
            result = await search_and_dump(
                api_client,
                args.search,
                base_path,
                max_items=int(args.search_max or 50),
            )
            display.print_success(f"Search results saved: {result['count']} entries -> {result['path']}")


async def _run_serve_subcommand(args, config: ConfigLoader) -> None:
    """启动 REST API 服务模式（fastapi + uvicorn 为可选依赖）。"""
    try:
        from server.app import run_server
    except ImportError as exc:
        display.print_error(
            f"REST server mode requires optional dependencies fastapi + uvicorn:"
            f"\n  pip install fastapi uvicorn\nOriginal error: {exc}"
        )
        return

    display.print_info(f"Starting REST server: http://{args.serve_host}:{args.serve_port}")
    await run_server(config, host=args.serve_host, port=args.serve_port)


async def _dispatch_notifications(config: ConfigLoader, total_result: Any, url_count: int) -> None:
    notifier = build_notifier(config)
    if not notifier.enabled:
        return

    if total_result is None:
        title = "Douyin Downloader: all failed"
        body = f"Processed {url_count} link(s); no successful results"
        level = "failure"
    else:
        fail_or_partial = total_result.failed > 0 or total_result.success == 0
        level = "failure" if fail_or_partial else "success"
        title = "Douyin download complete" if level == "success" else "Douyin download partially failed"
        body = (
            f"Links {url_count} / Total items {total_result.total} / "
            f"Success {total_result.success} / Failed {total_result.failed} / "
            f"Skipped {total_result.skipped}"
        )

    try:
        summary = await notifier.send(title=title, body=body, level=level)
        if summary:
            succ = sum(1 for ok in summary.values() if ok)
            logger.info(
                "Notification dispatched to %d provider(s), %d ok",
                len(summary),
                succ,
            )
    except Exception as exc:  # Notification failures must not affect main flow
        logger.warning("Notification dispatch error: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Douyin Downloader - batch download tool")
    parser.add_argument("-u", "--url", action="append", help="Download URL(s)")
    parser.add_argument("-c", "--config", help="Config file path (default: config.yml)")
    parser.add_argument("-p", "--path", help="Save path")
    parser.add_argument("-t", "--thread", type=int, help="Thread count")
    parser.add_argument("--show-warnings", action="store_true", help="Show warning logs in console")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose console logs")
    parser.add_argument(
        "--hot-board",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help="Fetch Douyin hot board and export JSONL; optional limit N (default: all)",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        metavar="KEYWORD",
        help="Search posts by keyword and export JSONL",
    )
    parser.add_argument(
        "--search-max",
        type=int,
        default=50,
        help="Max items to fetch for --search (default 50)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run in REST API server mode (requires fastapi + uvicorn)",
    )
    parser.add_argument("--serve-host", type=str, default="127.0.0.1", help="REST server listen address")
    parser.add_argument("--serve-port", type=int, default=8000, help="REST server listen port")
    parser.add_argument(
        "--scheduler",
        action="store_true",
        help="Run channel scheduler (default: every 10 minutes, 1 channel per tick)",
    )
    parser.add_argument(
        "--scheduler-interval",
        type=int,
        default=None,
        help="Scheduler interval in seconds (minimum 60)",
    )
    parser.add_argument(
        "--scheduler-once",
        action="store_true",
        help="Run scheduler once then exit",
    )
    try:
        from __init__ import __version__
    except ImportError:
        __version__ = "2.0.0"
    parser.add_argument("--version", action="version", version=__version__)

    args = parser.parse_args()

    if args.verbose:
        set_console_log_level(logging.INFO)
    elif args.show_warnings:
        set_console_log_level(logging.WARNING)
    else:
        set_console_log_level(logging.ERROR)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        display.print_warning("\nDownload interrupted by user")
        sys.exit(0)
    except Exception as e:
        display.print_error(f"Fatal error: {e}")
        logger.exception("Fatal error occurred")
        sys.exit(1)


if __name__ == "__main__":
    main()
