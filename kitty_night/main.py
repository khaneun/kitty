"""Kitty Night Mode — US stock automated trading after KR market hours"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kitty_night.agents import (
    NightAssetManagerAgent,
    NightBuyExecutorAgent,
    NightSectorAnalystAgent,
    NightSellExecutorAgent,
    NightStockEvaluatorAgent,
    NightStockPickerAgent,
    NightTendencyAgent,
)
from kitty_night.broker.kis_overseas import KISOverseasBroker
from kitty_night.config import night_settings
from kitty_night.evaluator import NightPerformanceEvaluator
from kitty_night.report import NightDailyReport
from kitty_night.telegram import NightTelegramReporter
from kitty_night.tools.market_calendar import (
    MarketPhase,
    get_market_phase,
    next_market_open_kst,
    now_kst,
    seconds_until,
)
from kitty_night.utils import logger, setup_night_logger
from kitty_night.utils.portfolio import save_portfolio_snapshot

setup_night_logger()

_KST = ZoneInfo("Asia/Seoul")
_AGENT_CONTEXT_PATH = Path("night-logs/night_agent_context.json")
_NIGHT_FORCE_SELL_DIR = Path("night-commands")
_NIGHT_MODE_REQ = Path("night-commands/night_mode_request.json")

# US market barometer symbols (major ETFs + mega caps)
_BAROMETER_SYMBOLS = [
    "SPY",   # S&P 500 ETF
    "QQQ",   # Nasdaq 100 ETF
    "AAPL",  # Apple
    "MSFT",  # Microsoft
    "NVDA",  # NVIDIA
    "GOOGL", # Alphabet
    "AMZN",  # Amazon
    "META",  # Meta
    "TSLA",  # Tesla
    "JPM",   # JPMorgan
]


def _save_agent_context(agent_name: str, output: dict) -> None:
    try:
        ctx: dict = {}
        if _AGENT_CONTEXT_PATH.exists():
            try:
                ctx = json.loads(_AGENT_CONTEXT_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        ctx[agent_name] = {
            "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
            "output": output,
        }
        _AGENT_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AGENT_CONTEXT_PATH.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug(f"Agent context save failed: {e}")


async def _night_force_sell_handler(broker: KISOverseasBroker) -> None:
    """night-commands/night_force_sell_{symbol}.json 청산 요청 처리 (2초 폴링)"""
    while True:
        await asyncio.sleep(2)
        try:
            _NIGHT_FORCE_SELL_DIR.mkdir(parents=True, exist_ok=True)
            for req_file in sorted(_NIGHT_FORCE_SELL_DIR.glob("night_force_sell_*.json")):
                try:
                    req = json.loads(req_file.read_text(encoding="utf-8"))
                    symbol = req.get("symbol", "")
                    qty = int(req.get("qty", 0))
                    excd = req.get("excd", "NAS")
                    req_file.unlink(missing_ok=True)
                    if not symbol or qty <= 0:
                        logger.warning(f"[Night:청산요청] 잘못된 요청: {req}")
                        continue
                    logger.info(f"[Night:청산요청] {symbol}({excd}) {qty}주 즉시 청산 시작")
                    try:
                        order = await broker.sell(symbol, excd, qty, 0)
                        logger.info(f"[Night:청산요청] {symbol} 청산 완료: {order}")
                    except Exception as e:
                        logger.error(f"[Night:청산요청] {symbol} 청산 실패: {e}")
                except Exception as e:
                    logger.warning(f"[Night:청산요청] 파일 처리 실패 {req_file.name}: {e}")
                    req_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Night:청산요청] 핸들러 오류: {e}")


async def _collect_market_data(broker: KISOverseasBroker) -> dict:
    """Collect US market barometer quotes + volume leaders"""
    barometers: list[dict] = []
    for sym in _BAROMETER_SYMBOLS:
        try:
            q = await broker.get_quote(sym)
            barometers.append(q.model_dump())
        except Exception as e:
            logger.debug(f"Barometer {sym} fetch failed: {e}")

    volume_leaders: list[dict] = []
    try:
        volume_leaders = await broker.get_volume_rank()
    except Exception as e:
        logger.warning(f"Volume rank fetch failed (ignored): {e}")

    return {"barometers": barometers, "volume_leaders": volume_leaders}


async def run_trading_cycle(
    broker: KISOverseasBroker,
    sector_analyst: NightSectorAnalystAgent,
    stock_evaluator: NightStockEvaluatorAgent,
    stock_picker: NightStockPickerAgent,
    asset_manager: NightAssetManagerAgent,
    buy_executor: NightBuyExecutorAgent,
    sell_executor: NightSellExecutorAgent,
    tendency_agent: NightTendencyAgent,
    reporter: NightTelegramReporter,
    daily_report: NightDailyReport,
) -> None:
    """Run one trading cycle"""
    logger.info("=== Night Trading Cycle Start ===")
    daily_report.begin_cycle(mode=night_settings.trading_mode.value)

    # 0. Tendency directive (no AI call)
    tendency_directive = tendency_agent.get_directive()
    logger.info(f"[Night:Tendency] {tendency_agent.profile['label']}")
    _save_agent_context("NightTendency", tendency_agent.profile)

    # 1. Balance + available cash
    balance_data = await broker.get_balance()
    holdings = balance_data.get("holdings", [])
    available_usd = await broker.get_available_usd()

    total_eval_usd = sum(
        float(h.get("eval_amount", 0)) for h in holdings
    ) + available_usd

    logger.info(
        f"Holdings: {len(holdings)} | Cash: ${available_usd:,.2f} | "
        f"Total: ${total_eval_usd:,.2f}"
    )

    # Portfolio snapshot
    total_pnl = sum(float(h.get("pnl_amount", 0)) for h in holdings)
    save_portfolio_snapshot(
        trading_mode=night_settings.trading_mode.value,
        available_usd=available_usd,
        total_eval_usd=total_eval_usd,
        total_pnl_usd=total_pnl,
        holdings=holdings,
    )

    # 1.5 Market data
    market_data = await _collect_market_data(broker)
    logger.info(
        f"Market data: barometers {len(market_data['barometers'])} | "
        f"volume leaders {len(market_data['volume_leaders'])}"
    )

    # 2. Sector analysis
    current_date = datetime.now(_KST).strftime("%Y-%m-%d")

    # Normalize holdings for agents (consistent format)
    portfolio_for_agents = [
        {
            "symbol": h.get("symbol", ""),
            "name": h.get("name", ""),
            "quantity": int(h.get("quantity", 0)),
            "avg_price": float(h.get("avg_price", 0)),
            "current_price": float(h.get("current_price", 0)),
            "eval_amount": float(h.get("eval_amount", 0)),
            "pnl_amount": float(h.get("pnl_amount", 0)),
            "pnl_rate": float(h.get("pnl_rate", 0)),
        }
        for h in holdings
    ]

    analysis = await sector_analyst.run({
        "portfolio": portfolio_for_agents,
        "current_date": current_date,
        "market_data": market_data,
    })
    daily_report.record_analysis(analysis)
    _save_agent_context("NightSectorAnalyst", analysis)

    portfolio_meta = {
        "holdings_count": len(holdings),
        "target_min_holdings": 3,
    }

    # 3. Fetch quotes for candidates + holdings + volume leaders
    candidate_symbols: set[str] = set()
    for sector in analysis.get("sectors", []):
        for symbol in sector.get("candidate_symbols", []):
            candidate_symbols.add(symbol)
    for h in holdings:
        sym = h.get("symbol", "")
        if sym:
            candidate_symbols.add(sym)
    for vl in market_data.get("volume_leaders", []):
        sym = vl.get("symbol", "")
        if sym:
            candidate_symbols.add(sym)
    # 바로미터 개별주 항상 포함 — 섹터분석가가 candidates:0 반환해도 파이프라인 데이터 확보
    _ETF_SYMBOLS = {"SPY", "QQQ"}
    for bm in market_data.get("barometers", []):
        sym = bm.get("symbol", "")
        if sym and sym not in _ETF_SYMBOLS:
            candidate_symbols.add(sym)

    logger.info(f"Quote targets: {len(candidate_symbols)} symbols")
    quotes: list[dict] = []
    for symbol in sorted(candidate_symbols):
        try:
            q = await broker.get_quote(symbol)
            quotes.append(q.model_dump())
        except Exception as e:
            logger.error(f"Quote fetch failed {symbol}: {e}")

    # 4. Stock evaluation (holdings)
    stock_evaluation = await stock_evaluator.run({
        "portfolio": portfolio_for_agents,
        "quotes": quotes,
        "sector_analysis": analysis,
        "max_buy_amount_usd": night_settings.max_buy_amount_usd,
        "tendency_directive": tendency_directive,
        "portfolio_meta": portfolio_meta,
    })
    daily_report.record_stock_evaluation(stock_evaluation)
    _save_agent_context("NightStockEvaluator", stock_evaluation)

    # 5. Stock picking (new candidates)
    new_candidates = await stock_picker.run({
        "analysis": analysis,
        "quotes": quotes,
        "portfolio": portfolio_for_agents,
        "available_cash_usd": available_usd,
        "max_buy_amount_usd": night_settings.max_buy_amount_usd,
        "tendency_directive": tendency_directive,
        "volume_leaders": market_data.get("volume_leaders", []),
        "portfolio_meta": portfolio_meta,
    })
    daily_report.record_stock_picks(new_candidates)
    _save_agent_context("NightStockPicker", new_candidates)

    # 6. Asset management (final orders)
    asset_plan = await asset_manager.run({
        "stock_evaluation": stock_evaluation,
        "new_candidates": new_candidates,
        "quotes": quotes,
        "portfolio": portfolio_for_agents,
        "available_cash_usd": available_usd,
        "total_asset_value_usd": total_eval_usd,
        "max_buy_amount_usd": night_settings.max_buy_amount_usd,
        "max_position_size_usd": night_settings.max_position_size_usd,
        "tendency_directive": tendency_directive,
        "portfolio_meta": portfolio_meta,
    })
    daily_report.record_asset_management(asset_plan)
    _save_agent_context("NightAssetManager", asset_plan)

    final_orders = asset_plan.get("final_orders", [])
    if not final_orders:
        logger.info("No orders this cycle")
        daily_report.end_cycle()
        return

    # 7. Market hours check
    phase = get_market_phase()
    if phase != MarketPhase.MARKET:
        logger.info(f"Not in market hours (phase={phase.value}) — {len(final_orders)} orders skipped (analysis only)")
        daily_report.end_cycle()
        return

    # 8. Execute buys and sells (sells first for cash)
    sell_result = await sell_executor.run({
        "final_orders": final_orders,
        "portfolio": portfolio_for_agents,
        "quotes": quotes,
    })
    buy_result = await buy_executor.run({
        "final_orders": final_orders,
        "quotes": quotes,
        "available_cash_usd": available_usd,
    })

    buy_results = buy_result.get("buy_results", [])
    sell_results = sell_result.get("sell_results", [])
    daily_report.record_executions(buy_results, sell_results)
    _save_agent_context("NightBuyExecutor", {"buy_results": buy_results})
    _save_agent_context("NightSellExecutor", {"sell_results": sell_results})

    # 9. 매매 실행 후 포트폴리오 스냅샷 갱신
    any_executed = any(
        r.get("status") not in ("SKIPPED", "FAILED")
        for r in buy_results + sell_results
    )
    if any_executed:
        try:
            post_balance = await broker.get_balance()
            post_holdings = post_balance.get("holdings", [])
            post_available = await broker.get_available_usd()
            post_total_eval = sum(
                float(h.get("eval_amount", 0)) for h in post_holdings
            ) + post_available
            post_total_pnl = sum(float(h.get("pnl_amount", 0)) for h in post_holdings)
            save_portfolio_snapshot(
                trading_mode=night_settings.trading_mode.value,
                available_usd=post_available,
                total_eval_usd=post_total_eval,
                total_pnl_usd=post_total_pnl,
                holdings=post_holdings,
            )
            logger.info(
                f"[Night] Post-trade snapshot updated: "
                f"{len(post_holdings)} holdings, cash ${post_available:,.2f}"
            )
        except Exception as e:
            logger.warning(f"[Night] Post-trade snapshot update failed: {e}")

    # Telegram trade reports (market orders fall back to quote reference price)
    night_quote_map = {q["symbol"]: q for q in quotes}
    for r in sell_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            price = r.get("price") or float(night_quote_map.get(r["symbol"], {}).get("current_price", 0))
            await reporter.report_trade("SELL", r["symbol"], r["quantity"], price, "Night strategy", name=r.get("name", ""))

    for r in buy_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            price = r.get("price") or float(night_quote_map.get(r["symbol"], {}).get("current_price", 0))
            await reporter.report_trade("BUY", r["symbol"], r["quantity"], price, "Night strategy", name=r.get("name", ""))

    daily_report.end_cycle()
    logger.info("=== Night Trading Cycle Complete ===")


def _format_eval_summary(results: dict) -> str:
    lines = ["📊 *Night Agent Performance*\n"]
    score_emoji = {range(0, 40): "🔴", range(40, 70): "🟡", range(70, 101): "🟢"}
    for agent_name, entry in results.items():
        score = entry.get("score", 50)
        emoji = next((e for r, e in score_emoji.items() if score in r), "⚪")
        lines.append(f"{emoji} *{agent_name}* `{score}/100`")
        lines.append(f"   {entry.get('summary', '')}")
        if entry.get("good_pattern"):
            lines.append(f"   ✅ _{entry['good_pattern']}_")
        if entry.get("improvement"):
            lines.append(f"   💡 _{entry['improvement']}_")
    lines.append("\n_Feedback applied to all agents._")
    return "\n".join(lines)


def _format_tendency_update(profile: dict) -> str:
    from kitty_night.agents.tendency import DIMS, DIM_LABELS, LEVEL_LABEL, LEVEL_VALUES

    label = profile.get("label", "-")
    levels = profile.get("levels", {})
    rationale = profile.get("rationale", "")

    lines = [f"📐 *Night Trading Strategy Updated*\n"]
    lines.append(f"Profile: *{label}*\n")

    dim_display = {
        "take_profit": ("Take Profit", lambda v: f"+{v:.1f}%"),
        "stop_loss":   ("Stop Loss",   lambda v: f"{v:.1f}%"),
        "cash":        ("Cash",        lambda v: f"{int(v*100)}%"),
        "max_weight":  ("Max Weight",  lambda v: f"max {v:.0f}%"),
        "entry":       ("Entry",       lambda v: f"±{v:.1f}%"),
    }
    for dim in DIMS:
        lv = levels.get(dim, "-")
        name, fmt = dim_display[dim]
        val = fmt(LEVEL_VALUES[dim][lv]) if isinstance(lv, int) else "-"
        lv_label = LEVEL_LABEL.get(lv, "-") if isinstance(lv, int) else "-"
        lines.append(f"  {name}: `L{lv} {lv_label}` → {val}")

    if rationale:
        lines.append(f"\n💭 _{rationale}_")
    return "\n".join(lines)


async def main() -> None:
    # 시작 시 저장된 모드 설정 복원 (컨테이너 재시작 후에도 live 유지)
    _NIGHT_MODE_CFG = Path("night-commands/night_mode_config.json")
    if _NIGHT_MODE_CFG.exists():
        try:
            cfg = json.loads(_NIGHT_MODE_CFG.read_text(encoding="utf-8"))
            saved_mode = cfg.get("mode", "")
            if saved_mode in ("paper", "live"):
                from kitty_night.config import TradingMode
                night_settings.trading_mode = TradingMode(saved_mode)
                logger.info(f"[Night] 저장된 모드 복원: {saved_mode}")
        except Exception as e:
            logger.warning(f"[Night] 모드 설정 파일 읽기 실패: {e}")

    logger.info(f"🌙 Kitty Night Mode starting — mode: {night_settings.trading_mode.value}")

    broker = KISOverseasBroker()
    sector_analyst = NightSectorAnalystAgent()
    stock_evaluator = NightStockEvaluatorAgent()
    stock_picker = NightStockPickerAgent()
    asset_manager = NightAssetManagerAgent()
    buy_executor = NightBuyExecutorAgent(broker)
    sell_executor = NightSellExecutorAgent(broker)
    tendency_agent = NightTendencyAgent(profile_name="aggressive")

    reporter = NightTelegramReporter().build()
    daily_report = NightDailyReport()
    evaluator = NightPerformanceEvaluator(broker)

    # Night 청산 요청 핸들러 백그라운드 태스크
    asyncio.create_task(_night_force_sell_handler(broker))

    # 실전 모드 기동 시 경고 카운트다운 — 미국장 활성 시간대일 때만 발송
    if night_settings.trading_mode.value == "live":
        _startup_phase = get_market_phase()
        if _startup_phase != MarketPhase.CLOSED:
            logger.warning("⚠️  LIVE MODE — 실제 자금으로 거래됩니다. 30초 후 시작...")
            await reporter.send(
                "⚠️ *LIVE MODE 기동!*\n"
                "실제 자금으로 US 주식 자동매매를 시작합니다.\n"
                "30초 안에 취소하려면 컨테이너를 중지하세요."
            )
            await asyncio.sleep(30)
            logger.warning("⚠️  LIVE MODE 시작!")
        else:
            logger.info("🌙 LIVE MODE — 한국장 시간(CLOSED), 미국장 대기 중")

    # 기동 알람 — CLOSED(한국 장중) 시간이면 발송 생략
    if get_market_phase() != MarketPhase.CLOSED:
        await reporter.send(
            f"🌙 *Kitty Night Mode Started!*\n"
            f"Mode: `{night_settings.trading_mode.value}`\n"
            f"Market: US (NYSE/NASDAQ)\n"
            f"Cycle interval: {night_settings.cycle_seconds}s"
        )
    else:
        logger.info("🌙 Night Mode 기동 — CLOSED 페이즈, 텔레그램 알람 생략")

    last_report_date = daily_report.date
    last_eval_done = False
    cycle_interval = night_settings.cycle_seconds
    _last_closed_snapshot = 0.0  # CLOSED 페이즈 스냅샷 마지막 갱신 시각

    try:
        while True:
            # 모니터 대시보드 모드 전환 요청 확인
            if _NIGHT_MODE_REQ.exists():
                try:
                    req = json.loads(_NIGHT_MODE_REQ.read_text(encoding="utf-8"))
                    new_mode = req.get("mode", "")
                    if new_mode in ("paper", "live"):
                        from kitty_night.config import TradingMode
                        night_settings.trading_mode = TradingMode(new_mode)
                        broker.reset_token()
                        logger.info(f"[Night:모드전환] {new_mode}")
                        await reporter.send(f"🔄 Night 모드 전환: `{new_mode}` (모니터 대시보드)")
                        await run_trading_cycle(  # 즉시 사이클 실행 → 포트폴리오 현행화
                            broker, sector_analyst, stock_evaluator, stock_picker,
                            asset_manager, buy_executor, sell_executor,
                            tendency_agent, reporter, daily_report,
                        )
                except Exception as e:
                    logger.warning(f"[Night:모드전환] 요청 처리 실패: {e}")
                finally:
                    _NIGHT_MODE_REQ.unlink(missing_ok=True)

            phase = get_market_phase()

            # Date rollover → send daily report and reset
            today = now_kst().strftime("%Y-%m-%d")
            if today != last_report_date:
                await reporter.send(daily_report.telegram_summary())
                daily_report = NightDailyReport()
                last_report_date = today
                last_eval_done = False

            if phase == MarketPhase.CLOSED:
                logger.info(f"[Night] Phase: CLOSED — waiting for night window")
                # 10분마다 스냅샷 갱신 — 모니터에 최신 잔고 표시
                if time.monotonic() - _last_closed_snapshot >= 600:
                    try:
                        _bal = await broker.get_balance()
                        _hld = _bal.get("holdings", [])
                        _usd = await broker.get_available_usd()
                        save_portfolio_snapshot(
                            trading_mode=night_settings.trading_mode.value,
                            available_usd=_usd,
                            total_eval_usd=sum(float(h.get("eval_amount", 0)) for h in _hld) + _usd,
                            total_pnl_usd=sum(float(h.get("pnl_amount", 0)) for h in _hld),
                            holdings=_hld,
                        )
                        _last_closed_snapshot = time.monotonic()
                        logger.info(f"[Night] CLOSED 스냅샷 갱신 — available: ${_usd:,.2f}")
                    except Exception as _e:
                        logger.debug(f"[Night] CLOSED 스냅샷 갱신 실패: {_e}")
                await asyncio.sleep(60)
                continue

            if phase == MarketPhase.WAITING:
                next_open = next_market_open_kst()
                wait_secs = seconds_until(next_open)
                logger.info(f"[Night] Phase: WAITING — next open in {wait_secs/60:.0f}min")
                await asyncio.sleep(min(300, wait_secs))
                continue

            if phase == MarketPhase.PRE_MARKET:
                # Pre-market: run analysis only (orders will be skipped by phase check)
                logger.info("[Night] Phase: PRE_MARKET — analysis only")
                try:
                    await run_trading_cycle(
                        broker, sector_analyst, stock_evaluator, stock_picker,
                        asset_manager, buy_executor, sell_executor,
                        tendency_agent, reporter, daily_report,
                    )
                except Exception as e:
                    logger.error(f"Pre-market cycle error: {e}")
                    await reporter.report_error(str(e))
                await asyncio.sleep(cycle_interval)
                continue

            if phase == MarketPhase.MARKET:
                logger.info("[Night] Phase: MARKET — full trading cycle")
                try:
                    await run_trading_cycle(
                        broker, sector_analyst, stock_evaluator, stock_picker,
                        asset_manager, buy_executor, sell_executor,
                        tendency_agent, reporter, daily_report,
                    )

                    # ── 사이클 종료 후 즉시 성과 평가 ──
                    if daily_report.cycles:
                        try:
                            results = await evaluator.run(daily_report)
                            if results:
                                all_agents = [
                                    sector_analyst, stock_evaluator, stock_picker,
                                    asset_manager, buy_executor, sell_executor,
                                    tendency_agent,
                                ]
                                for agent in all_agents:
                                    agent.reload_feedback()
                                logger.info("[Night:Eval] Feedback applied → next cycle")
                                try:
                                    new_profile = await tendency_agent.update_strategy(results)
                                    _save_agent_context("NightTendency", new_profile)
                                except Exception as te:
                                    logger.error(f"Tendency update error: {te}")
                        except Exception as e:
                            logger.error(f"Cycle evaluation error: {e}")

                except Exception as e:
                    logger.error(f"Trading cycle error: {e}")
                    await reporter.report_error(str(e))
                await asyncio.sleep(cycle_interval)
                continue

            if phase == MarketPhase.POST_MARKET:
                if not last_eval_done:
                    last_eval_done = True
                    await reporter.send(daily_report.telegram_summary())
                else:
                    logger.info("[Night] Phase: POST_MARKET — waiting")
                await asyncio.sleep(300)
                continue

    except KeyboardInterrupt:
        logger.info("Kitty Night Mode shutting down...")
    finally:
        await reporter.send("🌙 Kitty Night Mode stopped.")
        await broker.close()


if __name__ == "__main__":
    asyncio.run(main())
