from pathlib import Path

from utils.logger import configure_app_logging, reset_logging_for_tests, setup_logger


def test_configure_app_logging_writes_file(tmp_path: Path):
    reset_logging_for_tests()
    log_path = configure_app_logging(
        {
            "logging": {
                "enabled": True,
                "dir": str(tmp_path / "logs"),
                "file": "test-downloader.log",
                "level": "INFO",
                "max_bytes": 1024 * 1024,
                "backup_count": 2,
            }
        },
        project_root=tmp_path,
    )
    assert log_path is not None
    assert log_path.exists() or True

    logger = setup_logger("LoggerTestModule")
    logger.info("[step] start download aweme_id=123 title=hello")
    logger.warning("sample warning")

    for handler in list(logger.handlers):
        if hasattr(handler, "flush"):
            handler.flush()

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "File logging enabled" in content
    assert "[step] start download aweme_id=123" in content
    assert "sample warning" in content
    reset_logging_for_tests()


def test_configure_app_logging_can_disable(tmp_path: Path):
    reset_logging_for_tests()
    path = configure_app_logging(
        {"logging": {"enabled": False, "dir": str(tmp_path / "logs")}},
        project_root=tmp_path,
    )
    assert path is None
    reset_logging_for_tests()
