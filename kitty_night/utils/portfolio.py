"""Night mode 포트폴리오 스냅샷 — USD 기준"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kitty_night.utils import logger

_KST = ZoneInfo("Asia/Seoul")
_SNAPSHOT_PATH = Path("night-logs/night_portfolio_snapshot.json")


def save_portfolio_snapshot(
    trading_mode: str,
    available_usd: float,
    total_eval_usd: float,
    total_pnl_usd: float,
    holdings: list[dict[str, Any]],
) -> None:
    """포트폴리오 스냅샷을 night-logs/에 저장"""
    try:
        _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
            "trading_mode": trading_mode,
            "currency": "USD",
            "available_cash": round(available_usd, 2),
            "total_eval": round(total_eval_usd, 2),
            "total_pnl": round(total_pnl_usd, 2),
            "holdings": holdings,
        }
        _SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug(f"[Night] portfolio snapshot saved: ${total_eval_usd:,.2f}")
    except Exception as e:
        logger.warning(f"[Night] portfolio snapshot save failed: {e}")
