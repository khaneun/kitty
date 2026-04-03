"""Night mode Daily Report — trade history and agent decisions per cycle (USD)"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kitty_night.utils import logger

_KST = ZoneInfo("Asia/Seoul")
REPORTS_DIR = Path("night-reports")


class NightCycleRecord:
    """Single trading cycle record"""

    def __init__(self) -> None:
        self.timestamp: str = datetime.now(_KST).strftime("%H:%M:%S")
        self.market_analysis: dict[str, Any] = {}
        self.stock_evaluation: dict[str, Any] = {}
        self.stock_picks: dict[str, Any] = {}
        self.asset_management: dict[str, Any] = {}
        self.buy_results: list[dict[str, Any]] = []
        self.sell_results: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "market_analysis": self.market_analysis,
            "stock_evaluation": self.stock_evaluation,
            "stock_picks": self.stock_picks,
            "asset_management": self.asset_management,
            "buy_results": self.buy_results,
            "sell_results": self.sell_results,
        }


class NightDailyReport:
    """Daily trade report — accumulates cycles and saves to file"""

    def __init__(self) -> None:
        self.date: str = datetime.now(_KST).strftime("%Y-%m-%d")
        self.cycles: list[NightCycleRecord] = []
        self._current: NightCycleRecord | None = None
        REPORTS_DIR.mkdir(exist_ok=True)

    def begin_cycle(self) -> None:
        self._current = NightCycleRecord()

    def record_analysis(self, analysis: dict[str, Any]) -> None:
        if self._current:
            self._current.market_analysis = analysis
            sectors = analysis.get("sectors", [])
            candidate_count = sum(len(s.get("candidate_symbols", [])) for s in sectors)
            logger.info(
                f"[Night:Report] Sector analysis — sentiment:{analysis.get('market_sentiment')} "
                f"risk:{analysis.get('risk_level')} "
                f"sectors:{len(sectors)} candidates:{candidate_count}"
            )

    def record_stock_evaluation(self, evaluation: dict[str, Any]) -> None:
        if self._current:
            self._current.stock_evaluation = evaluation
            evaluations = evaluation.get("evaluations", [])
            hold = [e for e in evaluations if e.get("action") == "HOLD"]
            buy_more = [e for e in evaluations if e.get("action") == "BUY_MORE"]
            partial = [e for e in evaluations if e.get("action") == "PARTIAL_SELL"]
            sell = [e for e in evaluations if e.get("action") == "SELL"]
            logger.info(
                f"[Night:Report] Evaluation — HOLD:{len(hold)} BUY_MORE:{len(buy_more)} "
                f"PARTIAL_SELL:{len(partial)} SELL:{len(sell)} "
                f"| {evaluation.get('summary', '')[:60]}"
            )

    def record_stock_picks(self, strategy: dict[str, Any]) -> None:
        if self._current:
            self._current.stock_picks = strategy
            decisions = strategy.get("decisions", [])
            buys = [d for d in decisions if d.get("action") == "BUY"]
            logger.info(
                f"[Night:Report] Stock picks — BUY:{len(buys)} "
                f"summary:{strategy.get('strategy_summary', '')[:60]}"
            )

    def record_asset_management(self, result: dict[str, Any]) -> None:
        if self._current:
            self._current.asset_management = result
            final_orders = result.get("final_orders", [])
            buys = [o for o in final_orders if o.get("action") in ("BUY", "BUY_MORE")]
            sells = [o for o in final_orders if o.get("action") in ("SELL", "PARTIAL_SELL")]
            logger.info(
                f"[Night:Report] Asset mgmt — buys:{len(buys)} sells:{len(sells)} "
                f"| {result.get('summary', '')[:60]}"
            )

    def record_executions(
        self,
        buy_results: list[dict[str, Any]],
        sell_results: list[dict[str, Any]],
    ) -> None:
        if self._current:
            self._current.buy_results = buy_results
            self._current.sell_results = sell_results
            for r in buy_results:
                _lbl = f"{r.get('name', '')}({r.get('symbol', '')})"
                logger.info(f"[Night:Report] BUY {_lbl} {r.get('status', '')} | {r.get('order_id', '')}")
            for r in sell_results:
                _lbl = f"{r.get('name', '')}({r.get('symbol', '')})"
                logger.info(f"[Night:Report] SELL {_lbl} {r.get('status', '')} | {r.get('order_id', '')}")

    def end_cycle(self) -> None:
        if self._current:
            self.cycles.append(self._current)
            self._current = None
            self._save()

    def _save(self) -> None:
        path = REPORTS_DIR / f"night_{self.date}.json"
        data = {
            "date": self.date,
            "market": "US",
            "currency": "USD",
            "total_cycles": len(self.cycles),
            "cycles": [c.to_dict() for c in self.cycles],
            "summary": self._build_summary(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[Night:Report] Saved → {path}")

    def _build_summary(self) -> dict[str, Any]:
        all_buys = [r for c in self.cycles for r in c.buy_results if r.get("status") not in ("SKIPPED", "FAILED")]
        all_sells = [r for c in self.cycles for r in c.sell_results if r.get("status") not in ("SKIPPED", "FAILED")]
        sentiments = [c.market_analysis.get("market_sentiment", "") for c in self.cycles if c.market_analysis]
        return {
            "total_buy_orders": len(all_buys),
            "total_sell_orders": len(all_sells),
            "market_sentiments": sentiments,
            "traded_symbols": list({r.get("symbol") for r in all_buys + all_sells}),
        }

    def telegram_summary(self) -> str:
        s = self._build_summary()
        lines = [
            f"🌙 *Night Mode Report ({self.date})*",
            f"Cycles: {len(self.cycles)}",
            f"Buys: {s['total_buy_orders']}",
            f"Sells: {s['total_sell_orders']}",
        ]
        if s["traded_symbols"]:
            lines.append(f"Traded: {', '.join(s['traded_symbols'])}")
        if s["market_sentiments"]:
            lines.append(f"Sentiment: {' → '.join(s['market_sentiments'])}")
        lines.append(f"Detail: `night-reports/night_{self.date}.json`")
        return "\n".join(lines)
