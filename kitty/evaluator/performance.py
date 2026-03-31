"""장 마감 후 에이전트 성과 평가 엔진

평가 흐름:
  1. 오늘 DailyReport에서 각 에이전트의 결정 수집
  2. KIS API로 EOD 가격/등락률 조회
  3. 에이전트별 정량 지표 계산
  4. AI로 자연어 피드백 + 개선 포인트 생성
  5. feedback/*.json 에 저장 (BaseAgent가 다음 사이클부터 system_prompt에 주입)
"""
import json
import re
from typing import Any

from kitty.config import AIProvider, settings
from kitty.feedback.store import append_entry
from kitty.report import DailyReport
from kitty.utils import logger


class PerformanceEvaluator:
    """장 마감 후 에이전트별 성과 평가"""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    # ──────────────────────────────────────────
    # 진입점
    # ──────────────────────────────────────────

    async def run(self, daily_report: DailyReport) -> dict[str, Any]:
        """평가 실행 후 에이전트명 → 평가 결과 딕셔너리 반환"""
        if not daily_report.cycles:
            logger.info("[평가] 오늘 사이클 없음 — 평가 건너뜀")
            return {}

        logger.info("[평가] 장 마감 성과 평가 시작")

        symbols = self._collect_symbols(daily_report)
        if not symbols:
            logger.info("[평가] 분석 종목 없음")
            return {}

        eod = await self._fetch_eod(symbols)
        logger.info(f"[평가] EOD 가격 수집: {len(eod)}개 종목")

        results: dict[str, Any] = {}
        for agent_name, eval_fn in [
            ("섹터분석가", self._eval_sector_analyst),
            ("종목발굴가", self._eval_stock_picker),
            ("종목평가가", self._eval_stock_evaluator),
            ("자산운용가", self._eval_asset_manager),
            ("매수실행가", self._eval_buy_executor),
            ("매도실행가", self._eval_sell_executor),
        ]:
            try:
                metrics = eval_fn(daily_report, eod)
                if not metrics:
                    continue
                feedback = await self._ai_feedback(agent_name, metrics)
                entry = {
                    "date": daily_report.date,
                    "score": metrics["score"],
                    "summary": feedback.get("summary", f"점수 {metrics['score']}/10"),
                    "improvement": feedback.get("improvement", ""),
                    "metrics": metrics,
                }
                append_entry(agent_name, entry)
                results[agent_name] = entry
                logger.info(f"[평가] {agent_name}: {entry['score']}/10 — {entry['summary']}")
            except Exception as e:
                logger.error(f"[평가] {agent_name} 평가 오류: {e}")

        return results

    # ──────────────────────────────────────────
    # 데이터 수집
    # ──────────────────────────────────────────

    def _collect_symbols(self, report: DailyReport) -> set[str]:
        symbols: set[str] = set()
        for c in report.cycles:
            for s in c.market_analysis.get("sectors", []):
                symbols.update(s.get("candidate_symbols", []))
            for d in c.stock_picks.get("decisions", []):
                symbols.add(d.get("symbol", ""))
            for e in c.stock_evaluation.get("evaluations", []):
                symbols.add(e.get("symbol", ""))
            for r in c.buy_results + c.sell_results:
                symbols.add(r.get("symbol", ""))
        return {s for s in symbols if s}

    async def _fetch_eod(self, symbols: set[str]) -> dict[str, dict]:
        eod: dict[str, dict] = {}
        for sym in symbols:
            try:
                q = await self._broker.get_quote(sym)
                eod[sym] = {
                    "price": q.current_price,
                    "change_rate": q.change_rate,
                    "name": q.name,
                }
            except Exception as e:
                logger.warning(f"[평가] {sym} EOD 조회 실패: {e}")
        return eod

    # ──────────────────────────────────────────
    # 에이전트별 평가
    # ──────────────────────────────────────────

    def _eval_sector_analyst(self, report: DailyReport, eod: dict) -> dict:
        """섹터 방향 예측 적중률"""
        hits, total = 0, 0
        sector_details = []

        for c in report.cycles:
            for s in c.market_analysis.get("sectors", []):
                trend = s.get("trend")
                if trend == "neutral":
                    continue
                candidates = [sym for sym in s.get("candidate_symbols", []) if sym in eod]
                if not candidates:
                    continue
                avg_chg = sum(eod[sym]["change_rate"] for sym in candidates) / len(candidates)
                correct = (trend == "bullish" and avg_chg > 0) or (trend == "bearish" and avg_chg < 0)
                hits += int(correct)
                total += 1
                sector_details.append({
                    "sector": s.get("name"), "predicted": trend,
                    "avg_change": round(avg_chg, 2), "correct": correct,
                })

        if total == 0:
            return {}

        acc = hits / total
        return {
            "score": min(10, round(acc * 10 + 1)),  # +1 보정으로 5 이상부터 시작
            "accuracy": round(acc, 2),
            "hits": hits, "total": total,
            "sector_details": sector_details,
            "market_sentiment": report.cycles[-1].market_analysis.get("market_sentiment"),
        }

    def _eval_stock_picker(self, report: DailyReport, eod: dict) -> dict:
        """추천 종목의 당일 수익률"""
        buy_changes: list[float] = []
        sell_correct, sell_total = 0, 0

        for c in report.cycles:
            for d in c.stock_picks.get("decisions", []):
                sym, action = d.get("symbol"), d.get("action")
                if not sym or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                if action == "BUY":
                    buy_changes.append(chg)
                elif action == "SELL":
                    sell_total += 1
                    if chg < 0:
                        sell_correct += 1

        if not buy_changes and sell_total == 0:
            return {}

        avg_return = sum(buy_changes) / len(buy_changes) if buy_changes else None
        sell_acc = sell_correct / sell_total if sell_total > 0 else None

        score = 5
        if avg_return is not None:
            if avg_return > 2:     score = 9
            elif avg_return > 0.5: score = 7
            elif avg_return > 0:   score = 6
            elif avg_return > -1:  score = 4
            else:                  score = 2

        return {
            "score": score,
            "buy_count": len(buy_changes),
            "avg_buy_return": round(avg_return, 2) if avg_return is not None else None,
            "sell_accuracy": round(sell_acc, 2) if sell_acc is not None else None,
        }

    def _eval_stock_evaluator(self, report: DailyReport, eod: dict) -> dict:
        """보유 종목 HOLD/BUY_MORE/SELL 판단 정확도"""
        correct, total = 0, 0
        details = []

        for c in report.cycles:
            for e in c.stock_evaluation.get("evaluations", []):
                sym, action = e.get("symbol"), e.get("action")
                if not sym or not action or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                hit = (
                    (action == "HOLD"         and -3 <= chg <= 5) or
                    (action == "BUY_MORE"     and chg > 0)        or
                    (action == "PARTIAL_SELL" and chg < 2)        or
                    (action == "SELL"         and chg < 0)
                )
                correct += int(hit)
                total += 1
                details.append({"symbol": sym, "action": action, "change": chg, "correct": hit})

        if total == 0:
            return {}

        acc = correct / total
        return {
            "score": round(acc * 10),
            "accuracy": round(acc, 2),
            "correct": correct, "total": total,
            "details": details,
        }

    def _eval_asset_manager(self, report: DailyReport, eod: dict) -> dict:
        """최종 주문 결정의 방향성 정확도"""
        direction_scores: list[float] = []

        for c in report.cycles:
            for o in c.asset_management.get("final_orders", []):
                sym, action = o.get("symbol"), o.get("action")
                if not sym or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                # 매수 → 상승이 좋음 / 매도 → 하락이 좋음
                if action in ("BUY", "BUY_MORE"):
                    direction_scores.append(chg)
                elif action in ("SELL", "PARTIAL_SELL"):
                    direction_scores.append(-chg)

        if not direction_scores:
            return {}

        avg = sum(direction_scores) / len(direction_scores)
        score = 5
        if avg > 2:     score = 9
        elif avg > 0.5: score = 7
        elif avg >= 0:  score = 6
        elif avg > -1:  score = 4
        else:           score = 2

        return {
            "score": score,
            "order_count": len(direction_scores),
            "avg_direction_score": round(avg, 2),
        }

    def _eval_buy_executor(self, report: DailyReport, eod: dict) -> dict:
        """체결가 vs EOD 가격 (낮은 가격에 매수할수록 좋음)"""
        efficiencies: list[float] = []
        failed_count = 0

        for c in report.cycles:
            for r in c.buy_results:
                if r.get("status") in ("FILLED", "PARTIAL"):
                    sym, exec_price = r.get("symbol"), r.get("price", 0)
                    if not sym or not exec_price or sym not in eod:
                        continue
                    eod_price = eod[sym]["price"]
                    eff = (eod_price - exec_price) / exec_price * 100
                    efficiencies.append(eff)
                elif r.get("status") == "FAILED":
                    failed_count += 1

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        # 시도는 했으나 체결 없으면 최저점
        if not efficiencies:
            return {
                "score": 1,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
                "note": "주문 시도했으나 체결 없음",
            }

        avg = sum(efficiencies) / len(efficiencies)
        score = 5
        if avg > 1:      score = 9
        elif avg > 0:    score = 7
        elif avg > -0.5: score = 5
        else:            score = 3

        return {
            "score": score,
            "filled_count": len(efficiencies),
            "failed_count": failed_count,
            "avg_efficiency_pct": round(avg, 2),
        }

    def _eval_sell_executor(self, report: DailyReport, eod: dict) -> dict:
        """체결가 vs EOD 가격 (높은 가격에 매도할수록 좋음)"""
        efficiencies: list[float] = []
        failed_count = 0

        for c in report.cycles:
            for r in c.sell_results:
                if r.get("status") in ("FILLED", "PARTIAL"):
                    sym, exec_price = r.get("symbol"), r.get("price", 0)
                    if not sym or not exec_price or sym not in eod:
                        continue
                    eod_price = eod[sym]["price"]
                    eff = (exec_price - eod_price) / eod_price * 100
                    efficiencies.append(eff)
                elif r.get("status") == "FAILED":
                    failed_count += 1

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        # 시도는 했으나 체결 없으면 최저점
        if not efficiencies:
            return {
                "score": 1,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
                "note": "주문 시도했으나 체결 없음",
            }

        avg = sum(efficiencies) / len(efficiencies)
        score = 5
        if avg > 0.5:    score = 9
        elif avg > 0:    score = 7
        elif avg > -0.5: score = 5
        else:            score = 3

        return {
            "score": score,
            "filled_count": len(efficiencies),
            "failed_count": failed_count,
            "avg_efficiency_pct": round(avg, 2),
        }

    # ──────────────────────────────────────────
    # AI 피드백 생성
    # ──────────────────────────────────────────

    async def _ai_feedback(self, agent_name: str, metrics: dict) -> dict:
        """성과 지표 → 자연어 피드백 + 개선 포인트"""
        prompt = (
            f"다음은 오늘 '{agent_name}' 에이전트의 성과 데이터입니다.\n"
            f"{json.dumps(metrics, ensure_ascii=False)}\n\n"
            "이 데이터를 바탕으로 아래 JSON 형식으로만 응답하세요:\n"
            '{"summary": "오늘 성과 한 줄 요약 (수치 포함, 50자 이내)", '
            '"improvement": "다음 거래에서 개선할 구체적인 행동 지침 (40자 이내)"}'
        )
        try:
            if settings.ai_provider == AIProvider.OPENAI:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=settings.openai_api_key)
                resp = await client.chat.completions.create(
                    model=settings.resolved_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content or "{}")

            elif settings.ai_provider == AIProvider.ANTHROPIC:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                resp = await client.messages.create(
                    model=settings.resolved_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else "{}"
                m = re.search(r"\{.*?\}", text, re.DOTALL)
                return json.loads(m.group()) if m else {}

        except Exception as e:
            logger.warning(f"[평가] AI 피드백 생성 실패 ({agent_name}): {e}")

        return {
            "summary": f"점수 {metrics.get('score', 5)}/10 달성",
            "improvement": "추가 데이터 축적 후 분석 예정",
        }
