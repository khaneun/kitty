"""에이전트 성과 평가 엔진 — 매 사이클 종료 후 실행

평가 흐름:
  1. 오늘 DailyReport에서 각 에이전트의 결정 수집
  2. KIS API로 현재가/등락률 조회
  3. 에이전트별 정량 지표 + 구체적 판단 내역 기록
  4. AI로 자연어 피드백 (성과 요약 + 개선 지침 + 유지할 패턴 + 반성문)
  5. feedback/*.json 에 저장 (BaseAgent가 다음 사이클부터 system_prompt에 주입)
"""
import json
import re
from typing import Any

from kitty.config import AIProvider, settings
from kitty.feedback.store import append_entry
from kitty.report import DailyReport
from kitty.utils import logger

# 손실 패널티 기여 비율 (합계 = 1.0)
_LOSS_PENALTY_WEIGHTS: dict[str, float] = {
    "섹터분석가": 0.10,
    "종목발굴가": 0.15,
    "종목평가가": 0.38,
    "자산운용가": 0.37,
}


class PerformanceEvaluator:
    """매 사이클 종료 후 에이전트별 성과 평가"""

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

        logger.info("[평가] 사이클 성과 평가 시작")

        symbols = self._collect_symbols(daily_report)
        if not symbols:
            logger.info("[평가] 분석 종목 없음")
            return {}

        eod = await self._fetch_prices(symbols)
        logger.info(f"[평가] 현재가 수집: {len(eod)}개 종목")

        # 재매수 패턴 감지 (손절 후 당일 동일 종목 재매수)
        rebuy_symbols = self._detect_rebuy_patterns(daily_report)
        if rebuy_symbols:
            logger.warning(f"[평가] ⚠️ 재매수 패턴 감지: {rebuy_symbols}")

        # 실현 손실 패널티 계산
        loss_penalty = self._calc_loss_penalty(daily_report, eod)
        if loss_penalty > 0:
            logger.warning(f"[평가] 손실 패널티 적용: {loss_penalty:.1f}점")

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

                # 손실 패널티 적용 (해당 에이전트에만)
                if loss_penalty > 0 and agent_name in _LOSS_PENALTY_WEIGHTS:
                    weight = _LOSS_PENALTY_WEIGHTS[agent_name]
                    penalty_pts = round(loss_penalty * weight)
                    metrics["score"] = max(10, metrics["score"] - penalty_pts)
                    metrics["loss_penalty_applied"] = penalty_pts

                # 재매수 패턴 패널티 (종목평가가, 자산운용가에 추가 감점)
                if rebuy_symbols and agent_name in ("종목평가가", "자산운용가"):
                    rebuy_penalty = min(30, len(rebuy_symbols) * 15)
                    metrics["score"] = max(10, metrics["score"] - rebuy_penalty)
                    metrics["rebuy_symbols"] = rebuy_symbols
                    metrics["rebuy_penalty_applied"] = rebuy_penalty

                feedback = await self._ai_feedback(
                    agent_name, metrics, decision_details,
                    rebuy_symbols=rebuy_symbols,
                )
                entry = {
                    "date": daily_report.date,
                    "score": metrics["score"],
                    "summary": feedback.get("summary", f"점수 {metrics['score']}/100"),
                    "improvement": feedback.get("improvement", ""),
                    "good_pattern": feedback.get("good_pattern", ""),
                    "reflection": feedback.get("reflection", ""),
                    "metrics": metrics,
                }
                append_entry(agent_name, entry)
                results[agent_name] = entry
                logger.info(f"[평가] {agent_name}: {entry['score']}/100 — {entry['summary']}")
            except Exception as e:
                logger.error(f"[평가] {agent_name} 평가 오류: {e}")

        return results

    # ──────────────────────────────────────────
    # 재매수 패턴 감지
    # ──────────────────────────────────────────

    def _detect_rebuy_patterns(self, report: DailyReport) -> list[str]:
        """당일 매도(손절 포함) 후 동일 종목 재매수 패턴 감지"""
        sold_symbols: set[str] = set()
        rebought: list[str] = []
        for c in report.cycles:
            for r in c.sell_results:
                if r.get("status") in ("FILLED", "PARTIAL"):
                    sym = r.get("symbol", "")
                    if sym:
                        sold_symbols.add(sym)
            for r in c.buy_results:
                if r.get("status") in ("FILLED", "PARTIAL"):
                    sym = r.get("symbol", "")
                    if sym and sym in sold_symbols and sym not in rebought:
                        rebought.append(sym)
        return rebought

    def _calc_loss_penalty(self, report: DailyReport, eod: dict) -> float:
        """실제 체결된 매수 종목의 EOD 손실 기반 패널티 계산 (0~40점)"""
        losses: list[float] = []
        for c in report.cycles:
            for r in c.buy_results:
                if r.get("status") in ("FILLED", "PARTIAL"):
                    sym = r.get("symbol", "")
                    exec_price = r.get("price", 0)
                    if sym and exec_price and sym in eod:
                        chg = (eod[sym]["price"] - exec_price) / exec_price * 100
                        if chg < 0:
                            losses.append(chg)
        if not losses:
            return 0.0
        avg_loss = sum(losses) / len(losses)
        # -5% 손실 → 20점 패널티, -10% → 40점 (상한)
        return min(40.0, abs(avg_loss) * 4)

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

    async def _fetch_prices(self, symbols: set[str]) -> dict[str, dict]:
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

        # 연속적 점수 산출 (기존 이산적 5단계 → 선형 보간)
        score = 50
        if avg_return is not None:
            # -3% 이하 → 10점, 0% → 50점, +3% 이상 → 95점 (선형)
            score = max(10, min(95, round(50 + avg_return * 15)))
        # 매도 정확도 보정 (±10점)
        if sell_acc is not None:
            score = max(10, min(95, score + round((sell_acc - 0.5) * 20)))

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
        # 연속적 점수: -3% → 10점, 0% → 50점, +3% → 95점
        score = max(10, min(95, round(50 + avg * 15)))

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
        # 연속적 점수: -1% → 30점, 0% → 60점, +1% → 90점
        score = max(10, min(95, round(60 + avg * 30)))
        # 실패 건수 감점 (-5점/건)
        score = max(10, score - failed_count * 5)

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
        # 연속적 점수: -1% → 30점, 0% → 60점, +1% → 90점
        score = max(10, min(95, round(60 + avg * 30)))
        score = max(10, score - failed_count * 5)

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
        rebuy_symbols: list[str] | None = None,
    ) -> dict:
        """성과 지표 + 구체적 판단 내역 + 과거 피드백 컨텍스트 → 누적 자연어 피드백"""
        from kitty.feedback.store import load_entries

        score = metrics.get("score", 50)
        is_low_score = score <= 60

        detail_section = ""
        if decision_details:
            detail_section = f"\n[구체적 판단 내역 — ✓ 정확, ✗ 오판]\n{decision_details}\n"

        rebuy_section = ""
        if rebuy_symbols:
            rebuy_section = (
                f"\n[⚠️ 심각한 재매수 패턴 감지]\n"
                f"다음 종목을 당일 매도 후 재매수했습니다: {', '.join(rebuy_symbols)}\n"
                f"이는 손절 후 같은 종목을 비싸게 재매수하는 최악의 반복 실패 패턴입니다.\n"
            )

        # 과거 피드백 요약 제공 → AI가 반복되는 문제를 인식하고 누적 개선안 작성
        past_entries = load_entries(agent_name)
        past_section = ""
        if past_entries:
            past_improvements = [e.get("improvement", "") for e in past_entries[-5:] if e.get("improvement")]
            past_goods = [e.get("good_pattern", "") for e in past_entries[-5:] if e.get("good_pattern")]
            past_reflections = [e.get("reflection", "") for e in past_entries[-3:] if e.get("reflection")]
            past_section = "\n[과거 피드백 이력 — 반복 패턴을 파악하세요]\n"
            if past_improvements:
                past_section += "최근 개선 제안:\n" + "\n".join(f"  - {p}" for p in past_improvements) + "\n"
            if past_reflections:
                past_section += "최근 반성문 요약:\n" + "\n".join(f"  - {r}" for r in past_reflections) + "\n"
            if past_goods:
                past_section += "최근 좋은 패턴:\n" + "\n".join(f"  - {p}" for p in past_goods) + "\n"
            past_section += (
                "\n중요: 오늘 이슈가 과거 개선 제안과 겹치면, "
                "반복 테마를 종합한 누적 개선안을 작성하세요. "
                "과거 좋은 패턴이 오늘도 유지됐다면, 오늘의 근거와 함께 강화하세요.\n"
            )

        # 반성문 지시: 60점 이하면 강도 높임
        reflection_instruction = (
            '"reflection": "에이전트 자신의 반성문. 구체적인 실패 사례를 인정하고 다음에 절대 반복하지 않을 것을 다짐하는 문장. (150자 이내)"'
            if not is_low_score else
            '"reflection": "⚠️ 심각한 실패에 대한 강력한 반성문. 무엇을 잘못했는지, 왜 손실이 났는지, 어떤 판단이 틀렸는지 구체적으로 자기비판하고, 다시는 이 패턴을 반복하지 않겠다는 강한 다짐. 재매수 패턴이 있으면 반드시 언급. (250자 이내)"'
        )

        prompt = (
            f"다음은 '{agent_name}' 에이전트의 최근 사이클 성과 데이터입니다.\n"
            f"점수는 0~100점 척도입니다. {'⚠️ 이 점수는 심각하게 낮습니다. 엄격하게 평가하세요.' if is_low_score else ''}\n\n"
            f"[성과 지표]\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n"
            f"{detail_section}{rebuy_section}{past_section}\n"
            "위 데이터를 분석하여 아래 JSON 형식으로만 응답하세요:\n"
            "{\n"
            '  "summary": "오늘 성과: 점수 + 핵심 결과 + 잘한 점/못한 점 (120자 이내)",\n'
            '  "improvement": "가장 중요한 이슈의 구체적 개선안. '
            '과거와 반복되는 이슈면 반복임을 명시하고 긴급도를 높이세요. (250자 이내)",\n'
            '  "good_pattern": "오늘 효과가 입증된 패턴. '
            '과거 좋은 패턴과 일치하면 일관성을 언급하세요. (100자 이내, 없으면 빈 문자열)",\n'
            f'  {reflection_instruction}\n'
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

        score = metrics.get("score", 50)
        default_reflection = (
            f"점수 {score}/100. AI 피드백 생성 실패."
            if score > 60 else
            f"⚠️ 점수 {score}/100 — 심각한 저성과. 판단 로직 전면 재검토 필요."
        )
        return {
            "summary": f"점수 {score}/100 달성",
            "improvement": "추가 데이터 축적 후 분석 예정",
            "good_pattern": "",
            "reflection": default_reflection,
        }
