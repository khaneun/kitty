"""Night mode post-session agent performance evaluator

Evaluation flow:
  1. Collect each agent's decisions from today's NightDailyReport
  2. Fetch EOD prices via KIS overseas API
  3. Compute per-agent quantitative metrics + decision details
  4. Generate natural language feedback via AI
  5. Save to night-feedback/*.json (NightBaseAgent injects into system_prompt next cycle)
"""
import asyncio
import json
import re
from typing import Any

from kitty_night.config import AIProvider, night_settings
from kitty_night.feedback.store import append_entry
from kitty_night.report import NightDailyReport
from kitty_night.utils import logger


class NightPerformanceEvaluator:
    """Post-session agent performance evaluator for US market"""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    async def run(self, daily_report: NightDailyReport) -> dict[str, Any]:
        if not daily_report.cycles:
            logger.info("[Night:Eval] No cycles today — skipping evaluation")
            return {}

        logger.info("[Night:Eval] Post-session performance evaluation starting")

        symbols = self._collect_symbols(daily_report)
        if not symbols:
            logger.info("[Night:Eval] No symbols to analyze")
            return {}

        eod = await self._fetch_prices(symbols)
        logger.info(f"[Night:Eval] EOD prices collected: {len(eod)} symbols")

        results: dict[str, Any] = {}
        for agent_name, eval_fn in [
            ("NightSectorAnalyst", self._eval_sector_analyst),
            ("NightStockPicker", self._eval_stock_picker),
            ("NightStockEvaluator", self._eval_stock_evaluator),
            ("NightAssetManager", self._eval_asset_manager),
            ("NightBuyExecutor", self._eval_buy_executor),
            ("NightSellExecutor", self._eval_sell_executor),
        ]:
            try:
                metrics = eval_fn(daily_report, eod)
                if not metrics:
                    continue
                decision_details = metrics.pop("decision_details", "")
                feedback = await self._ai_feedback(agent_name, metrics, decision_details)
                entry = {
                    "date": daily_report.date,
                    "score": metrics["score"],
                    "summary": feedback.get("summary", f"Score {metrics['score']}/100"),
                    "improvement": feedback.get("improvement", ""),
                    "good_pattern": feedback.get("good_pattern", ""),
                    "metrics": metrics,
                }
                append_entry(agent_name, entry)
                results[agent_name] = entry
                logger.info(f"[Night:Eval] {agent_name}: {entry['score']}/100 — {entry['summary']}")
            except Exception as e:
                logger.error(f"[Night:Eval] {agent_name} evaluation error: {e}")

        return results

    # ── Data collection ───────────────────────────────────────────────────────

    def _collect_symbols(self, report: NightDailyReport) -> set[str]:
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
        sym_list = sorted(symbols)  # 순서 결정적으로 고정
        for i, sym in enumerate(sym_list):
            # 10개마다 2초 추가 대기 — 대량 조회 시 KIS 레이트리밋 방지
            if i > 0 and i % 10 == 0:
                logger.debug(f"[Night:Eval] 시세 batch pause ({i}/{len(sym_list)})")
                await asyncio.sleep(2.0)
            try:
                q = await self._broker.get_quote(sym)
                eod[sym] = {
                    "price": q.current_price,
                    "change_rate": q.change_rate,
                    "name": q.name,
                }
            except Exception as e:
                logger.warning(f"[Night:Eval] {sym} EOD fetch failed: {e}")
        return eod

    # ── Per-agent evaluations ─────────────────────────────────────────────────

    def _eval_sector_analyst(self, report: NightDailyReport, eod: dict) -> dict:
        hits, total = 0, 0
        detail_lines = []
        neutral_count = 0

        for c in report.cycles:
            for s in c.market_analysis.get("sectors", []):
                trend = s.get("trend")
                if trend == "neutral":
                    neutral_count += 1
                    continue
                candidates = [sym for sym in s.get("candidate_symbols", []) if sym in eod]
                if not candidates:
                    continue
                avg_chg = sum(eod[sym]["change_rate"] for sym in candidates) / len(candidates)
                correct = (trend == "bullish" and avg_chg > 0) or (trend == "bearish" and avg_chg < 0)
                hits += int(correct)
                total += 1
                mark = "O" if correct else "X"
                detail_lines.append(
                    f"  {s.get('name')} predicted:{trend} → actual {avg_chg:+.2f}% {mark}"
                )

        if total == 0:
            # 방향성 예측 없음(전 섹터 neutral 또는 EOD 가격 없음) — 기본 점수 부여
            sector_count = sum(
                len(c.market_analysis.get("sectors", [])) for c in report.cycles
            )
            if sector_count == 0:
                return {}
            neutral_lines = [
                f"  {s.get('name','?')} neutral"
                for c in report.cycles
                for s in c.market_analysis.get("sectors", [])
            ]
            return {
                "score": 50,
                "accuracy": None,
                "hits": 0,
                "total": 0,
                "neutral_sectors": neutral_count,
                "note": "All sectors neutral — no directional calls to evaluate",
                "decision_details": "\n".join(neutral_lines[:10]),
            }

        acc = hits / total
        return {
            "score": min(100, round(acc * 100 + 10)),
            "accuracy": round(acc, 2),
            "hits": hits, "total": total,
            "neutral_sectors": neutral_count,
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_stock_picker(self, report: NightDailyReport, eod: dict) -> dict:
        buy_changes: list[float] = []
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
                    mark = "O" if chg > 0 else "X"
                    detail_lines.append(f"  BUY {name}({sym}) → {chg:+.2f}% {mark} | {reason}")

        if not buy_changes:
            return {}

        avg_return = sum(buy_changes) / len(buy_changes)
        score = 50
        if avg_return > 2:     score = 90
        elif avg_return > 0.5: score = 70
        elif avg_return > 0:   score = 60
        elif avg_return > -1:  score = 40
        else:                  score = 20

        return {
            "score": score,
            "buy_count": len(buy_changes),
            "avg_buy_return": round(avg_return, 2),
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_stock_evaluator(self, report: NightDailyReport, eod: dict) -> dict:
        correct, total = 0, 0
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
                    (action == "PARTIAL_SELL"  and chg < 2)        or
                    (action == "SELL"          and chg < 0)
                )
                correct += int(hit)
                total += 1
                mark = "O" if hit else "X"
                detail_lines.append(f"  {action} {name}({sym}) → {chg:+.2f}% {mark} | {reason}")

        if total == 0:
            return {}

        acc = correct / total
        return {
            "score": round(acc * 100),
            "accuracy": round(acc, 2),
            "correct": correct, "total": total,
            "decision_details": "\n".join(detail_lines),
        }

    def _eval_asset_manager(self, report: NightDailyReport, eod: dict) -> dict:
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
                    mark = "O" if chg > 0 else "X"
                elif action in ("SELL", "PARTIAL_SELL"):
                    direction_scores.append(-chg)
                    mark = "O" if chg < 0 else "X"
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

    def _eval_buy_executor(self, report: NightDailyReport, eod: dict) -> dict:
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
                    mark = "O" if eff > 0 else "X"
                    detail_lines.append(
                        f"  {name}({sym}) buy@${exec_price:,.2f} → EOD ${eod_price:,.2f} ({eff:+.1f}%) {mark}"
                    )
                elif r.get("status") == "FAILED":
                    failed_count += 1
                    detail_lines.append(f"  {sym} buy FAILED: {r.get('reason', '')[:60]}")

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        if not efficiencies:
            return {
                "score": 10,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
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

    def _eval_sell_executor(self, report: NightDailyReport, eod: dict) -> dict:
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
                    mark = "O" if eff > 0 else "X"
                    detail_lines.append(
                        f"  {name}({sym}) sell@${exec_price:,.2f} → EOD ${eod_price:,.2f} ({eff:+.1f}%) {mark}"
                    )
                elif r.get("status") == "FAILED":
                    failed_count += 1
                    detail_lines.append(f"  {sym} sell FAILED: {r.get('reason', '')[:60]}")

        total_attempted = len(efficiencies) + failed_count
        if total_attempted == 0:
            return {}

        if not efficiencies:
            return {
                "score": 10,
                "filled_count": 0,
                "failed_count": failed_count,
                "avg_efficiency_pct": None,
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

    # ── AI feedback generation ────────────────────────────────────────────────

    async def _ai_feedback(
        self, agent_name: str, metrics: dict, decision_details: str = "",
    ) -> dict:
        from kitty_night.feedback.store import load_entries

        detail_section = ""
        if decision_details:
            detail_section = f"\n[Decision Details — O correct, X wrong]\n{decision_details}\n"

        # 과거 피드백 요약 제공 → AI가 반복되는 문제를 인식하고 누적 개선안 작성
        past_entries = load_entries(agent_name)
        past_section = ""
        if past_entries:
            past_improvements = [e.get("improvement", "") for e in past_entries[-5:] if e.get("improvement")]
            past_goods = [e.get("good_pattern", "") for e in past_entries[-5:] if e.get("good_pattern")]
            past_section = "\n[Past Feedback History — identify recurring patterns]\n"
            if past_improvements:
                past_section += "Recent improvements suggested:\n" + "\n".join(f"  - {p}" for p in past_improvements) + "\n"
            if past_goods:
                past_section += "Recent good patterns:\n" + "\n".join(f"  - {p}" for p in past_goods) + "\n"
            past_section += (
                "\nIMPORTANT: If today's issues overlap with past improvements, "
                "write a CUMULATIVE improvement that synthesizes the recurring theme. "
                "If a past good_pattern held true today, reinforce it with today's evidence.\n"
            )

        prompt = (
            f"Below is today's performance data for the '{agent_name}' agent.\n"
            f"Scores are on 0-100 scale.\n\n"
            f"[Performance Metrics]\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n"
            f"{detail_section}{past_section}\n"
            "Analyze the data and respond ONLY in this JSON format:\n"
            "{\n"
            '  "summary": "Today\'s performance: score + key result + what worked/failed (under 120 chars)",\n'
            '  "improvement": "Specific, actionable fix for the MOST IMPORTANT issue. '
            'If recurring from past sessions, say so and escalate urgency. (under 250 chars)",\n'
            '  "good_pattern": "Pattern that worked today AND should be repeated. '
            'If it confirms a past good pattern, note the consistency. (under 100 chars, empty string if none)"\n'
            "}"
        )
        try:
            if night_settings.ai_provider == AIProvider.OPENAI:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=night_settings.openai_api_key)
                resp = await client.chat.completions.create(
                    model=night_settings.resolved_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                return json.loads(resp.choices[0].message.content or "{}")

            elif night_settings.ai_provider == AIProvider.ANTHROPIC:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=night_settings.anthropic_api_key)
                resp = await client.messages.create(
                    model=night_settings.resolved_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else "{}"
                m = re.search(r"\{.*\}", text, re.DOTALL)
                return json.loads(m.group()) if m else {}

            elif night_settings.ai_provider == AIProvider.GEMINI:
                import google.generativeai as genai
                genai.configure(api_key=night_settings.gemini_api_key)
                model = genai.GenerativeModel(model_name=night_settings.resolved_model)
                resp = await model.generate_content_async(prompt)
                text = resp.text or "{}"
                m = re.search(r"\{.*\}", text, re.DOTALL)
                return json.loads(m.group()) if m else {}

        except Exception as e:
            logger.warning(f"[Night:Eval] AI feedback generation failed ({agent_name}): {e}")

        return {
            "summary": f"Score {metrics.get('score', 50)}/100",
            "improvement": "More data needed for detailed analysis",
            "good_pattern": "",
        }
