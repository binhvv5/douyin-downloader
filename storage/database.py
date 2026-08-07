import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from storage.db_connection import DbConnection, open_mysql, open_sqlite
from storage.pipeline_handoff import build_asset_entries, find_local_source_mp4
from utils.logger import setup_logger
from utils.path_mapping import map_to_path2

logger = setup_logger("Database")


def create_database(config: Dict[str, Any]) -> "Database":
    engine = str(config.get("database_engine") or "sqlite").strip().lower()
    if engine == "mysql":
        mysql_cfg = config.get("database_mysql") or {}
        return Database(engine="mysql", mysql=mysql_cfg)
    db_path = config.get("database_path") or "dy_downloader.db"
    return Database(engine="sqlite", db_path=str(db_path))


class Database:
    def __init__(
        self,
        db_path: str = "dy_downloader.db",
        *,
        engine: str = "sqlite",
        mysql: Optional[Dict[str, Any]] = None,
    ):
        self.engine = str(engine or "sqlite").strip().lower()
        self.db_path = db_path
        self.mysql_config = dict(mysql or {})
        self._initialized = False
        self._conn: Optional[DbConnection] = None
        self._conn_lock: Optional[asyncio.Lock] = None

    def _placeholder(self, count: int = 1) -> str:
        token = "%s" if self.engine == "mysql" else "?"
        if count <= 1:
            return token
        return ", ".join([token] * count)

    def _in_clause(self, count: int) -> str:
        return ", ".join([self._placeholder()] * count)

    async def _get_conn(self) -> DbConnection:
        if self._conn is not None:
            return self._conn
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            if self._conn is None:
                if self.engine == "mysql":
                    self._conn = await open_mysql(self.mysql_config)
                else:
                    self._conn = await open_sqlite(self.db_path)
        return self._conn

    async def initialize(self):
        if self._initialized:
            return

        db = await self._get_conn()

        if self.engine == "mysql":
            await self._initialize_mysql(db)
        else:
            await self._initialize_sqlite(db)

        self._initialized = True

    async def _initialize_sqlite(self, db: DbConnection):
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS aweme (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT UNIQUE NOT NULL,
                aweme_type TEXT NOT NULL,
                title TEXT,
                author_id TEXT,
                author_name TEXT,
                create_time INTEGER,
                download_time INTEGER,
                file_path TEXT,
                metadata TEXT
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                url_type TEXT NOT NULL,
                download_time INTEGER,
                total_count INTEGER,
                success_count INTEGER,
                config TEXT
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id TEXT NOT NULL,
                video_path TEXT NOT NULL,
                transcript_dir TEXT,
                text_path TEXT,
                json_path TEXT,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                skip_reason TEXT,
                error_message TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                UNIQUE(aweme_id, video_path, model)
            )
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS job (
                job_id              TEXT PRIMARY KEY,
                url                 TEXT NOT NULL,
                status              TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                started_at          TEXT,
                finished_at         TEXT,
                total               INTEGER NOT NULL DEFAULT 0,
                success             INTEGER NOT NULL DEFAULT 0,
                failed              INTEGER NOT NULL DEFAULT 0,
                skipped             INTEGER NOT NULL DEFAULT 0,
                error               TEXT,
                author_nickname     TEXT,
                author_sec_uid      TEXT,
                retry_count         INTEGER NOT NULL DEFAULT 0,
                last_retry_at       TEXT,
                last_retry_summary  TEXT,
                retry_history       TEXT,
                overrides           TEXT
            )
        """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_aweme_id ON aweme(aweme_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_author_id ON aweme(author_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_download_time ON aweme(download_time)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcript_aweme_id ON transcript_job(aweme_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcript_status ON transcript_job(status)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_job_created_at ON job(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_job_status ON job(status)")

        cursor = await db.execute("PRAGMA table_info(aweme)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        if "author_sec_uid" not in existing_columns:
            await db.execute("ALTER TABLE aweme ADD COLUMN author_sec_uid TEXT")

        cursor = await db.execute("PRAGMA table_info(job)")
        existing_job_columns = {row[1] for row in await cursor.fetchall()}
        if "retry_history" not in existing_job_columns:
            await db.execute("ALTER TABLE job ADD COLUMN retry_history TEXT")

        await self._ensure_aweme_translation_columns(db, engine="sqlite")
        await self._ensure_aweme_pipeline_columns(db, engine="sqlite")
        await self._initialize_pipeline_tables(db, engine="sqlite")
        await self._ensure_channels_download_batch_size(db, engine="sqlite")
        await self._ensure_channels_pinned_and_like_columns(db, engine="sqlite")
        await self._ensure_file_path2_columns(db, engine="sqlite")

        await db.commit()

    async def _initialize_mysql(self, db: DbConnection):
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS aweme (
                id INT AUTO_INCREMENT PRIMARY KEY,
                aweme_id VARCHAR(64) NOT NULL,
                aweme_type VARCHAR(32) NOT NULL,
                title TEXT,
                author_id VARCHAR(64),
                author_name VARCHAR(255),
                author_sec_uid VARCHAR(128),
                create_time BIGINT,
                download_time BIGINT,
                file_path TEXT,
                metadata LONGTEXT,
                UNIQUE KEY uq_aweme_id (aweme_id),
                KEY idx_author_id (author_id),
                KEY idx_download_time (download_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS download_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                url TEXT NOT NULL,
                url_type VARCHAR(32) NOT NULL,
                download_time BIGINT,
                total_count INT,
                success_count INT,
                config LONGTEXT
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_job (
                id INT AUTO_INCREMENT PRIMARY KEY,
                aweme_id VARCHAR(64) NOT NULL,
                video_path TEXT NOT NULL,
                transcript_dir TEXT,
                text_path TEXT,
                json_path TEXT,
                model VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                skip_reason TEXT,
                error_message TEXT,
                created_at BIGINT,
                updated_at BIGINT,
                UNIQUE KEY uq_transcript_job (aweme_id, video_path(255), model),
                KEY idx_transcript_aweme_id (aweme_id),
                KEY idx_transcript_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS job (
                job_id              VARCHAR(64) PRIMARY KEY,
                url                 TEXT NOT NULL,
                status              VARCHAR(32) NOT NULL,
                created_at          VARCHAR(64) NOT NULL,
                started_at          VARCHAR(64),
                finished_at         VARCHAR(64),
                total               INT NOT NULL DEFAULT 0,
                success             INT NOT NULL DEFAULT 0,
                failed              INT NOT NULL DEFAULT 0,
                skipped             INT NOT NULL DEFAULT 0,
                error               TEXT,
                author_nickname     VARCHAR(255),
                author_sec_uid      VARCHAR(128),
                retry_count         INT NOT NULL DEFAULT 0,
                last_retry_at       VARCHAR(64),
                last_retry_summary  LONGTEXT,
                retry_history       LONGTEXT,
                overrides           LONGTEXT,
                KEY idx_job_created_at (created_at),
                KEY idx_job_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
        )

        db_name = str(self.mysql_config.get("database") or "douyin_downloader")
        cursor = await db.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'aweme' AND COLUMN_NAME = 'author_sec_uid'
            """,
            (db_name,),
        )
        if not await cursor.fetchone():
            await db.execute("ALTER TABLE aweme ADD COLUMN author_sec_uid VARCHAR(128)")

        cursor = await db.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'job' AND COLUMN_NAME = 'retry_history'
            """,
            (db_name,),
        )
        if not await cursor.fetchone():
            await db.execute("ALTER TABLE job ADD COLUMN retry_history LONGTEXT")

        await self._ensure_aweme_translation_columns(db, engine="mysql")
        await self._ensure_aweme_pipeline_columns(db, engine="mysql")
        await self._initialize_pipeline_tables(db, engine="mysql")
        await self._ensure_channels_download_batch_size(db, engine="mysql")
        await self._ensure_channels_pinned_and_like_columns(db, engine="mysql")
        await self._ensure_file_path2_columns(db, engine="mysql")

        await db.commit()

    async def _ensure_aweme_translation_columns(self, db: DbConnection, *, engine: str) -> None:
        columns = {
            "title_vi": "TEXT",
            "description_vi": "TEXT",
            "tags_vi": "TEXT",
        }
        if engine == "mysql":
            db_name = str(self.mysql_config.get("database") or "douyin_downloader")
            for column, col_type in columns.items():
                cursor = await db.execute(
                    """
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'aweme' AND COLUMN_NAME = %s
                    """,
                    (db_name, column),
                )
                if not await cursor.fetchone():
                    await db.execute(f"ALTER TABLE aweme ADD COLUMN {column} {col_type}")
            return

        cursor = await db.execute("PRAGMA table_info(aweme)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column, col_type in columns.items():
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE aweme ADD COLUMN {column} {col_type}")

    async def _ensure_aweme_pipeline_columns(self, db: DbConnection, *, engine: str) -> None:
        if engine == "mysql":
            columns = {
                "channel_id": "INT NULL",
                "download_status": "VARCHAR(32) NOT NULL DEFAULT 'success'",
                "created_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
            }
            db_name = str(self.mysql_config.get("database") or "douyin_downloader")
            for column, col_type in columns.items():
                cursor = await db.execute(
                    """
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'aweme' AND COLUMN_NAME = %s
                    """,
                    (db_name, column),
                )
                if not await cursor.fetchone():
                    await db.execute(f"ALTER TABLE aweme ADD COLUMN {column} {col_type}")
            return

        columns = {
            "channel_id": "INTEGER",
            "download_status": "TEXT NOT NULL DEFAULT 'success'",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }
        cursor = await db.execute("PRAGMA table_info(aweme)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column, col_type in columns.items():
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE aweme ADD COLUMN {column} {col_type}")

    async def _ensure_channels_download_batch_size(self, db: DbConnection, *, engine: str) -> None:
        if engine == "mysql":
            db_name = str(self.mysql_config.get("database") or "douyin_downloader")
            cursor = await db.execute(
                """
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'channels'
                  AND COLUMN_NAME = 'download_batch_size'
                """,
                (db_name,),
            )
            if not await cursor.fetchone():
                await db.execute(
                    """
                    ALTER TABLE channels
                    ADD COLUMN download_batch_size INT NOT NULL DEFAULT 10
                    """
                )
            return

        cursor = await db.execute("PRAGMA table_info(channels)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        if "download_batch_size" not in existing_columns:
            await db.execute(
                "ALTER TABLE channels ADD COLUMN download_batch_size INTEGER NOT NULL DEFAULT 10"
            )

    async def _ensure_channels_pinned_and_like_columns(
        self, db: DbConnection, *, engine: str
    ) -> None:
        if engine == "mysql":
            db_name = str(self.mysql_config.get("database") or "douyin_downloader")
            for column, col_type in (
                ("download_pinned", "TINYINT(1) NULL"),
                ("number_like", "INT NULL"),
            ):
                cursor = await db.execute(
                    """
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'channels'
                      AND COLUMN_NAME = %s
                    """,
                    (db_name, column),
                )
                if not await cursor.fetchone():
                    await db.execute(f"ALTER TABLE channels ADD COLUMN {column} {col_type}")
            return

        cursor = await db.execute("PRAGMA table_info(channels)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("download_pinned", "number_like"):
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE channels ADD COLUMN {column} INTEGER")

    async def _ensure_file_path2_columns(self, db: DbConnection, *, engine: str) -> None:
        aweme_column = "file_path2"
        video_assets_column = "file_path2"
        if engine == "mysql":
            db_name = str(self.mysql_config.get("database") or "douyin_downloader")
            for table, column in (("aweme", aweme_column), ("video_assets", video_assets_column)):
                cursor = await db.execute(
                    """
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (db_name, table, column),
                )
                if not await cursor.fetchone():
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NULL")
            return

        for table, column in (("aweme", aweme_column), ("video_assets", video_assets_column)):
            cursor = await db.execute(f"PRAGMA table_info({table})")
            existing_columns = {row[1] for row in await cursor.fetchall()}
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    async def _initialize_pipeline_tables(self, db: DbConnection, *, engine: str) -> None:
        if engine == "mysql":
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id              INT AUTO_INCREMENT PRIMARY KEY,
                    name            VARCHAR(255) NOT NULL,
                    douyin_url      TEXT NOT NULL,
                    sec_uid         VARCHAR(128) NULL,
                    enabled         TINYINT(1) NOT NULL DEFAULT 1,
                    sync_mode       ENUM('full', 'incremental') NOT NULL DEFAULT 'incremental',
                    download_batch_size INT NOT NULL DEFAULT 10,
                    download_pinned TINYINT(1) NULL,
                    number_like     INT NULL,
                    last_sync_at    DATETIME NULL,
                    notes           TEXT NULL,
                    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_channels_sec_uid (sec_uid),
                    KEY idx_channels_enabled (enabled)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS video_assets (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    aweme_id        VARCHAR(64) NOT NULL,
                    asset_type      ENUM(
                        'source_mp4', 'cover', 'music', 'metadata_json',
                        'transcript_zh', 'transcript_vi', 'dubbed_mp4', 'subtitle_vi'
                    ) NOT NULL,
                    file_path       TEXT NOT NULL,
                    file_size       BIGINT NULL,
                    checksum        VARCHAR(64) NULL,
                    mime_type       VARCHAR(128) NULL,
                    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_video_assets_aweme_type (aweme_id, asset_type),
                    KEY idx_video_assets_aweme_id (aweme_id),
                    KEY idx_video_assets_type (asset_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_jobs (
                    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                    aweme_id        VARCHAR(64) NOT NULL,
                    channel_id      INT NULL,
                    stage           ENUM(
                        'download', 'metadata_translate', 'dub',
                        'upload_facebook', 'upload_youtube'
                    ) NOT NULL,
                    status          ENUM(
                        'pending', 'processing', 'success', 'failed', 'skipped'
                    ) NOT NULL DEFAULT 'pending',
                    priority        INT NOT NULL DEFAULT 0,
                    attempt_count   INT NOT NULL DEFAULT 0,
                    max_attempts    INT NOT NULL DEFAULT 3,
                    locked_by       VARCHAR(64) NULL,
                    locked_at       DATETIME NULL,
                    started_at      DATETIME NULL,
                    finished_at     DATETIME NULL,
                    error_message   TEXT NULL,
                    result_json     JSON NULL,
                    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_pipeline_jobs_aweme_stage (aweme_id, stage),
                    KEY idx_pipeline_claim (stage, status, priority, created_at),
                    KEY idx_pipeline_aweme_id (aweme_id),
                    KEY idx_pipeline_channel_id (channel_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_accounts (
                    id                  INT AUTO_INCREMENT PRIMARY KEY,
                    platform            ENUM('facebook', 'youtube') NOT NULL,
                    account_name        VARCHAR(255) NOT NULL,
                    page_id             VARCHAR(128) NULL,
                    youtube_channel_id  VARCHAR(128) NULL,
                    access_token        TEXT NULL,
                    refresh_token       TEXT NULL,
                    token_expires_at    DATETIME NULL,
                    app_id              VARCHAR(128) NULL,
                    app_secret          VARCHAR(512) NULL,
                    client_id           VARCHAR(256) NULL,
                    client_secret       VARCHAR(512) NULL,
                    api_key             TEXT NULL,
                    enabled             TINYINT(1) NOT NULL DEFAULT 1,
                    daily_quota         INT NULL,
                    notes               TEXT NULL,
                    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_upload_accounts_platform_name (platform, account_name),
                    KEY idx_upload_accounts_enabled (enabled)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_records (
                    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
                    aweme_id            VARCHAR(64) NOT NULL,
                    platform            ENUM('facebook', 'youtube') NOT NULL,
                    account_id          INT NOT NULL,
                    platform_video_id   VARCHAR(128) NULL,
                    platform_url        TEXT NULL,
                    title_used          TEXT NULL,
                    description_used    TEXT NULL,
                    tags_used           JSON NULL,
                    status              ENUM('pending', 'uploading', 'success', 'failed') NOT NULL DEFAULT 'pending',
                    error_message       TEXT NULL,
                    uploaded_at         DATETIME NULL,
                    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_upload_records_aweme_platform_account (aweme_id, platform, account_id),
                    KEY idx_upload_records_status (status),
                    KEY idx_upload_records_platform (platform)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            )
            return

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                douyin_url      TEXT NOT NULL,
                sec_uid         TEXT,
                enabled         INTEGER NOT NULL DEFAULT 1,
                sync_mode       TEXT NOT NULL DEFAULT 'incremental',
                download_batch_size INTEGER NOT NULL DEFAULT 10,
                download_pinned INTEGER,
                number_like     INTEGER,
                last_sync_at    TEXT,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_channels_sec_uid ON channels(sec_uid)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_channels_enabled ON channels(enabled)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS video_assets (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id        TEXT NOT NULL,
                asset_type      TEXT NOT NULL,
                file_path       TEXT NOT NULL,
                file_size       INTEGER,
                checksum        TEXT,
                mime_type       TEXT,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(aweme_id, asset_type)
            )
        """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_assets_aweme_id ON video_assets(aweme_id)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id        TEXT NOT NULL,
                channel_id      INTEGER,
                stage           TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                priority        INTEGER NOT NULL DEFAULT 0,
                attempt_count   INTEGER NOT NULL DEFAULT 0,
                max_attempts    INTEGER NOT NULL DEFAULT 3,
                locked_by       TEXT,
                locked_at       TEXT,
                started_at      TEXT,
                finished_at     TEXT,
                error_message   TEXT,
                result_json     TEXT,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(aweme_id, stage)
            )
        """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_claim ON pipeline_jobs(stage, status, priority, created_at)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_accounts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                platform            TEXT NOT NULL,
                account_name        TEXT NOT NULL,
                page_id             TEXT,
                youtube_channel_id  TEXT,
                access_token        TEXT,
                refresh_token       TEXT,
                token_expires_at    TEXT,
                app_id              TEXT,
                app_secret          TEXT,
                client_id           TEXT,
                client_secret       TEXT,
                api_key             TEXT,
                enabled             INTEGER NOT NULL DEFAULT 1,
                daily_quota         INTEGER,
                notes               TEXT,
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, account_name)
            )
        """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_records (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                aweme_id            TEXT NOT NULL,
                platform            TEXT NOT NULL,
                account_id          INTEGER NOT NULL,
                platform_video_id   TEXT,
                platform_url        TEXT,
                title_used          TEXT,
                description_used    TEXT,
                tags_used           TEXT,
                status              TEXT NOT NULL DEFAULT 'pending',
                error_message       TEXT,
                uploaded_at         TEXT,
                created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(aweme_id, platform, account_id)
            )
        """
        )

    def _upsert_aweme_sql(self) -> str:
        cols = (
            "aweme_id, aweme_type, channel_id, title, author_id, author_name, author_sec_uid, "
            "create_time, download_time, download_status, file_path, file_path2, metadata, "
            "title_vi, description_vi, tags_vi"
        )
        values = self._placeholder(16)
        if self.engine == "mysql":
            return f"""
                INSERT INTO aweme ({cols}) VALUES ({values})
                ON DUPLICATE KEY UPDATE
                    aweme_type=VALUES(aweme_type),
                    channel_id=VALUES(channel_id),
                    title=VALUES(title),
                    author_id=VALUES(author_id),
                    author_name=VALUES(author_name),
                    author_sec_uid=VALUES(author_sec_uid),
                    create_time=VALUES(create_time),
                    download_time=VALUES(download_time),
                    download_status=VALUES(download_status),
                    file_path=VALUES(file_path),
                    file_path2=VALUES(file_path2),
                    metadata=VALUES(metadata),
                    title_vi=IF(VALUES(title_vi) IS NULL OR VALUES(title_vi) = '', title_vi, VALUES(title_vi)),
                    description_vi=IF(VALUES(description_vi) IS NULL, description_vi, VALUES(description_vi)),
                    tags_vi=IF(VALUES(tags_vi) IS NULL OR VALUES(tags_vi) = '', tags_vi, VALUES(tags_vi))
            """
        return f"""
            INSERT INTO aweme ({cols}) VALUES ({values})
            ON CONFLICT(aweme_id) DO UPDATE SET
                aweme_type=excluded.aweme_type,
                channel_id=excluded.channel_id,
                title=excluded.title,
                author_id=excluded.author_id,
                author_name=excluded.author_name,
                author_sec_uid=excluded.author_sec_uid,
                create_time=excluded.create_time,
                download_time=excluded.download_time,
                download_status=excluded.download_status,
                file_path=excluded.file_path,
                file_path2=excluded.file_path2,
                metadata=excluded.metadata,
                title_vi=CASE
                    WHEN excluded.title_vi IS NULL OR excluded.title_vi = '' THEN aweme.title_vi
                    ELSE excluded.title_vi
                END,
                description_vi=CASE
                    WHEN excluded.description_vi IS NULL THEN aweme.description_vi
                    ELSE excluded.description_vi
                END,
                tags_vi=CASE
                    WHEN excluded.tags_vi IS NULL OR excluded.tags_vi = '' THEN aweme.tags_vi
                    ELSE excluded.tags_vi
                END
        """

    def _aweme_row_values(self, aweme_data: Dict[str, Any], *, download_time: Optional[int] = None) -> tuple:
        tags_vi_raw = aweme_data.get("tags_vi")
        if isinstance(tags_vi_raw, list):
            tags_vi_value = json.dumps(tags_vi_raw, ensure_ascii=False)
        else:
            tags_vi_value = tags_vi_raw
        ts = download_time if download_time is not None else int(datetime.now().timestamp())
        return (
            aweme_data.get("aweme_id"),
            aweme_data.get("aweme_type"),
            aweme_data.get("channel_id"),
            aweme_data.get("title"),
            aweme_data.get("author_id"),
            aweme_data.get("author_name"),
            aweme_data.get("author_sec_uid"),
            aweme_data.get("create_time"),
            ts,
            aweme_data.get("download_status") or "success",
            aweme_data.get("file_path"),
            aweme_data.get("file_path2"),
            aweme_data.get("metadata"),
            aweme_data.get("title_vi"),
            aweme_data.get("description_vi"),
            tags_vi_value,
        )

    async def is_downloaded(self, aweme_id: str) -> bool:
        db = await self._get_conn()
        cursor = await db.execute(
            f"SELECT id FROM aweme WHERE aweme_id = {self._placeholder()}",
            (aweme_id,),
        )
        result = await cursor.fetchone()
        return result is not None

    async def add_aweme(
        self,
        aweme_data: Dict[str, Any],
        *,
        author_sec_uid: Optional[str] = None,
    ):
        db = await self._get_conn()
        sec_uid = author_sec_uid if author_sec_uid is not None else aweme_data.get("author_sec_uid")
        if sec_uid is not None and aweme_data.get("author_sec_uid") is None:
            aweme_data = dict(aweme_data)
            aweme_data["author_sec_uid"] = sec_uid
        sql = self._upsert_aweme_sql()
        await db.execute(sql, self._aweme_row_values(aweme_data))
        await db.commit()

    async def add_aweme_batch(self, items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        db = await self._get_conn()
        now_ts = int(datetime.now().timestamp())
        rows = [self._aweme_row_values(item, download_time=now_ts) for item in items]
        sql = self._upsert_aweme_sql()
        if self.engine == "mysql":
            for row in rows:
                await db.execute(sql, row)
        else:
            await db.executemany(sql, rows)
        await db.commit()

    async def get_latest_aweme_time(self, author_id: str) -> Optional[int]:
        db = await self._get_conn()
        cursor = await db.execute(
            f"SELECT MAX(create_time) FROM aweme WHERE author_id = {self._placeholder()}",
            (author_id,),
        )
        result = await cursor.fetchone()
        return result[0] if result and result[0] else None

    async def add_history(self, history_data: Dict[str, Any]):
        db = await self._get_conn()
        await db.execute(
            f"""
            INSERT INTO download_history
            (url, url_type, download_time, total_count, success_count, config)
            VALUES ({self._placeholder(6)})
        """,
            (
                history_data.get("url"),
                history_data.get("url_type"),
                int(datetime.now().timestamp()),
                history_data.get("total_count"),
                history_data.get("success_count"),
                history_data.get("config"),
            ),
        )
        await db.commit()

    async def get_aweme_history(
        self,
        *,
        page: int = 1,
        size: int = 50,
        author: Optional[str] = None,
        date_from: Optional[int] = None,
        date_to: Optional[int] = None,
        aweme_type: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = await self._get_conn()
        where: list = []
        params: list = []
        if author:
            where.append(f"author_name = {self._placeholder()}")
            params.append(author)
        if date_from is not None:
            where.append(f"create_time >= {self._placeholder()}")
            params.append(int(date_from))
        if date_to is not None:
            where.append(f"create_time <= {self._placeholder()}")
            params.append(int(date_to))
        if aweme_type:
            where.append(f"aweme_type = {self._placeholder()}")
            params.append(aweme_type)
        if title:
            where.append(f"LOWER(COALESCE(title, '')) LIKE {self._placeholder()}")
            params.append(f"%{title.lower()}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        cursor = await db.execute(f"SELECT COUNT(*) FROM aweme {where_sql}", params)
        row = await cursor.fetchone()
        total = int(row[0]) if row else 0

        offset = max(0, (page - 1) * size)
        cursor = await db.execute(
            f"SELECT aweme_id, aweme_type, title, author_id, author_name, "
            f"author_sec_uid, create_time, download_time, file_path FROM aweme "
            f"{where_sql} ORDER BY download_time DESC, id DESC "
            f"LIMIT {self._placeholder()} OFFSET {self._placeholder()}",
            params + [int(size), int(offset)],
        )
        rows = await cursor.fetchall()
        items = [
            {
                "aweme_id": r[0],
                "aweme_type": r[1],
                "title": r[2],
                "author_id": r[3],
                "author_name": r[4],
                "author_sec_uid": r[5],
                "create_time": r[6],
                "download_time": r[7],
                "file_path": r[8],
            }
            for r in rows
        ]
        return {"total": total, "page": int(page), "size": int(size), "items": items}

    async def get_aweme_count_by_author(self, author_id: str) -> int:
        db = await self._get_conn()
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM aweme WHERE author_id = {self._placeholder()}",
            (author_id,),
        )
        result = await cursor.fetchone()
        return result[0] if result else 0

    async def get_top_authors(self, *, days: int, limit: int) -> List[Dict[str, Any]]:
        cutoff = int(datetime.now().timestamp()) - int(days) * 86400
        db = await self._get_conn()
        cursor = await db.execute(
            f"""
            SELECT a.author_sec_uid,
                   (SELECT a2.author_name FROM aweme a2
                     WHERE a2.author_sec_uid = a.author_sec_uid
                       AND a2.author_name IS NOT NULL
                       AND a2.author_name != ''
                     ORDER BY a2.download_time DESC
                     LIMIT 1) AS author_name,
                   COUNT(*) AS download_count
              FROM aweme a
             WHERE a.create_time >= {self._placeholder()}
               AND a.author_sec_uid IS NOT NULL
               AND a.author_sec_uid != ''
             GROUP BY a.author_sec_uid
             ORDER BY download_count DESC, a.author_sec_uid ASC
             LIMIT {self._placeholder()}
            """,
            (cutoff, int(limit)),
        )
        rows = await cursor.fetchall()
        return [
            {
                "sec_uid": row[0],
                "author_name": row[1] if row[1] else "Unknown author",
                "download_count": int(row[2]),
            }
            for row in rows
        ]

    async def upsert_transcript_job(self, job_data: Dict[str, Any]):
        now_ts = int(datetime.now().timestamp())
        db = await self._get_conn()
        if self.engine == "mysql":
            sql = f"""
                INSERT INTO transcript_job (
                    aweme_id, video_path, transcript_dir, text_path, json_path,
                    model, status, skip_reason, error_message, created_at, updated_at
                )
                VALUES ({self._placeholder(11)})
                ON DUPLICATE KEY UPDATE
                    transcript_dir=VALUES(transcript_dir),
                    text_path=VALUES(text_path),
                    json_path=VALUES(json_path),
                    status=VALUES(status),
                    skip_reason=VALUES(skip_reason),
                    error_message=VALUES(error_message),
                    updated_at=VALUES(updated_at)
            """
        else:
            sql = f"""
                INSERT INTO transcript_job (
                    aweme_id, video_path, transcript_dir, text_path, json_path,
                    model, status, skip_reason, error_message, created_at, updated_at
                )
                VALUES ({self._placeholder(11)})
                ON CONFLICT(aweme_id, video_path, model) DO UPDATE SET
                    transcript_dir = excluded.transcript_dir,
                    text_path = excluded.text_path,
                    json_path = excluded.json_path,
                    status = excluded.status,
                    skip_reason = excluded.skip_reason,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
            """
        await db.execute(
            sql,
            (
                job_data.get("aweme_id"),
                job_data.get("video_path"),
                job_data.get("transcript_dir"),
                job_data.get("text_path"),
                job_data.get("json_path"),
                job_data.get("model") or "gpt-4o-mini-transcribe",
                job_data.get("status"),
                job_data.get("skip_reason"),
                job_data.get("error_message"),
                now_ts,
                now_ts,
            ),
        )
        await db.commit()

    async def get_transcript_job(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_conn()
        cursor = await db.execute(
            f"""
            SELECT aweme_id, video_path, transcript_dir, text_path, json_path,
                   model, status, skip_reason, error_message, created_at, updated_at
            FROM transcript_job
            WHERE aweme_id = {self._placeholder()}
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (aweme_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "aweme_id": row[0],
            "video_path": row[1],
            "transcript_dir": row[2],
            "text_path": row[3],
            "json_path": row[4],
            "model": row[5],
            "status": row[6],
            "skip_reason": row[7],
            "error_message": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }

    async def delete_aweme_by_ids(self, aweme_ids: List[str]) -> int:
        if not aweme_ids:
            return 0
        seen: Dict[str, None] = {}
        for aid in aweme_ids:
            if aid not in seen:
                seen[aid] = None
        unique_ids = list(seen.keys())

        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        deleted = 0
        chunk_size = 500
        async with self._conn_lock:
            for start in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[start : start + chunk_size]
                placeholders = self._in_clause(len(chunk))
                cursor = await db.execute(
                    f"DELETE FROM aweme WHERE aweme_id IN ({placeholders})",
                    chunk,
                )
                if cursor.rowcount is not None and cursor.rowcount > 0:
                    deleted += cursor.rowcount
            await db.commit()
        return deleted

    async def truncate_history(self) -> None:
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            await db.execute("DELETE FROM aweme")
            await db.execute("DELETE FROM download_history")
            await db.commit()

    async def upsert_job(self, job_dict: Dict[str, Any]) -> None:
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()

        last_retry_summary = job_dict.get("last_retry_summary")
        retry_history = job_dict.get("retry_history")
        overrides = job_dict.get("overrides")
        params = (
            job_dict.get("job_id"),
            job_dict.get("url") or "",
            job_dict.get("status") or "",
            job_dict.get("created_at") or "",
            job_dict.get("started_at"),
            job_dict.get("finished_at"),
            int(job_dict.get("total") or 0),
            int(job_dict.get("success") or 0),
            int(job_dict.get("failed") or 0),
            int(job_dict.get("skipped") or 0),
            job_dict.get("error"),
            job_dict.get("author_nickname"),
            job_dict.get("author_sec_uid"),
            int(job_dict.get("retry_count") or 0),
            job_dict.get("last_retry_at"),
            json.dumps(last_retry_summary) if last_retry_summary else None,
            json.dumps(retry_history) if retry_history else None,
            json.dumps(overrides) if overrides else None,
        )
        cols = (
            "job_id, url, status, created_at, started_at, finished_at, "
            "total, success, failed, skipped, error, author_nickname, author_sec_uid, "
            "retry_count, last_retry_at, last_retry_summary, retry_history, overrides"
        )
        if self.engine == "mysql":
            sql = f"""
                INSERT INTO job ({cols}) VALUES ({self._placeholder(18)})
                ON DUPLICATE KEY UPDATE
                    url=VALUES(url),
                    status=VALUES(status),
                    created_at=VALUES(created_at),
                    started_at=VALUES(started_at),
                    finished_at=VALUES(finished_at),
                    total=VALUES(total),
                    success=VALUES(success),
                    failed=VALUES(failed),
                    skipped=VALUES(skipped),
                    error=VALUES(error),
                    author_nickname=VALUES(author_nickname),
                    author_sec_uid=VALUES(author_sec_uid),
                    retry_count=VALUES(retry_count),
                    last_retry_at=VALUES(last_retry_at),
                    last_retry_summary=VALUES(last_retry_summary),
                    retry_history=VALUES(retry_history),
                    overrides=VALUES(overrides)
            """
        else:
            sql = f"""
                INSERT OR REPLACE INTO job ({cols}) VALUES ({self._placeholder(18)})
            """
        async with self._conn_lock:
            await db.execute(sql, params)
            await db.commit()

    async def delete_jobs(self, job_ids: List[str]) -> int:
        if not job_ids:
            return 0
        seen: Dict[str, None] = {}
        for jid in job_ids:
            if jid and jid not in seen:
                seen[jid] = None
        unique_ids = list(seen.keys())
        if not unique_ids:
            return 0

        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        deleted = 0
        chunk_size = 500
        async with self._conn_lock:
            for start in range(0, len(unique_ids), chunk_size):
                chunk = unique_ids[start : start + chunk_size]
                placeholders = self._in_clause(len(chunk))
                cursor = await db.execute(
                    f"DELETE FROM job WHERE job_id IN ({placeholders})",
                    chunk,
                )
                if cursor.rowcount is not None and cursor.rowcount > 0:
                    deleted += cursor.rowcount
            await db.commit()
        return deleted

    async def load_terminal_jobs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        db = await self._get_conn()
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()

        sql = (
            "SELECT job_id, url, status, created_at, started_at, finished_at, "
            "total, success, failed, skipped, error, author_nickname, "
            "author_sec_uid, retry_count, last_retry_at, last_retry_summary, "
            "retry_history, overrides FROM job "
            "WHERE status IN ('success', 'failed', 'cancelled') "
            "ORDER BY created_at DESC"
        )
        if limit is not None and limit > 0:
            sql += f" LIMIT {int(limit)}"

        async with self._conn_lock:
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            summary_raw = row[15]
            history_raw = row[16]
            overrides_raw = row[17]
            try:
                summary = json.loads(summary_raw) if summary_raw else None
            except (TypeError, ValueError):
                summary = None
            try:
                history = json.loads(history_raw) if history_raw else []
                if not isinstance(history, list):
                    history = []
            except (TypeError, ValueError):
                history = []
            try:
                overrides = json.loads(overrides_raw) if overrides_raw else None
            except (TypeError, ValueError):
                overrides = None
            result.append(
                {
                    "job_id": row[0],
                    "url": row[1],
                    "status": row[2],
                    "created_at": row[3],
                    "started_at": row[4],
                    "finished_at": row[5],
                    "total": row[6] or 0,
                    "success": row[7] or 0,
                    "failed": row[8] or 0,
                    "skipped": row[9] or 0,
                    "error": row[10],
                    "author_nickname": row[11],
                    "author_sec_uid": row[12],
                    "retry_count": row[13] or 0,
                    "last_retry_at": row[14],
                    "last_retry_summary": summary,
                    "retry_history": history,
                    "overrides": overrides,
                }
            )
        return result

    async def upsert_channel(
        self,
        *,
        name: str,
        douyin_url: str,
        sec_uid: Optional[str] = None,
        enabled: int = 1,
        sync_mode: str = "incremental",
        notes: Optional[str] = None,
    ) -> int:
        db = await self._get_conn()
        if sec_uid:
            cursor = await db.execute(
                f"SELECT id FROM channels WHERE sec_uid = {self._placeholder()}",
                (sec_uid,),
            )
            row = await cursor.fetchone()
            if row:
                channel_id = int(row[0])
                await db.execute(
                    f"""
                    UPDATE channels SET name = {self._placeholder()},
                        douyin_url = {self._placeholder()}, enabled = {self._placeholder()},
                        sync_mode = {self._placeholder()}, notes = {self._placeholder()}
                    WHERE id = {self._placeholder()}
                    """,
                    (name, douyin_url, enabled, sync_mode, notes, channel_id),
                )
                await db.commit()
                return channel_id

        cursor = await db.execute(
            f"SELECT id FROM channels WHERE douyin_url = {self._placeholder()}",
            (douyin_url,),
        )
        row = await cursor.fetchone()
        if row:
            channel_id = int(row[0])
            await db.execute(
                f"""
                UPDATE channels SET name = {self._placeholder()}, sec_uid = {self._placeholder()},
                    enabled = {self._placeholder()}, sync_mode = {self._placeholder()},
                    notes = {self._placeholder()}
                WHERE id = {self._placeholder()}
                """,
                (name, sec_uid, enabled, sync_mode, notes, channel_id),
            )
            await db.commit()
            return channel_id

        if self.engine == "mysql":
            await db.execute(
                f"""
                INSERT INTO channels (name, douyin_url, sec_uid, enabled, sync_mode, notes)
                VALUES ({self._placeholder(6)})
                """,
                (name, douyin_url, sec_uid, enabled, sync_mode, notes),
            )
            cursor = await db.execute("SELECT LAST_INSERT_ID()")
            row = await cursor.fetchone()
            await db.commit()
            return int(row[0])

        await db.execute(
            f"""
            INSERT INTO channels (name, douyin_url, sec_uid, enabled, sync_mode, notes)
            VALUES ({self._placeholder(6)})
            """,
            (name, douyin_url, sec_uid, enabled, sync_mode, notes),
        )
        cursor = await db.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        await db.commit()
        return int(row[0])

    async def ensure_channel_for_user(
        self,
        douyin_url: str,
        sec_uid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> int:
        label = name or sec_uid or douyin_url
        if len(label) > 255:
            label = label[:252] + "..."
        return await self.upsert_channel(name=label, douyin_url=douyin_url, sec_uid=sec_uid)

    async def sync_channels_from_urls(self, urls: Sequence[str]) -> None:
        from core.url_parser import URLParser

        for url in urls:
            if not url:
                continue
            parsed = URLParser.parse(url)
            sec_uid = parsed.get("sec_uid") if parsed else None
            name = sec_uid or url
            await self.ensure_channel_for_user(url, sec_uid=sec_uid, name=name)

    async def get_enabled_channels(self) -> List[Dict[str, Any]]:
        db = await self._get_conn()
        cursor = await db.execute(
            """
            SELECT id, name, douyin_url, sec_uid, enabled, sync_mode,
                   download_batch_size, last_sync_at, download_pinned, number_like
            FROM channels WHERE enabled = 1 ORDER BY id ASC
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "douyin_url": row[2],
                "sec_uid": row[3],
                "enabled": row[4],
                "sync_mode": row[5],
                "download_batch_size": row[6],
                "last_sync_at": row[7],
                "download_pinned": row[8],
                "number_like": row[9],
            }
            for row in rows
        ]

    async def resolve_download_urls(self, config_links: Sequence[str]) -> List[str]:
        channels = await self.get_enabled_channels()
        if channels:
            return [str(c["douyin_url"]) for c in channels if c.get("douyin_url")]
        return list(config_links)

    async def pick_next_channel_for_sync(self) -> Optional[Dict[str, Any]]:
        db = await self._get_conn()
        cursor = await db.execute(
            """
            SELECT id, name, douyin_url, sec_uid, enabled, sync_mode,
                   download_batch_size, last_sync_at, download_pinned, number_like
            FROM channels
            WHERE enabled = 1
            ORDER BY last_sync_at IS NULL DESC, last_sync_at ASC, id ASC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "douyin_url": row[2],
            "sec_uid": row[3],
            "enabled": row[4],
            "sync_mode": row[5],
            "download_batch_size": row[6],
            "last_sync_at": row[7],
            "download_pinned": row[8],
            "number_like": row[9],
        }

    async def get_channel_daily_video_limit(self, channel_id: int, *, default: int = 5) -> int:
        """
        daily_video_limit từ channel_pipeline_configs.
        NULL / thiếu row -> default (5).
        0 -> không giới hạn (trả về 0).
        """
        db = await self._get_conn()
        try:
            cursor = await db.execute(
                f"""
                SELECT daily_video_limit
                FROM channel_pipeline_configs
                WHERE channel_id = {self._placeholder()}
                """,
                (int(channel_id),),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.warning(
                "channel_pipeline_configs unavailable; using default daily_video_limit=%s",
                default,
            )
            return int(default)

        if not row or row[0] is None:
            return int(default)
        return max(0, int(row[0]))

    async def get_channel_use_chatgpt_html_proxy(
        self, channel_id: int, *, default: bool = False
    ) -> bool:
        db = await self._get_conn()
        try:
            cursor = await db.execute(
                f"""
                SELECT use_chatgpt_html_proxy
                FROM channel_pipeline_configs
                WHERE channel_id = {self._placeholder()}
                """,
                (int(channel_id),),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.warning(
                "channel_pipeline_configs.use_chatgpt_html_proxy unavailable; default=%s",
                default,
            )
            return bool(default)

        if not row or row[0] is None:
            return bool(default)
        return bool(int(row[0]))

    async def get_channel_target_language(
        self, channel_id: int, *, default: str = "vi"
    ) -> str:
        db = await self._get_conn()
        try:
            cursor = await db.execute(
                f"""
                SELECT target_language
                FROM channel_pipeline_configs
                WHERE channel_id = {self._placeholder()}
                """,
                (int(channel_id),),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.warning(
                "channel_pipeline_configs.target_language unavailable; default=%s",
                default,
            )
            return "en" if str(default).strip().lower() in {"en", "eng", "english"} else "vi"

        if not row or row[0] is None:
            return "en" if str(default).strip().lower() in {"en", "eng", "english"} else "vi"
        text = str(row[0]).strip().lower()
        if text in {"en", "eng", "english"}:
            return "en"
        return "vi"

    async def get_channel_movie_topic(
        self, channel_id: int, *, default: bool = False
    ) -> bool:
        db = await self._get_conn()
        try:
            cursor = await db.execute(
                f"""
                SELECT movie_topic
                FROM channel_pipeline_configs
                WHERE channel_id = {self._placeholder()}
                """,
                (int(channel_id),),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.warning(
                "channel_pipeline_configs.movie_topic unavailable; default=%s",
                default,
            )
            return bool(default)

        if not row or row[0] is None:
            return bool(default)
        return bool(int(row[0]))

    async def count_channel_downloads_today(self, channel_id: int) -> int:
        """Số aweme download success của channel trong ngày lịch hiện tại (theo DB server time)."""
        db = await self._get_conn()
        if self.engine == "mysql":
            cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM aweme
                WHERE channel_id = {self._placeholder()}
                  AND download_status = 'success'
                  AND download_time IS NOT NULL
                  AND download_time >= UNIX_TIMESTAMP(CURDATE())
                  AND download_time < UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY)
                """,
                (int(channel_id),),
            )
        else:
            cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM aweme
                WHERE channel_id = {self._placeholder()}
                  AND download_status = 'success'
                  AND download_time IS NOT NULL
                  AND download_time >= strftime('%s', 'now', 'start of day')
                  AND download_time < strftime('%s', 'now', 'start of day', '+1 day')
                """,
                (int(channel_id),),
            )
        row = await cursor.fetchone()
        return int(row[0] or 0) if row else 0

    async def get_channel_daily_download_quota(self, channel_id: int, *, default_limit: int = 5) -> Dict[str, Any]:
        limit = await self.get_channel_daily_video_limit(channel_id, default=default_limit)
        if limit == 0:
            return {"limit": 0, "used": 0, "remaining": None}
        used = await self.count_channel_downloads_today(channel_id)
        remaining = max(0, limit - used)
        return {"limit": limit, "used": used, "remaining": remaining}

    async def mysql_get_lock(self, lock_name: str, timeout_seconds: int) -> bool:
        if self.engine != "mysql":
            return False
        db = await self._get_conn()
        cursor = await db.execute(
            "SELECT GET_LOCK(%s, %s)",
            (lock_name, int(timeout_seconds)),
        )
        row = await cursor.fetchone()
        return bool(row and row[0] == 1)

    async def mysql_release_lock(self, lock_name: str) -> None:
        if self.engine != "mysql":
            return
        db = await self._get_conn()
        await db.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))

    async def update_channel_last_sync(self, channel_id: int) -> None:
        db = await self._get_conn()
        if self.engine == "mysql":
            await db.execute(
                "UPDATE channels SET last_sync_at = NOW() WHERE id = %s",
                (channel_id,),
            )
        else:
            await db.execute(
                f"UPDATE channels SET last_sync_at = {self._placeholder()} WHERE id = {self._placeholder()}",
                (datetime.now().isoformat(sep=" ", timespec="seconds"), channel_id),
            )
        await db.commit()

    async def upsert_video_asset(
        self,
        *,
        aweme_id: str,
        asset_type: str,
        file_path: str,
        file_size: Optional[int] = None,
        mime_type: Optional[str] = None,
        checksum: Optional[str] = None,
        file_path2: Optional[str] = None,
    ) -> None:
        db = await self._get_conn()
        if self.engine == "mysql":
            sql = f"""
                INSERT INTO video_assets
                (aweme_id, asset_type, file_path, file_path2, file_size, checksum, mime_type)
                VALUES ({self._placeholder(7)})
                ON DUPLICATE KEY UPDATE
                    file_path=VALUES(file_path),
                    file_path2=VALUES(file_path2),
                    file_size=VALUES(file_size),
                    checksum=VALUES(checksum),
                    mime_type=VALUES(mime_type)
            """
        else:
            sql = f"""
                INSERT INTO video_assets
                (aweme_id, asset_type, file_path, file_path2, file_size, checksum, mime_type)
                VALUES ({self._placeholder(7)})
                ON CONFLICT(aweme_id, asset_type) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_path2 = excluded.file_path2,
                    file_size = excluded.file_size,
                    checksum = excluded.checksum,
                    mime_type = excluded.mime_type
            """
        await db.execute(
            sql,
            (aweme_id, asset_type, file_path, file_path2, file_size, checksum, mime_type),
        )

    async def get_pipeline_job_status(self, aweme_id: str, stage: str) -> Optional[str]:
        db = await self._get_conn()
        cursor = await db.execute(
            f"""
            SELECT status FROM pipeline_jobs
            WHERE aweme_id = {self._placeholder()} AND stage = {self._placeholder()}
            """,
            (aweme_id, stage),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_aweme_vi_content(self, aweme_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_conn()
        cursor = await db.execute(
            f"""
            SELECT title_vi, description_vi, tags_vi
            FROM aweme
            WHERE aweme_id = {self._placeholder()}
            """,
            (aweme_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        tags_raw = row[2]
        tags_vi: List[str] = []
        if isinstance(tags_raw, str) and tags_raw.strip():
            try:
                parsed = json.loads(tags_raw)
                if isinstance(parsed, list):
                    tags_vi = [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                tags_vi = []
        elif isinstance(tags_raw, list):
            tags_vi = [str(x).strip() for x in tags_raw if str(x).strip()]
        return {
            "title_vi": str(row[0] or "").strip(),
            "description_vi": str(row[1] or "").strip(),
            "tags_vi": tags_vi,
        }

    async def try_claim_metadata_translate(
        self,
        *,
        aweme_id: str,
        channel_id: Optional[int] = None,
    ) -> bool:
        db = await self._get_conn()
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        if self.engine == "mysql":
            await db.execute(
                f"""
                INSERT INTO pipeline_jobs (aweme_id, channel_id, stage, status)
                VALUES ({self._placeholder(4)})
                ON DUPLICATE KEY UPDATE aweme_id = aweme_id
                """,
                (aweme_id, channel_id, "metadata_translate", "pending"),
            )
            cursor = await db.execute(
                f"""
                UPDATE pipeline_jobs
                SET status = 'processing',
                    channel_id = COALESCE({self._placeholder()}, channel_id),
                    error_message = NULL,
                    started_at = NOW(),
                    finished_at = NULL
                WHERE aweme_id = {self._placeholder()}
                  AND stage = 'metadata_translate'
                  AND status NOT IN ('processing', 'success')
                """,
                (channel_id, aweme_id),
            )
        else:
            await db.execute(
                f"""
                INSERT OR IGNORE INTO pipeline_jobs (aweme_id, channel_id, stage, status)
                VALUES ({self._placeholder(4)})
                """,
                (aweme_id, channel_id, "metadata_translate", "pending"),
            )
            cursor = await db.execute(
                f"""
                UPDATE pipeline_jobs
                SET status = 'processing',
                    channel_id = COALESCE({self._placeholder()}, channel_id),
                    error_message = NULL,
                    started_at = {self._placeholder()},
                    finished_at = NULL
                WHERE aweme_id = {self._placeholder()}
                  AND stage = 'metadata_translate'
                  AND status NOT IN ('processing', 'success')
                """,
                (channel_id, now, aweme_id),
            )
        await db.commit()
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    async def upsert_pipeline_job(
        self,
        *,
        aweme_id: str,
        stage: str,
        status: str,
        channel_id: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        db = await self._get_conn()
        if self.engine == "mysql":
            sql = f"""
                INSERT INTO pipeline_jobs
                (aweme_id, channel_id, stage, status, error_message, finished_at)
                VALUES ({self._placeholder(6)})
                ON DUPLICATE KEY UPDATE
                    channel_id=VALUES(channel_id),
                    status=VALUES(status),
                    error_message=VALUES(error_message),
                    finished_at=CASE
                        WHEN VALUES(status) IN ('success', 'failed', 'skipped')
                        THEN NOW() ELSE finished_at END
            """
            finished = datetime.now().isoformat(sep=" ", timespec="seconds")
        else:
            sql = f"""
                INSERT INTO pipeline_jobs
                (aweme_id, channel_id, stage, status, error_message, finished_at)
                VALUES ({self._placeholder(6)})
                ON CONFLICT(aweme_id, stage) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    finished_at = excluded.finished_at
            """
            finished = datetime.now().isoformat(sep=" ", timespec="seconds")
        await db.execute(
            sql,
            (aweme_id, channel_id, stage, status, error_message, finished),
        )

    async def ensure_dub_pending_if_missing(
        self,
        *,
        aweme_id: str,
        channel_id: Optional[int] = None,
    ) -> None:
        existing = await self.get_pipeline_job_status(aweme_id, "dub")
        if existing is not None:
            return
        db = await self._get_conn()
        await db.execute(
            f"""
            INSERT INTO pipeline_jobs (aweme_id, channel_id, stage, status)
            VALUES ({self._placeholder(4)})
            """,
            (aweme_id, channel_id, "dub", "pending"),
        )

    async def _upsert_video_assets_from_files(
        self,
        aweme_id: str,
        downloaded_files: Sequence[Union[Path, str]],
        *,
        base_path: Optional[Union[Path, str]] = None,
        path2: Optional[Union[Path, str]] = None,
    ) -> None:
        for entry in build_asset_entries(
            [Path(p) for p in downloaded_files],
            base_path=base_path,
            path2=path2,
        ):
            await self.upsert_video_asset(
                aweme_id=aweme_id,
                asset_type=entry["asset_type"],
                file_path=entry["file_path"],
                file_path2=entry.get("file_path2"),
                file_size=entry.get("file_size"),
                mime_type=entry.get("mime_type"),
            )

    async def complete_download_handoff(
        self,
        *,
        aweme_id: str,
        channel_id: Optional[int],
        downloaded_files: Sequence[Union[Path, str]],
        metadata_translate_ok: Optional[bool],
        translation_enabled: bool,
        download_status: str = "success",
        commit: bool = True,
        base_path: Optional[Union[Path, str]] = None,
        path2: Optional[Union[Path, str]] = None,
    ) -> None:
        db = await self._get_conn()
        await self._upsert_video_assets_from_files(
            aweme_id,
            downloaded_files,
            base_path=base_path,
            path2=path2,
        )
        await self.upsert_pipeline_job(
            aweme_id=aweme_id,
            stage="download",
            status=download_status,
            channel_id=channel_id,
        )
        if not translation_enabled:
            await self.upsert_pipeline_job(
                aweme_id=aweme_id,
                stage="metadata_translate",
                status="skipped",
                channel_id=channel_id,
            )
        elif metadata_translate_ok is True:
            await self.upsert_pipeline_job(
                aweme_id=aweme_id,
                stage="metadata_translate",
                status="success",
                channel_id=channel_id,
            )
        elif metadata_translate_ok is False:
            await self.upsert_pipeline_job(
                aweme_id=aweme_id,
                stage="metadata_translate",
                status="failed",
                channel_id=channel_id,
            )
        await self.ensure_dub_pending_if_missing(aweme_id=aweme_id, channel_id=channel_id)
        if commit:
            await db.commit()

    async def handoff_skipped_download(
        self,
        *,
        aweme_id: str,
        channel_id: Optional[int],
        base_path: Union[Path, str],
        path2: Optional[Union[Path, str]] = None,
        commit: bool = True,
    ) -> None:
        db = await self._get_conn()
        source_mp4 = find_local_source_mp4(Path(base_path), aweme_id)
        if source_mp4:
            file_path2 = map_to_path2(source_mp4, base_path, path2) if path2 else None
            await self.upsert_video_asset(
                aweme_id=aweme_id,
                asset_type="source_mp4",
                file_path=str(source_mp4),
                file_path2=file_path2,
                file_size=source_mp4.stat().st_size,
                mime_type="video/mp4",
            )
        await self.upsert_pipeline_job(
            aweme_id=aweme_id,
            stage="download",
            status="skipped",
            channel_id=channel_id,
        )
        await self.upsert_pipeline_job(
            aweme_id=aweme_id,
            stage="metadata_translate",
            status="skipped",
            channel_id=channel_id,
        )
        await self.ensure_dub_pending_if_missing(aweme_id=aweme_id, channel_id=channel_id)
        if commit:
            await db.commit()

    async def mark_download_failed(
        self,
        *,
        aweme_id: str,
        channel_id: Optional[int] = None,
        error_message: Optional[str] = None,
        commit: bool = True,
    ) -> None:
        db = await self._get_conn()
        await self.upsert_pipeline_job(
            aweme_id=aweme_id,
            stage="download",
            status="failed",
            channel_id=channel_id,
            error_message=error_message,
        )
        if commit:
            await db.commit()

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
