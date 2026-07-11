from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence, Tuple

import aiosqlite

try:
    import aiomysql
except ImportError:  # pragma: no cover - optional dependency
    aiomysql = None  # type: ignore


class ExecResult:
    def __init__(
        self,
        cursor: Any,
        *,
        rowcount: Optional[int] = None,
        buffered_rows: Optional[Sequence[Tuple[Any, ...]]] = None,
    ):
        if buffered_rows is not None:
            self._buffered_rows = list(buffered_rows)
            self.rowcount = rowcount if rowcount is not None else len(self._buffered_rows)
            self._cursor = None
        elif cursor is not None:
            rc = getattr(cursor, "rowcount", None)
            self.rowcount = rc if rc is not None else -1
            self._cursor = cursor
            self._buffered_rows = None
        else:
            self.rowcount = rowcount if rowcount is not None else -1
            self._cursor = None
            self._buffered_rows = None

    async def _close_cursor(self) -> None:
        if self._cursor is not None:
            await self._cursor.close()
            self._cursor = None

    async def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._buffered_rows is not None:
            if not self._buffered_rows:
                return None
            return self._buffered_rows.pop(0)
        if self._cursor is None:
            return None
        row = await self._cursor.fetchone()
        await self._close_cursor()
        return row

    async def fetchall(self) -> Sequence[Tuple[Any, ...]]:
        if self._buffered_rows is not None:
            rows = self._buffered_rows
            self._buffered_rows = []
            return rows
        if self._cursor is None:
            return []
        rows = await self._cursor.fetchall()
        await self._close_cursor()
        return rows


class DbConnection:
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        raise NotImplementedError

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        raise NotImplementedError

    async def commit(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SqliteDbConnection(DbConnection):
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        cursor = await self._conn.execute(sql, tuple(params))
        return ExecResult(cursor)

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        await self._conn.executemany(sql, [tuple(row) for row in params_seq])

    async def commit(self) -> None:
        await self._conn.commit()

    async def close(self) -> None:
        await self._conn.close()


class MysqlDbConnection(DbConnection):
    def __init__(self, conn: Any):
        self._conn = conn
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_read_query(sql: str) -> bool:
        head = sql.lstrip().upper()
        return head.startswith("SELECT") or head.startswith("SHOW") or head.startswith("DESCRIBE")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        async with self._lock:
            cursor = await self._conn.cursor()
            await cursor.execute(sql, tuple(params))
            if self._is_read_query(sql):
                rows = await cursor.fetchall()
                rowcount = cursor.rowcount if cursor.rowcount is not None else len(rows)
                await cursor.close()
                return ExecResult(None, rowcount=rowcount, buffered_rows=rows)
            rowcount = cursor.rowcount if cursor.rowcount is not None else 0
            await cursor.close()
            return ExecResult(None, rowcount=rowcount)

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        async with self._lock:
            cursor = await self._conn.cursor()
            await cursor.executemany(sql, [tuple(row) for row in params_seq])
            await cursor.close()

    async def commit(self) -> None:
        async with self._lock:
            await self._conn.commit()

    async def close(self) -> None:
        self._conn.close()


async def open_sqlite(db_path: str) -> SqliteDbConnection:
    conn = await aiosqlite.connect(db_path)
    return SqliteDbConnection(conn)


async def open_mysql(mysql_config: dict) -> MysqlDbConnection:
    if aiomysql is None:
        raise RuntimeError(
            "MySQL backend requires aiomysql. Install with: pip install aiomysql"
        )

    host = str(mysql_config.get("host") or "127.0.0.1")
    port = int(mysql_config.get("port") or 3306)
    user = str(mysql_config.get("user") or "root")
    password = str(mysql_config.get("password") or "")
    database = str(mysql_config.get("database") or "douyin_downloader")

    conn = await aiomysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        db=database,
        charset="utf8mb4",
        autocommit=False,
    )
    return MysqlDbConnection(conn)


async def open_connection(
    *,
    engine: str,
    db_path: str,
    mysql_config: Optional[dict] = None,
    conn_lock: Optional[asyncio.Lock] = None,
    existing: Optional[DbConnection] = None,
) -> DbConnection:
    if existing is not None:
        return existing
    if engine == "mysql":
        return await open_mysql(mysql_config or {})
    return await open_sqlite(db_path)
