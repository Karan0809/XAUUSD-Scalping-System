import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger
from config.settings import get_settings


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_record["lvl"] = record.levelname
        log_record["mod"] = f"{record.name}:{record.funcName}:{record.lineno}"
        if "trade" in log_record:
            log_record.pop("trade", None)


def setup_logging() -> None:
    settings = get_settings()

    sys.stdout.reconfigure(line_buffering=True)

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    json_formatter = CustomJsonFormatter(
        fmt="%(ts)s %(lvl)s %(mod)s %(message)s",
        timestamp=True,
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    console_handler.setFormatter(json_formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_dir / "trading.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    error_handler = RotatingFileHandler(
        log_dir / "error.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(json_formatter)
    root_logger.addHandler(error_handler)

    trade_logger = logging.getLogger("trade")
    trade_logger.setLevel(logging.INFO)
    trade_handler = RotatingFileHandler(
        log_dir / "trades.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=30,
        encoding="utf-8",
    )
    trade_handler.setFormatter(json_formatter)
    trade_logger.addHandler(trade_handler)
    trade_logger.propagate = False

    logging.getLogger("MetaTrader5").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    root_logger.info(
        f"Logging initialized - level={settings.log_level}, dir={log_dir}",
        extra={"log_level": settings.log_level, "log_dir": str(log_dir)},
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
