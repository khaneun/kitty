"""Night mode 전용 로거 — night-logs/ 디렉토리에 별도 저장"""
import sys

from loguru import logger


def setup_night_logger(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )
    logger.add(
        "night-logs/kitty-night_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level=level,
        encoding="utf-8",
    )
    # ERROR/WARNING/CRITICAL 전용 로그 — monitor가 이 파일만 스캔
    logger.add(
        "night-logs/kitty-night_errors_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level="WARNING",
        encoding="utf-8",
    )


__all__ = ["logger", "setup_night_logger"]
