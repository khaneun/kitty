"""포트폴리오 및 잔고 조회 유틸리티"""
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .logger import logger

_KST = ZoneInfo("Asia/Seoul")

if TYPE_CHECKING:
    from kitty.broker import KISBroker

_SNAPSHOT_PATH = Path("logs/portfolio_snapshot.json")


async def print_portfolio_and_balance(broker: "KISBroker", label: str = "") -> None:
    """포트폴리오 보유 종목과 주문 가능 잔고를 조회해 로그로 출력한다."""
    prefix = f"[{label}] " if label else ""

    try:
        balance_data = await broker.get_balance()
        available_cash = await broker.get_available_cash()
        holdings = [h for h in balance_data.get("output1", []) if int(h.get("hldg_qty", 0)) > 0]

        # ── 잔고 요약 ──────────────────────────────────────────
        summary = (balance_data.get("output2") or [{}])[0]
        total_eval = int(summary.get("tot_evlu_amt", 0))
        total_pnl  = int(summary.get("evlu_pfls_smtl_amt", 0))

        logger.info(f"{prefix}{'─' * 50}")
        logger.info(f"{prefix}💰 잔고  |  주문가능: {available_cash:>14,}원  |  총평가: {total_eval:>14,}원  |  평가손익: {total_pnl:>+,}원")

        # ── 보유 종목 ──────────────────────────────────────────
        holding_list = []
        if not holdings:
            logger.info(f"{prefix}📭 보유 종목 없음")
        else:
            logger.info(f"{prefix}{'종목코드':<8}  {'종목명':<14}  {'수량':>6}  {'평균단가':>10}  {'현재가':>10}  {'수익률':>8}  {'평가금액':>12}")
            logger.info(f"{prefix}{'─' * 80}")
            for h in holdings:
                symbol   = h.get("pdno", "")
                name     = h.get("prdt_name", "")[:12]
                qty      = int(h.get("hldg_qty", 0))
                avg      = int(float(h.get("pchs_avg_pric", 0)))
                eval_amt = int(h.get("evlu_amt", 0))
                pnl_amt  = int(h.get("evlu_pfls_amt", 0))   # KIS 평가손익금액 (원)
                pnl_rt   = float(h.get("evlu_pfls_rt", 0))
                cur      = int(eval_amt / qty) if qty else 0
                arrow    = "▲" if pnl_rt >= 0 else "▼"
                logger.info(
                    f"{prefix}{symbol:<8}  {name:<14}  {qty:>6,}주  {avg:>10,}원  "
                    f"{cur:>10,}원  {arrow}{abs(pnl_rt):>6.2f}%  {pnl_amt:>+,}원  {eval_amt:>12,}원"
                )
                holding_list.append({
                    "symbol":   symbol,
                    "name":     h.get("prdt_name", ""),
                    "qty":      qty,
                    "avg":      avg,
                    "current":  cur,
                    "eval_amt": eval_amt,
                    "pnl_amt":  pnl_amt,
                    "pnl_rt":   pnl_rt,
                })

        logger.info(f"{prefix}{'─' * 50}")

        # ── 스냅샷 저장 (monitor가 읽음) ───────────────────────
        try:
            from kitty.config import settings as _cfg
            snapshot = {
                "ts":            datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
                "trading_mode":  _cfg.trading_mode.value,
                "available_cash": available_cash,
                "total_eval":    total_eval,
                "total_pnl":     total_pnl,
                "holdings":      holding_list,
            }
            _SNAPSHOT_PATH.parent.mkdir(exist_ok=True)
            _SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"포트폴리오 스냅샷 저장 실패: {e}")

    except Exception as e:
        logger.error(f"{prefix}포트폴리오 조회 실패: {e}")
