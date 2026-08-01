import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

_APP_LOGGER_PREFIX = "dy-downloader"
_KNOWN_LOGGER_NAMES = set()
_FILE_HANDLER: Optional[logging.Handler] = None
_FILE_LOG_PATH: Optional[Path] = None
_DEFAULT_FORMAT = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logger(
    name: str = "dy-downloader",
    level: int = logging.INFO,
    log_file: str = None,
    console_level: int = logging.ERROR,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    _KNOWN_LOGGER_NAMES.add(name)

    has_console = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in logger.handlers
    )
    if not has_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(console_level)
        console_handler.setFormatter(_DEFAULT_FORMAT)
        logger.addHandler(console_handler)

    if log_file:
        _attach_dedicated_file_handler(logger, log_file, level)
    elif _FILE_HANDLER is not None and _FILE_HANDLER not in logger.handlers:
        logger.addHandler(_FILE_HANDLER)

    return logger


def configure_app_logging(config: Optional[Dict[str, Any]] = None, *, project_root: Optional[Path] = None) -> Optional[Path]:
    """Attach a shared rotating file handler to all known module loggers.

    Config keys under ``logging``:
      enabled (default True), dir, file, level, max_bytes, backup_count
    """
    global _FILE_HANDLER, _FILE_LOG_PATH

    cfg = config or {}
    if isinstance(cfg.get("logging"), dict):
        log_cfg = dict(cfg.get("logging") or {})
    else:
        log_cfg = {}

    if not _as_bool(log_cfg.get("enabled", True), default=True):
        return _FILE_LOG_PATH

    root = Path(project_root) if project_root else Path.cwd()
    log_dir = Path(str(log_cfg.get("dir") or "logs"))
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    filename = str(log_cfg.get("file") or "douyin-downloader.log")
    log_path = log_dir / filename
    level = _parse_level(log_cfg.get("level"), default=logging.INFO)
    max_bytes = int(log_cfg.get("max_bytes") or 20 * 1024 * 1024)
    backup_count = int(log_cfg.get("backup_count") or 10)

    if _FILE_HANDLER is not None and _FILE_LOG_PATH == log_path:
        _FILE_HANDLER.setLevel(level)
        _attach_shared_file_handler_to_known()
        return _FILE_LOG_PATH

    if _FILE_HANDLER is not None:
        for name in list(_KNOWN_LOGGER_NAMES):
            logger = logging.getLogger(name)
            if _FILE_HANDLER in logger.handlers:
                logger.removeHandler(_FILE_HANDLER)
        try:
            _FILE_HANDLER.close()
        except Exception:
            pass
        _FILE_HANDLER = None

    handler = RotatingFileHandler(
        log_path,
        maxBytes=max(1024 * 1024, max_bytes),
        backupCount=max(1, backup_count),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(_DEFAULT_FORMAT)
    _FILE_HANDLER = handler
    _FILE_LOG_PATH = log_path
    _attach_shared_file_handler_to_known()

    bootstrap = setup_logger("Logging")
    bootstrap.info("File logging enabled: %s (level=%s)", log_path, logging.getLevelName(level))
    return log_path


def get_log_file_path() -> Optional[Path]:
    return _FILE_LOG_PATH


def set_console_log_level(level: int) -> None:
    for name in _KNOWN_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(level)


def _attach_shared_file_handler_to_known() -> None:
    if _FILE_HANDLER is None:
        return
    for name in list(_KNOWN_LOGGER_NAMES):
        logger = logging.getLogger(name)
        if _FILE_HANDLER not in logger.handlers:
            logger.addHandler(_FILE_HANDLER)
        logger.setLevel(min(logger.level or logging.INFO, _FILE_HANDLER.level))


def _attach_dedicated_file_handler(logger: logging.Logger, log_file: str, level: int) -> None:
    log_path = Path(log_file)
    if log_path.parent != Path("."):
        log_path.parent.mkdir(parents=True, exist_ok=True)
    already = any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == str(log_path.resolve())
        for handler in logger.handlers
    )
    if already:
        return
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(_DEFAULT_FORMAT)
    logger.addHandler(file_handler)


def _parse_level(value: Any, default: int = logging.INFO) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    return getattr(logging, text, default)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def reset_logging_for_tests() -> None:
    """Detach shared file handler — tests only."""
    global _FILE_HANDLER, _FILE_LOG_PATH
    if _FILE_HANDLER is not None:
        for name in list(_KNOWN_LOGGER_NAMES):
            logger = logging.getLogger(name)
            if _FILE_HANDLER in logger.handlers:
                logger.removeHandler(_FILE_HANDLER)
        try:
            _FILE_HANDLER.close()
        except Exception:
            pass
    _FILE_HANDLER = None
    _FILE_LOG_PATH = None
