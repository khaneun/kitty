import sys

from loguru import logger


def setup_logger(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )
    logger.add(
        "logs/kitty_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level=level,
        encoding="utf-8",
    )
    # ERROR/WARNING/CRITICAL 전용 로그 — monitor가 이 파일만 스캔
    logger.add(
        "logs/kitty_errors_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        level="WARNING",
        encoding="utf-8",
    )


__all__ = ["logger", "setup_logger"]
