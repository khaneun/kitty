"""포트폴리오 및 잔고 조회 유틸리티"""
from typing import TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    from kitty.broker import KISBroker


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
                pnl_rt   = float(h.get("evlu_pfls_rt", 0))
                cur      = int(eval_amt / qty) if qty else 0
                arrow    = "▲" if pnl_rt >= 0 else "▼"
                logger.info(
                    f"{prefix}{symbol:<8}  {name:<14}  {qty:>6,}주  {avg:>10,}원  "
                    f"{cur:>10,}원  {arrow}{abs(pnl_rt):>6.2f}%  {eval_amt:>12,}원"
                )

        logger.info(f"{prefix}{'─' * 50}")

    except Exception as e:
        logger.error(f"{prefix}포트폴리오 조회 실패: {e}")
