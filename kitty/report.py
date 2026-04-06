"""Daily Report - 매매 이력 및 에이전트 의견 기록"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

from kitty.utils import logger

REPORTS_DIR = Path("reports")
RETAIN_DAYS = 30


class CycleRecord:
    """매매 사이클 1회 기록"""

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


class DailyReport:
    """하루치 매매 리포트 - 사이클마다 누적하고 파일로 저장"""

    def __init__(self) -> None:
        self.date: str = datetime.now(_KST).strftime("%Y-%m-%d")
        self.cycles: list[CycleRecord] = []
        self._current: CycleRecord | None = None
        REPORTS_DIR.mkdir(exist_ok=True)
        self._cleanup_old_reports()

    def begin_cycle(self) -> None:
        """새 사이클 시작"""
        self._current = CycleRecord()

    def record_analysis(self, analysis: dict[str, Any]) -> None:
        """시장분석가 결과 기록"""
        if self._current:
            self._current.market_analysis = analysis
            sectors = analysis.get("sectors", [])
            candidate_count = sum(len(s.get("candidate_symbols", [])) for s in sectors)
            logger.info(
                f"[리포트] 시장분석 - 분위기:{analysis.get('market_sentiment')} "
                f"리스크:{analysis.get('risk_level')} "
                f"분석섹터:{len(sectors)}개 후보종목:{candidate_count}개"
            )

    def record_stock_evaluation(self, evaluation: dict[str, Any]) -> None:
        """종목평가 결과 기록"""
        if self._current:
            self._current.stock_evaluation = evaluation
            evaluations = evaluation.get("evaluations", [])
            hold = [e for e in evaluations if e.get("action") == "HOLD"]
            buy_more = [e for e in evaluations if e.get("action") == "BUY_MORE"]
            partial = [e for e in evaluations if e.get("action") == "PARTIAL_SELL"]
            sell = [e for e in evaluations if e.get("action") == "SELL"]
            logger.info(
                f"[리포트] 종목평가 - 유지:{len(hold)} 추가매수:{len(buy_more)} "
                f"일부매도:{len(partial)} 전량매도:{len(sell)} "
                f"| {evaluation.get('summary', '')[:60]}"
            )
            for e in evaluations:
                logger.debug(
                    f"         [{e.get('action')}] {e.get('symbol')} {e.get('name')} "
                    f"수익률:{e.get('pnl_rate', 0):+.1f}% "
                    f"섹터:{e.get('sector_trend', '-')} "
                    f"| {e.get('reason', '')[:60]}"
                )

    def record_stock_picks(self, strategy: dict[str, Any]) -> None:
        """종목발굴 결정 기록"""
        if self._current:
            self._current.stock_picks = strategy
            decisions = strategy.get("decisions", [])
            buys = [d for d in decisions if d.get("action") == "BUY"]
            sells = [d for d in decisions if d.get("action") == "SELL"]
            logger.info(
                f"[리포트] 종목발굴 - 매수:{len(buys)}건 매도:{len(sells)}건 "
                f"요약:{strategy.get('strategy_summary', '')[:60]}"
            )
            for d in decisions:
                logger.debug(
                    f"         {d.get('action')} {d.get('symbol')} "
                    f"{d.get('quantity')}주 @ {d.get('price', 0):,}원 "
                    f"손절:{d.get('stop_loss', '-')} 목표:{d.get('take_profit', '-')} "
                    f"| {d.get('reason', '')}"
                )

    def record_asset_management(self, result: dict[str, Any]) -> None:
        """자산운용 결정 기록"""
        if self._current:
            self._current.asset_management = result
            final_orders = result.get("final_orders", [])
            buys = [o for o in final_orders if o.get("action") in ("BUY", "BUY_MORE")]
            sells = [o for o in final_orders if o.get("action") in ("SELL", "PARTIAL_SELL")]
            logger.info(
                f"[리포트] 자산운용 - 매수:{len(buys)}건 매도:{len(sells)}건 "
                f"현금비율:{result.get('cash_reserve_ratio', 0):.1%} "
                f"| {result.get('summary', '')[:60]}"
            )
            for o in final_orders:
                logger.debug(
                    f"         [{o.get('priority', 'NORMAL')}] {o.get('action')} {o.get('symbol')} "
                    f"{o.get('quantity')}주 order_type:{o.get('order_type', 'SINGLE')} "
                    f"| {o.get('reason', '')[:60]}"
                )

    def record_executions(
        self,
        buy_results: list[dict[str, Any]],
        sell_results: list[dict[str, Any]],
    ) -> None:
        """체결 결과 기록"""
        if self._current:
            self._current.buy_results = buy_results
            self._current.sell_results = sell_results
            for r in buy_results:
                status = r.get("status", "")
                _n, _s = r.get("name", ""), r.get("symbol", "")
                _lbl = f"{_n}({_s})" if _n else _s
                logger.info(f"[리포트] 매수체결 - {_lbl} {status} | {r.get('reason', r.get('order_id', ''))}")
            for r in sell_results:
                status = r.get("status", "")
                _n, _s = r.get("name", ""), r.get("symbol", "")
                _lbl = f"{_n}({_s})" if _n else _s
                logger.info(f"[리포트] 매도체결 - {_lbl} {status} | {r.get('reason', r.get('order_id', ''))}")

    def end_cycle(self) -> None:
        """사이클 종료 및 파일 저장"""
        if self._current:
            self.cycles.append(self._current)
            self._current = None
            self._save()

    def _cleanup_old_reports(self) -> None:
        """보관 기한(30일) 초과 리포트 파일 삭제"""
        cutoff = (datetime.now(_KST) - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
        for f in REPORTS_DIR.glob("*.json"):
            if f.stem < cutoff:
                try:
                    f.unlink()
                    logger.info(f"[리포트] 오래된 파일 삭제: {f.name}")
                except OSError as e:
                    logger.warning(f"[리포트] 파일 삭제 실패: {f.name}: {e}")

    def _save(self) -> None:
        path = REPORTS_DIR / f"{self.date}.json"
        data = {
            "date": self.date,
            "total_cycles": len(self.cycles),
            "cycles": [c.to_dict() for c in self.cycles],
            "summary": self._build_summary(),
        }
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            logger.debug(f"[리포트] 저장 완료 → {path} ({path.stat().st_size:,}B)")
        except OSError as e:
            logger.error(f"[리포트] 저장 실패 (디스크 용량 확인 필요): {e}")

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
        """텔레그램용 일일 요약 메시지"""
        s = self._build_summary()
        lines = [
            f"📋 *일일 매매 리포트 ({self.date})*",
            f"총 사이클: {len(self.cycles)}회",
            f"매수체결: {s['total_buy_orders']}건",
            f"매도체결: {s['total_sell_orders']}건",
        ]
        if s["traded_symbols"]:
            lines.append(f"거래종목: {', '.join(s['traded_symbols'])}")
        if s["market_sentiments"]:
            lines.append(f"시장분위기: {' → '.join(s['market_sentiments'])}")
        lines.append(f"상세: `reports/{self.date}.json`")
        return "\n".join(lines)
