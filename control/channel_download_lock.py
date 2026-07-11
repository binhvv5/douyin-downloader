import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from storage.database import Database
from utils.logger import setup_logger

logger = setup_logger("ChannelDownloadLock")

GLOBAL_LOCK_NAME = "douyin_single_channel_download"


class ChannelDownloadLock:
    def __init__(
        self,
        database: Optional[Database] = None,
        *,
        lock_dir: Optional[Path] = None,
    ):
        self.database = database
        self.lock_dir = Path(lock_dir or ".")
        self._file_handle = None
        self._acquired = False

    async def try_acquire(self, *, holder: str = "", timeout_seconds: int = 0) -> bool:
        if self._acquired:
            return True
        if self.database is not None and self.database.engine == "mysql":
            acquired = await self.database.mysql_get_lock(GLOBAL_LOCK_NAME, timeout_seconds)
        else:
            acquired = await self._try_file_lock(timeout_seconds)
        if acquired:
            self._acquired = True
            logger.info(
                "Channel download lock acquired (holder=%s, pid=%s)",
                holder or "unknown",
                os.getpid(),
            )
        return acquired

    async def release(self) -> None:
        if not self._acquired:
            return
        if self.database is not None and self.database.engine == "mysql":
            await self.database.mysql_release_lock(GLOBAL_LOCK_NAME)
        else:
            await self._release_file_lock()
        self._acquired = False
        logger.info("Channel download lock released (pid=%s)", os.getpid())

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()
        return False

    async def _try_file_lock(self, timeout_seconds: int) -> bool:
        loop = asyncio.get_running_loop()
        lock_path = self.lock_dir / ".douyin_channel_download.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        def _blocking_acquire():
            import fcntl

            handle = open(str(lock_path), "a", encoding="utf-8")
            flags = fcntl.LOCK_EX
            if timeout_seconds == 0:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
                return handle
            except BlockingIOError:
                handle.close()
                return None

        if timeout_seconds <= 0:
            handle = await loop.run_in_executor(None, _blocking_acquire)
            if handle is None:
                return False
            self._file_handle = handle
            return True

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            handle = await loop.run_in_executor(None, _blocking_acquire)
            if handle is not None:
                self._file_handle = handle
                return True
            await asyncio.sleep(1)
        return False

    async def _release_file_lock(self) -> None:
        if self._file_handle is None:
            return
        loop = asyncio.get_running_loop()
        handle = self._file_handle
        self._file_handle = None

        def _blocking_release():
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

        await loop.run_in_executor(None, _blocking_release)
