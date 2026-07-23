from __future__ import annotations

from typing import Any, Dict, List

from core.user_modes.base_strategy import BaseUserModeStrategy
from utils.logger import setup_logger

logger = setup_logger("PostUserModeStrategy")


class PostUserModeStrategy(BaseUserModeStrategy):
    mode_name = "post"
    api_method_name = "get_user_post"

    async def collect_items(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        fetcher = getattr(self.downloader.api_client, self.api_method_name, None)
        if not callable(fetcher):
            logger.error("API client missing get_user_post")
            return []

        pending: List[Dict[str, Any]] = []
        max_cursor = 0
        has_more = True
        pagination_restricted = False

        number_limit = int(self.downloader.config.get("number", {}).get(self.mode_name, 0) or 0)
        media_filter_enabled = self._media_type_filter_enabled()

        self.downloader._progress_update_step("Fetching post list", "Paginating")

        while has_more:
            await self.downloader.rate_limiter.acquire()
            request_cursor = max_cursor
            page_data = await fetcher(sec_uid, request_cursor, 20)
            page = self._normalize_page_data(page_data)
            page_items = self.select_items(page)

            if not page_items:
                if page.get("status_code") == 0:
                    pagination_restricted = True
                    logger.warning(
                        "User post page empty at cursor=%s (status_code=0); "
                        "will attempt browser fallback",
                        request_cursor,
                    )
                break

            page_items = self._filter_pinned_items(page_items)
            if media_filter_enabled:
                page_items = self._filter_by_media_type(page_items)

            for item in page_items:
                if not await self._is_pending_download(item):
                    continue
                pending.append(item)
                if number_limit > 0 and len(pending) >= number_limit:
                    break

            self.downloader._progress_update_step(
                "Fetching post list",
                f"Pending undownloaded {len(pending)} item(s)",
            )

            if number_limit > 0 and len(pending) >= number_limit:
                break

            has_more = bool(page.get("has_more", False))
            max_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and max_cursor == request_cursor:
                logger.warning(
                    "max_cursor did not advance (%s), stop paging to avoid loop",
                    max_cursor,
                )
                pagination_restricted = True
                break

        if pagination_restricted:
            if number_limit <= 0 or len(pending) < number_limit:
                self.downloader._progress_update_step(
                    "Fetching post list", "Pagination restricted; trying browser fallback"
                )
                recovered: List[Dict[str, Any]] = []
                await self.downloader._recover_user_post_with_browser(sec_uid, user_info, recovered)
                recovered = self._filter_pinned_items(recovered)
                if media_filter_enabled:
                    recovered = self._filter_by_media_type(recovered)
                seen_ids = {
                    str(item.get("aweme_id") or "").strip()
                    for item in pending
                    if item.get("aweme_id")
                }
                for item in recovered:
                    aweme_id = str(item.get("aweme_id") or "").strip()
                    if not aweme_id or aweme_id in seen_ids:
                        continue
                    if not await self._is_pending_download(item):
                        continue
                    pending.append(item)
                    seen_ids.add(aweme_id)
                    if number_limit > 0 and len(pending) >= number_limit:
                        break
            if not pending:
                raise RuntimeError(
                    "Douyin API returned no posts (possible anti-bot limit);"
                    "retry later or re-login to Douyin to refresh cookies"
                )

        if number_limit > 0:
            return pending[:number_limit]
        return pending

    def apply_filters(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        filtered = self.downloader._filter_by_time(items)
        return self.downloader._limit_count(filtered, self.mode_name)

    async def _is_pending_download(self, item: Dict[str, Any]) -> bool:
        aweme_id = str(item.get("aweme_id") or "").strip()
        if not aweme_id:
            return False
        if self.downloader.database and await self.downloader.database.is_downloaded(aweme_id):
            return False
        is_local = getattr(self.downloader, "_is_locally_downloaded", None)
        if callable(is_local) and is_local(aweme_id):
            return False
        return True
