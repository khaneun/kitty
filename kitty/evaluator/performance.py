"""장 마감 후 에이전트 성과 평가 엔진

평가 흐름:
  1. 오늘 DailyReport에서 각 에이전트의 결정 수집
  2. KIS API로 EOD 가격/등락률 조회
  3. 에이전트별 정량 지표 + 구체적 판단 내역 기록
  4. AI로 자연어 피드백 (성과 요약 + 개선 지침 + 유지할 패턴)
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
                # decision_details는 AI에게 전달하되 최종 저장에서는 제외
                decision_details = metrics.pop("decision_details", "")
                feedback = await self._ai_feedback(agent_name, metrics, decision_details)
                entry = {
                    "date": daily_report.date,
                    "score": metrics["score"],
                    "summary": feedback.get("summary", f"점수 {metrics['score']}/100"),
                    "improvement": feedback.get("improvement", ""),
                    "good_pattern": feedback.get("good_pattern", ""),
                    "metrics": metrics,
                }
                append_entry(agent_name, entry)
                results[agent_name] = entry
                logger.info(f"[평가] {agent_name}: {entry['score']}/100 — {entry['summary']}")
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
        """섹터 방향 예측 적중률 + 구체적 섹터별 결과"""
        hits, total = 0, 0
        sector_details = []
        detail_lines = []

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
                mark = "✓" if correct else "✗"
                detail_lines.append(
                    f"  {s.get('name')} 예측:{trend} → 실제 {avg_chg:+.2f}% {mark}"
                )

        if total == 0:
            return {}

        acc = hits / total
        return {
            "score": min(100, round(acc * 100 + 10)),
            "accuracy": round(acc, 2),
            "hits": hits, "total": total,
            "sector_details": sector_details,
            "market_sentiment": report.cycles[-1].market_analysis.get("market_sentiment"),
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_stock_picker(self, report: DailyReport, eod: dict) -> dict:
        """추천 종목의 당일 수익률 + 개별 종목 결과"""
        buy_changes: list[float] = []
        sell_correct, sell_total = 0, 0
        detail_lines: list[str] = []

        for c in report.cycles:
            for d in c.stock_picks.get("decisions", []):
                sym, action = d.get("symbol"), d.get("action")
                if not sym or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                name = eod[sym].get("name", sym)
                reason = d.get("reason", "")[:80]
                if action == "BUY":
                    buy_changes.append(chg)
                    mark = "✓" if chg > 0 else "✗"
                    detail_lines.append(f"  BUY {name}({sym}) → {chg:+.2f}% {mark} | {reason}")
                elif action == "SELL":
                    sell_total += 1
                    hit = chg < 0
                    if hit:
                        sell_correct += 1
                    mark = "✓" if hit else "✗"
                    detail_lines.append(f"  SELL {name}({sym}) → {chg:+.2f}% {mark} | {reason}")

        if not buy_changes and sell_total == 0:
            return {}

        avg_return = sum(buy_changes) / len(buy_changes) if buy_changes else None
        sell_acc = sell_correct / sell_total if sell_total > 0 else None

        score = 50
        if avg_return is not None:
            if avg_return > 2:     score = 90
            elif avg_return > 0.5: score = 70
            elif avg_return > 0:   score = 60
            elif avg_return > -1:  score = 40
            else:                  score = 20

        return {
            "score": score,
            "buy_count": len(buy_changes),
            "avg_buy_return": round(avg_return, 2) if avg_return is not None else None,
            "sell_accuracy": round(sell_acc, 2) if sell_acc is not None else None,
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_stock_evaluator(self, report: DailyReport, eod: dict) -> dict:
        """보유 종목 HOLD/BUY_MORE/SELL 판단 정확도 + 개별 결과"""
        correct, total = 0, 0
        details = []
        detail_lines: list[str] = []

        for c in report.cycles:
            for e in c.stock_evaluation.get("evaluations", []):
                sym, action = e.get("symbol"), e.get("action")
                if not sym or not action or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                name = eod[sym].get("name", sym)
                reason = e.get("reason", "")[:80]
                hit = (
                    (action == "HOLD"         and -3 <= chg <= 5) or
                    (action == "BUY_MORE"     and chg > 0)        or
                    (action == "PARTIAL_SELL" and chg < 2)        or
                    (action == "SELL"         and chg < 0)
                )
                correct += int(hit)
                total += 1
                details.append({"symbol": sym, "action": action, "change": chg, "correct": hit})
                mark = "✓" if hit else "✗"
                detail_lines.append(f"  {action} {name}({sym}) → {chg:+.2f}% {mark} | {reason}")

        if total == 0:
            return {}

        acc = correct / total
        return {
            "score": round(acc * 100),
            "accuracy": round(acc, 2),
            "correct": correct, "total": total,
            "details": details,
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_asset_manager(self, report: DailyReport, eod: dict) -> dict:
        """최종 주문 결정의 방향성 정확도 + 개별 주문 결과"""
        direction_scores: list[float] = []
        detail_lines: list[str] = []

        for c in report.cycles:
            for o in c.asset_management.get("final_orders", []):
                sym, action = o.get("symbol"), o.get("action")
                if not sym or sym not in eod:
                    continue
                chg = eod[sym]["change_rate"]
                name = eod[sym].get("name", sym)
                reason = o.get("reason", "")[:80]
                if action in ("BUY", "BUY_MORE"):
                    direction_scores.append(chg)
                    mark = "✓" if chg > 0 else "✗"
                elif action in ("SELL", "PARTIAL_SELL"):
                    direction_scores.append(-chg)
                    mark = "✓" if chg < 0 else "✗"
                else:
                    continue
                detail_lines.append(f"  {action} {name}({sym}) → {chg:+.2f}% {mark} | {reason}")

        if not direction_scores:
            return {}

        avg = sum(direction_scores) / len(direction_scores)
        score = 50
        if avg > 2:     score = 90
        elif avg > 0.5: score = 70
        elif avg >= 0:  score = 60
        elif avg > -1:  score = 40
        else:           score = 20

        return {
            "score": score,
            "order_count": len(direction_scores),
            "avg_direction_score": round(avg, 2),
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_buy_executor(self, report: DailyReport, eod: dict) -> dict:
        """체결가 vs EOD 가격 + 개별 주문 효율"""
        efficiencies: list[float] = []
        failed_count = 0
        detail_lines: list[str] = []

        for c in report.cycles:
            for r in c.buy_results:
                sym = r.get("symbol", "")
                if r.get("status") in ("FILLED", "PARTIAL"):
                    exec_price = r.get("price", 0)
                    if not sym or not exec_price or sym not in eod:
                        continue
                    eod_price = eod[sym]["price"]
                    name = eod[sym].get("name", sym)
                    eff = (eod_price - exec_price) / exec_price * 100
                    efficiencies.append(eff)
                    mark = "✓" if eff > 0 else "✗"
                    detail_lines.append(
                        f"  {name}({sym}) 매수@{exec_price:,} → EOD {eod_price:,} ({eff:+.1f}%) {mark}"
                    )
                elif r.get("status") == "FAILED":
                    failed_count += 1
                    reason = r.get("reason", "")[:60]
                    detail_lines.append(f"  {sym} 매수 실패: {reason}")

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        if not efficiencies:
            return {
                "score": 10,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
                "note": "주문 시도했으나 체결 없음",
                "decision_details": "\n".join(detail_lines),
            }

        avg = sum(efficiencies) / len(efficiencies)
        score = 50
        if avg > 1:      score = 90
        elif avg > 0:    score = 70
        elif avg > -0.5: score = 50
        else:            score = 30

        return {
            "score": score,
            "filled_count": len(efficiencies),
            "failed_count": failed_count,
            "avg_efficiency_pct": round(avg, 2),
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_sell_executor(self, report: DailyReport, eod: dict) -> dict:
        """체결가 vs EOD 가격 + 개별 주문 효율"""
        efficiencies: list[float] = []
        failed_count = 0
        detail_lines: list[str] = []

        for c in report.cycles:
            for r in c.sell_results:
                sym = r.get("symbol", "")
                if r.get("status") in ("FILLED", "PARTIAL"):
                    exec_price = r.get("price", 0)
                    if not sym or not exec_price or sym not in eod:
                        continue
                    eod_price = eod[sym]["price"]
                    name = eod[sym].get("name", sym)
                    eff = (exec_price - eod_price) / eod_price * 100
                    efficiencies.append(eff)
                    mark = "✓" if eff > 0 else "✗"
                    detail_lines.append(
                        f"  {name}({sym}) 매도@{exec_price:,} → EOD {eod_price:,} ({eff:+.1f}%) {mark}"
                    )
                elif r.get("status") == "FAILED":
                    failed_count += 1
                    reason = r.get("reason", "")[:60]
                    detail_lines.append(f"  {sym} 매도 실패: {reason}")

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        if not efficiencies:
            return {
                "score": 10,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
                "note": "주문 시도했으나 체결 없음",
                "decision_details": "\n".join(detail_lines),
            }

        avg = sum(efficiencies) / len(efficiencies)
        score = 50
        if avg > 0.5:    score = 90
        elif avg > 0:    score = 70
        elif avg > -0.5: score = 50
        else:            score = 30

        return {
            "score": score,
            "filled_count": len(efficiencies),
            "failed_count": failed_count,
            "avg_efficiency_pct": round(avg, 2),
            "decision_details": "\n".join(detail_lines),
        }

    # ──────────────────────────────────────────
    # AI 피드백 생성
    # ──────────────────────────────────────────

    async def _ai_feedback(
        self, agent_name: str, metrics: dict, decision_details: str = "",
    ) -> dict:
        """성과 지표 + 구체적 판단 내역 → 자연어 피드백"""
        detail_section = ""
        if decision_details:
            detail_section = f"\n[구체적 판단 내역 — ✓ 정확, ✗ 오판]\n{decision_details}\n"

        prompt = (
            f"다음은 오늘 '{agent_name}' 에이전트의 성과 데이터입니다.\n"
            f"점수는 0~100점 척도입니다.\n\n"
            f"[성과 지표]\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n"
            f"{detail_section}\n"
            "위 데이터를 분석하여 아래 JSON 형식으로만 응답하세요:\n"
            "{\n"
            '  "summary": "오늘 성과 요약 — 점수·핵심 수치·잘한 점·못한 점 포함 (100자 이내)",\n'
            '  "improvement": "내일 개선할 구체적 행동 지침 — 무엇을 어떻게 바꿀지, 오늘 오판의 원인과 대안을 명시 (200자 이내)",\n'
            '  "good_pattern": "오늘 잘한 판단이 있다면 내일도 유지할 패턴 (80자 이내, 없으면 빈 문자열)"\n'
            "}"
        )
        try:
            if settings.ai_provider == AIProvider.OPENAI:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=settings.openai_api_key)
                resp = await client.chat.completions.create(
                    model=settings.resolved_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content or "{}")

            elif settings.ai_provider == AIProvider.ANTHROPIC:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                resp = await client.messages.create(
                    model=settings.resolved_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else "{}"
                m = re.search(r"\{.*\}", text, re.DOTALL)
                return json.loads(m.group()) if m else {}

            elif settings.ai_provider == AIProvider.GEMINI:
                import google.generativeai as genai
                genai.configure(api_key=settings.gemini_api_key)
                model = genai.GenerativeModel(model_name=settings.resolved_model)
                resp = await model.generate_content_async(prompt)
                text = resp.text or "{}"
                m = re.search(r"\{.*\}", text, re.DOTALL)
                return json.loads(m.group()) if m else {}

        except Exception as e:
            logger.warning(f"[평가] AI 피드백 생성 실패 ({agent_name}): {e}")

        return {
            "summary": f"점수 {metrics.get('score', 50)}/100 달성",
            "improvement": "추가 데이터 축적 후 분석 예정",
            "good_pattern": "",
        }
