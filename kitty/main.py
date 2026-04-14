"""Kitty - 한국투자증권 멀티 에이전트 자동 매매 시스템"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_MODE_REQ          = Path("commands/mode_request.json")
_CHAT_DIR          = Path("commands/chat")
_FORCE_SELL_DIR    = Path("commands")
_AGENT_CONTEXT_PATH = Path("logs/agent_context.json")

import holidays

_KST = ZoneInfo("Asia/Seoul")
_kr_holidays = holidays.KR()

# 시장 지표 심볼 (ETF + 대형 대표주)
_BAROMETER_SYMBOLS = [
    "069500",  # KODEX 200 (KOSPI 프록시)
    "229200",  # KODEX 코스닥150
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "005380",  # 현대차
    "035420",  # NAVER
    "068270",  # 셀트리온
    "006400",  # 삼성SDI
    "105560",  # KB금융
    "051910",  # LG화학
]

from kitty.agents import (
    AssetManagerAgent,
    BuyExecutorAgent,
    SectorAnalystAgent,
    SellExecutorAgent,
    StockEvaluatorAgent,
    StockPickerAgent,
    StockScreenerAgent,
    TendencyAgent,
)
from kitty.broker import KISBroker
from kitty.config import settings
from kitty.evaluator import PerformanceEvaluator
from kitty.report import DailyReport
from kitty.telegram import TelegramReporter
from kitty.utils import logger, print_portfolio_and_balance, setup_logger


async def _collect_market_data(broker: KISBroker) -> dict:
    """실시간 시장 지표 + 거래량/등락률 상위 종목 수집 (KOSPI + KOSDAQ 전체)"""
    # 시장 지표 (ETF + 대형주)
    barometers: list[dict] = []
    for sym in _BAROMETER_SYMBOLS:
        try:
            q = await broker.get_quote(sym)
            barometers.append(q.model_dump())
        except Exception as e:
            logger.debug(f"시장지표 {sym} 조회 실패: {e}")

    # KOSPI 거래량 순위
    kospi_vol: list[dict] = []
    try:
        kospi_vol = await broker.get_volume_rank(market="J", count=50)
    except Exception as e:
        logger.warning(f"KOSPI 거래량순위 조회 실패 (무시): {e}")

    # KOSDAQ 거래량 순위
    kosdaq_vol: list[dict] = []
    try:
        kosdaq_vol = await broker.get_volume_rank(market="Q", count=50)
    except Exception as e:
        logger.warning(f"KOSDAQ 거래량순위 조회 실패 (무시): {e}")

    # KOSPI 등락률 순위
    kospi_chg: list[dict] = []
    try:
        kospi_chg = await broker.get_change_rate_rank(market="J", count=50)
    except Exception as e:
        logger.warning(f"KOSPI 등락률순위 조회 실패 (무시): {e}")

    # KOSDAQ 등락률 순위
    kosdaq_chg: list[dict] = []
    try:
        kosdaq_chg = await broker.get_change_rate_rank(market="Q", count=50)
    except Exception as e:
        logger.warning(f"KOSDAQ 등락률순위 조회 실패 (무시): {e}")

    market_pool = kospi_vol + kosdaq_vol + kospi_chg + kosdaq_chg

    return {
        "barometers": barometers,
        "volume_leaders": kospi_vol,  # 기존 호환성 유지 (섹터분석가 입력)
        "market_pool": market_pool,
    }


def _save_agent_context(agent_name: str, output: dict) -> None:
    """에이전트 마지막 출력을 logs/agent_context.json 에 저장 (채팅 컨텍스트용)"""
    try:
        ctx: dict = {}
        if _AGENT_CONTEXT_PATH.exists():
            try:
                ctx = json.loads(_AGENT_CONTEXT_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        ctx[agent_name] = {
            "ts":     datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
            "output": output,
        }
        _AGENT_CONTEXT_PATH.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug(f"에이전트 컨텍스트 저장 실패: {e}")


async def _chat_handler(agents_map: dict) -> None:
    """commands/chat/req_*.json 채팅 요청 처리 → res_*.json 응답 (2초 폴링)"""
    while True:
        await asyncio.sleep(2)
        try:
            _CHAT_DIR.mkdir(parents=True, exist_ok=True)
            for req_file in sorted(_CHAT_DIR.glob("req_*.json")):
                try:
                    req       = json.loads(req_file.read_text(encoding="utf-8"))
                    req_id    = req.get("id", "")
                    agent_name = req.get("agent", "")
                    message   = req.get("message", "")

                    agent = agents_map.get(agent_name)
                    if agent is None:
                        reply = f"에이전트 '{agent_name}'를 찾을 수 없습니다."
                    else:
                        context = ""
                        if _AGENT_CONTEXT_PATH.exists():
                            try:
                                ctx_data   = json.loads(_AGENT_CONTEXT_PATH.read_text(encoding="utf-8"))
                                agent_ctx  = ctx_data.get(agent_name, {})
                                if agent_ctx.get("output"):
                                    context = json.dumps(agent_ctx["output"], ensure_ascii=False, indent=2)
                            except Exception:
                                pass
                        reply = await agent.chat(message, context)

                    res_file = _CHAT_DIR / f"res_{req_id}.json"
                    res_file.write_text(
                        json.dumps({"id": req_id, "agent": agent_name, "reply": reply,
                                    "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False),
                        encoding="utf-8",
                    )
                    req_file.unlink(missing_ok=True)
                    logger.debug(f"[채팅] {agent_name} 응답 완료 ({req_id})")
                except Exception as e:
                    logger.warning(f"[채팅] 요청 처리 실패 {req_file.name}: {e}")
                    req_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[채팅] 핸들러 오류: {e}")


async def _force_sell_handler(broker: "KISBroker") -> None:
    """commands/force_sell_{symbol}.json 청산 요청 처리 (2초 폴링)"""
    while True:
        await asyncio.sleep(2)
        try:
            _FORCE_SELL_DIR.mkdir(parents=True, exist_ok=True)
            for req_file in sorted(_FORCE_SELL_DIR.glob("force_sell_*.json")):
                try:
                    req = json.loads(req_file.read_text(encoding="utf-8"))
                    symbol = req.get("symbol", "")
                    qty = int(req.get("qty", 0))
                    req_file.unlink(missing_ok=True)
                    if not symbol or qty <= 0:
                        logger.warning(f"[청산요청] 잘못된 요청: {req}")
                        continue
                    logger.info(f"[청산요청] {symbol} {qty}주 즉시 청산 시작")
                    try:
                        order = await broker.sell(symbol, qty, 0)
                        logger.info(f"[청산요청] {symbol} 청산 완료: {order}")
                    except Exception as e:
                        logger.error(f"[청산요청] {symbol} 청산 실패: {e}")
                except Exception as e:
                    logger.warning(f"[청산요청] 파일 처리 실패 {req_file.name}: {e}")
                    req_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[청산요청] 핸들러 오류: {e}")


def _is_pre_market_or_market() -> bool:
    """분석 시작 시간 여부 (평일 + 공휴일 제외 8:50~15:30 KST)"""
    now = datetime.now(_KST)
    if now.weekday() >= 5 or now.date() in _kr_holidays:
        return False
    minutes = now.hour * 60 + now.minute
    return 8 * 60 + 50 <= minutes < 15 * 60 + 30


def _is_post_market_eval_window() -> bool:
    """장 마감 직후 평가 실행 구간 (15:35 ~ 16:00 KST)"""
    now = datetime.now(_KST)
    if now.weekday() >= 5 or now.date() in _kr_holidays:
        return False
    minutes = now.hour * 60 + now.minute
    return 15 * 60 + 35 <= minutes < 16 * 60


def _is_market_hours() -> bool:
    """주문 가능 시간 여부 (평일 + 공휴일 제외 9:00~15:30 KST)"""
    now = datetime.now(_KST)
    return (
        now.weekday() < 5
        and now.date() not in _kr_holidays
        and (9 <= now.hour < 15 or (now.hour == 15 and now.minute < 30))
    )


async def run_trading_cycle(
    broker: KISBroker,
    sector_analyst: SectorAnalystAgent,
    stock_screener: StockScreenerAgent,
    stock_evaluator: StockEvaluatorAgent,
    stock_picker: StockPickerAgent,
    asset_manager: AssetManagerAgent,
    buy_executor: BuyExecutorAgent,
    sell_executor: SellExecutorAgent,
    tendency_agent: TendencyAgent,
    reporter: TelegramReporter,
    daily_report: DailyReport,
) -> None:
    """매매 사이클 1회 실행"""
    logger.info("=== 매매 사이클 시작 ===")
    daily_report.begin_cycle(mode=settings.trading_mode.value)

    # 0. 투자 성향 디렉티브 생성 (AI 호출 없음)
    tendency_directive = tendency_agent.get_directive()
    logger.info(f"[투자성향] {tendency_agent.profile['label']}")
    _save_agent_context("투자성향관리자", tendency_agent.profile)

    # 1. 잔고 + 가용현금 조회
    balance_data = await broker.get_balance()
    portfolio = balance_data.get("output1", [])
    available_cash = await broker.get_available_cash()
    balance_summary = balance_data.get("output2", [{}])[0]
    total_asset_value = int(balance_summary.get("tot_evlu_amt", 0))
    logger.info(f"보유종목: {len(portfolio)}개 | 가용현금: {available_cash:,}원 | 총자산: {total_asset_value:,}원")

    # 1.5. 실시간 시장 데이터 수집 (지표 + KOSPI/KOSDAQ 거래량·등락률 순위)
    market_data = await _collect_market_data(broker)
    logger.info(
        f"시장데이터: 지표 {len(market_data['barometers'])}개 | "
        f"시장풀 {len(market_data['market_pool'])}개 (중복 포함)"
    )

    # 2. 섹터분석 (SectorAnalystAgent) — 실시간 데이터 기반
    current_date = datetime.now(_KST).strftime("%Y-%m-%d")
    analysis = await sector_analyst.run({
        "portfolio": portfolio,
        "current_date": current_date,
        "market_data": market_data,
    })
    daily_report.record_analysis(analysis)
    reporter.update_analysis(analysis)
    _save_agent_context("섹터분석가", analysis)

    # 포트폴리오 다양성 메타데이터
    portfolio_meta = {
        "holdings_count": len(portfolio),
        "target_min_holdings": 3,
    }

    # 2.5. 종목스크리닝 (StockScreenerAgent) — KOSPI+KOSDAQ 전종목 섹터 필터링
    holdings_symbols = [h.get("pdno", "") for h in portfolio if h.get("pdno")]
    screener_result = await stock_screener.run({
        "sector_analysis": analysis,
        "market_pool": market_data["market_pool"],
        "holdings_symbols": holdings_symbols,
    })
    screened = screener_result.get("screened", [])
    logger.info(f"[스크리너] {screener_result.get('summary', f'{len(screened)}개 선별')}")
    _save_agent_context("종목스크리너", screener_result)

    # 3. 스크리닝된 후보 + 보유 종목 시세 조회
    candidate_symbols: set[str] = set()
    for s in screened:
        sym = s.get("symbol", "")
        if sym:
            candidate_symbols.add(sym)
    for holding in portfolio:
        pdno = holding.get("pdno", "")
        if pdno:
            candidate_symbols.add(pdno)
    # 섹터분석 candidate_symbols도 보조 추가 (스크리너 실패 시 폴백)
    for sector in analysis.get("sectors", []):
        for symbol in sector.get("candidate_symbols", []):
            candidate_symbols.add(symbol)

    logger.info(f"시세 조회 대상: {len(candidate_symbols)}개")
    quotes = []
    for symbol in sorted(candidate_symbols):
        try:
            q = await broker.get_quote(symbol)
            quotes.append(q.model_dump())
        except Exception as e:
            logger.error(f"주가 조회 실패 {symbol}: {e}")
        # _throttle_quote()가 broker 내부에서 0.25s 간격 보장

    # 4. 종목평가 (StockEvaluatorAgent) - 보유 종목
    stock_evaluation = await stock_evaluator.run({
        "portfolio": portfolio,
        "quotes": quotes,
        "sector_analysis": analysis,
        "max_buy_amount": settings.max_buy_amount,
        "tendency_directive": tendency_directive,
        "portfolio_meta": portfolio_meta,
    })
    daily_report.record_stock_evaluation(stock_evaluation)
    reporter.update_evaluation(stock_evaluation)
    _save_agent_context("종목평가가", stock_evaluation)

    # 5. 종목발굴 (StockPickerAgent) - 스크리닝된 후보 + 시세 기반 최종 선정
    new_candidates = await stock_picker.run({
        "analysis": analysis,
        "quotes": quotes,
        "portfolio": portfolio,
        "available_cash": available_cash,
        "max_buy_amount": settings.max_buy_amount,
        "tendency_directive": tendency_directive,
        "volume_leaders": market_data.get("volume_leaders", []),
        "portfolio_meta": portfolio_meta,
        "screened_candidates": screened,
    })
    daily_report.record_stock_picks(new_candidates)
    _save_agent_context("종목발굴가", new_candidates)

    # 6. 자산운용 (AssetManagerAgent) - 최종 주문 결정
    asset_plan = await asset_manager.run({
        "stock_evaluation": stock_evaluation,
        "new_candidates": new_candidates,
        "quotes": quotes,
        "portfolio": portfolio,
        "available_cash": available_cash,
        "total_asset_value": total_asset_value,
        "max_buy_amount": settings.max_buy_amount,
        "max_position_size": settings.max_position_size,
        "tendency_directive": tendency_directive,
        "portfolio_meta": portfolio_meta,
    })
    daily_report.record_asset_management(asset_plan)
    _save_agent_context("자산운용가", asset_plan)

    final_orders = asset_plan.get("final_orders", [])
    if not final_orders:
        logger.info("이번 사이클에 최종 주문 없음")
        daily_report.end_cycle()
        return

    # 7. 장 시간 체크
    if not _is_market_hours():
        logger.info(f"장 외 시간 — 주문 {len(final_orders)}건 스킵 (분석만 완료)")
        daily_report.end_cycle()
        reporter.mark_cycle_done()
        return

    # 8. 매도 먼저 실행 (잔고 확보) → 매수 실행
    sell_result = await sell_executor.run({
        "final_orders": final_orders,
        "portfolio": portfolio,
        "quotes": quotes,
    })
    sell_results = sell_result.get("sell_results", [])

    # 매도 후 가용현금 재조회하여 매수 한도 검증
    try:
        refreshed_cash = await broker.get_available_cash()
        logger.info(f"매도 후 가용현금: {refreshed_cash:,}원")
    except Exception:
        refreshed_cash = available_cash

    buy_result = await buy_executor.run({
        "final_orders": final_orders,
        "quotes": quotes,
        "available_cash": refreshed_cash,
    })
    buy_results = buy_result.get("buy_results", [])
    daily_report.record_executions(buy_results, sell_results)
    _save_agent_context("매수실행가", {"buy_results": buy_results})
    _save_agent_context("매도실행가", {"sell_results": sell_results})

    # 텔레그램 체결 보고 (시장가 주문은 quote 가격을 참조 가격으로 사용)
    quote_map = {q["symbol"]: q for q in quotes}
    for r in buy_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            price = r.get("price") or int(quote_map.get(r["symbol"], {}).get("current_price", 0))
            await reporter.report_trade("BUY", r["symbol"], r["quantity"], price, "전략 매수", name=r.get("name", ""))

    for r in sell_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            price = r.get("price") or int(quote_map.get(r["symbol"], {}).get("current_price", 0))
            await reporter.report_trade("SELL", r["symbol"], r["quantity"], price, "전략 매도", name=r.get("name", ""))

    daily_report.end_cycle()
    reporter.mark_cycle_done()

    # 9. 포트폴리오 출력
    await print_portfolio_and_balance(broker, label="사이클 완료")
    logger.info("=== 매매 사이클 완료 ===")


def _format_eval_summary(results: dict) -> str:
    """성과 평가 결과 → 텔레그램 메시지"""
    lines = ["📊 *오늘의 에이전트 성과 평가*\n"]
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
    lines.append("\n_피드백이 각 에이전트에 반영되었습니다._")
    return "\n".join(lines)


def _format_tendency_update(profile: dict) -> str:
    """투자성향 업데이트 결과 → 텔레그램 메시지"""
    from kitty.agents.tendency import DIMS, DIM_LABELS, LEVEL_LABEL, LEVEL_VALUES

    label = profile.get("label", "-")
    levels = profile.get("levels", {})
    rationale = profile.get("rationale", "")
    updated_at = profile.get("updated_at", "")

    lines = [f"📐 *내일 투자 성향 확정*\n"]
    lines.append(f"성향: *{label}*\n")

    dim_display = {
        "take_profit": ("익절",    lambda v: f"+{v:.1f}%"),
        "stop_loss":   ("손절",    lambda v: f"{v:.1f}%"),
        "cash":        ("현금",    lambda v: f"{int(v*100)}%"),
        "max_weight":  ("종목집중", lambda v: f"최대 {v:.0f}%"),
        "entry":       ("진입기준", lambda v: f"±{v:.1f}%"),
    }
    for dim in DIMS:
        lv = levels.get(dim, "-")
        name, fmt = dim_display[dim]
        val = fmt(LEVEL_VALUES[dim][lv]) if isinstance(lv, int) else "-"
        lv_label = LEVEL_LABEL.get(lv, "-") if isinstance(lv, int) else "-"
        lines.append(f"  {name}: `L{lv} {lv_label}` → {val}")

    if rationale:
        lines.append(f"\n💭 _{rationale}_")
    if updated_at:
        lines.append(f"\n_내일 장 시작부터 위 기준이 적용됩니다._")
    return "\n".join(lines)


async def main() -> None:
    setup_logger()

    # 저장된 모드 설정 복원 (대시보드 전환 영속화 — 컨테이너 재시작 후에도 유지)
    _MODE_CONFIG = Path("commands/mode_config.json")
    if _MODE_CONFIG.exists():
        try:
            saved_mode = json.loads(_MODE_CONFIG.read_text(encoding="utf-8")).get("mode", "")
            if saved_mode in ("paper", "live"):
                from kitty.config import TradingMode
                settings.trading_mode = TradingMode(saved_mode)
                logger.info(f"[mode_config] 저장된 모드 복원: {saved_mode}")
        except Exception as e:
            logger.warning(f"[mode_config] 읽기 실패: {e}")

    logger.info(f"🐱 Kitty 시작 - 모드: {settings.trading_mode.value}")

    broker = KISBroker()
    sector_analyst = SectorAnalystAgent()
    stock_screener = StockScreenerAgent()
    stock_evaluator = StockEvaluatorAgent()
    stock_picker = StockPickerAgent()
    asset_manager = AssetManagerAgent()
    buy_executor = BuyExecutorAgent(broker)
    sell_executor = SellExecutorAgent(broker)
    tendency_agent = TendencyAgent(profile_name="aggressive")

    reporter = TelegramReporter().build()
    reporter.set_broker(broker)

    daily_report = DailyReport()

    _last_cycle_time: float = time.monotonic()

    async def _cycle_now() -> None:
        nonlocal _last_cycle_time
        _last_cycle_time = time.monotonic()
        await run_trading_cycle(
            broker,
            sector_analyst,
            stock_screener,
            stock_evaluator,
            stock_picker,
            asset_manager,
            buy_executor,
            sell_executor,
            tendency_agent,
            reporter,
            daily_report,
        )

    reporter.set_daily_report(daily_report)
    reporter.set_cycle_callback(_cycle_now)

    # 채팅 핸들러 백그라운드 태스크
    _agents_map = {
        "섹터분석가": sector_analyst,
        "종목스크리너": stock_screener,
        "종목평가가": stock_evaluator,
        "종목발굴가": stock_picker,
        "자산운용가": asset_manager,
        "매수실행가": buy_executor,
        "매도실행가": sell_executor,
        "투자성향관리자": tendency_agent,
    }
    asyncio.create_task(_chat_handler(_agents_map))
    asyncio.create_task(_force_sell_handler(broker))

    # Telegram 폴링 시작 — 네트워크 미준비 or API 일시 오류 시 재시도
    for attempt in range(1, 6):
        try:
            await reporter.start_polling()
            break
        except Exception as e:
            logger.warning(f"Telegram 폴링 시작 실패 ({attempt}/5): {e}")
            if attempt == 5:
                logger.error("Telegram 폴링 최종 실패 — 봇 없이 계속 실행")
            else:
                await asyncio.sleep(10 * attempt)

    dashboard_url = f"http://{await reporter.fetch_dashboard_url()}:{settings.monitor_port}"
    await reporter.send(
        f"🐱 Kitty 시작! 모드: `{settings.trading_mode.value}`\n"
        f"📊 대시보드: [{dashboard_url}]({dashboard_url})"
    )

    try:
        await print_portfolio_and_balance(broker, label="시작")
    except Exception as e:
        logger.warning(f"시작 시 잔고 조회 실패 (무시): {e}")
    last_report_date = daily_report.date
    # last_eval_date 제거됨 — 매 사이클 평가로 전환
    evaluator = PerformanceEvaluator(broker)

    try:
        while True:
            now = datetime.now(_KST)

            # 모니터 대시보드 모드 전환 요청 확인
            if _MODE_REQ.exists():
                try:
                    req = json.loads(_MODE_REQ.read_text(encoding="utf-8"))
                    new_mode = req.get("mode", "")
                    if new_mode in ("paper", "live"):
                        from kitty.config import TradingMode
                        settings.trading_mode = TradingMode(new_mode)
                        broker.reset_token()
                        logger.info(f"[모니터] 모드 전환: {new_mode}")
                        await reporter.send(f"🔄 모드 전환: `{new_mode}` (모니터 대시보드)")
                        await _cycle_now()  # 즉시 사이클 실행 → 포트폴리오 현행화
                except Exception as e:
                    logger.warning(f"모드 전환 요청 처리 실패: {e}")
                finally:
                    _MODE_REQ.unlink(missing_ok=True)

            # 날짜가 바뀌면 일일 리포트 텔레그램 발송 후 새 리포트 시작
            today = now.strftime("%Y-%m-%d")
            if today != last_report_date:
                await reporter.send(daily_report.telegram_summary())
                daily_report = DailyReport()
                last_report_date = today

            # 8:50 이전 또는 15:30 이후에는 사이클 건너뜀 (모의/실전 공통)
            if not _is_pre_market_or_market():
                logger.debug("장 외 시간 - 대기 중")
            elif not reporter.is_paused:
                try:
                    await run_trading_cycle(
                        broker,
                        sector_analyst,
                        stock_screener,
                        stock_evaluator,
                        stock_picker,
                        asset_manager,
                        buy_executor,
                        sell_executor,
                        tendency_agent,
                        reporter,
                        daily_report,
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
                                logger.info("[평가] 피드백 반영 완료 → 다음 사이클에 적용")

                                # 투자성향 업데이트
                                try:
                                    new_profile = await tendency_agent.update_strategy(results)
                                    _save_agent_context("투자성향관리자", new_profile)
                                except Exception as te:
                                    logger.error(f"투자성향 업데이트 오류: {te}")
                        except Exception as e:
                            logger.error(f"사이클 평가 오류: {e}")

                except Exception as e:
                    logger.error(f"매매 사이클 오류: {e}")
                    await reporter.report_error(str(e))
                finally:
                    _last_cycle_time = time.monotonic()
                    # 매 사이클 완료 시 snapshot 갱신 (주문 없는 사이클에서도 현재가 반영)
                    try:
                        await print_portfolio_and_balance(broker)
                    except Exception as _se:
                        logger.debug(f"포트폴리오 스냅샷 갱신 실패: {_se}")
            else:
                logger.debug("매매 일시정지 중...")

            # 마지막 사이클 실행 시각 기준 300초 대기 (즉시 실행 요청 시 타이머 리셋)
            elapsed = time.monotonic() - _last_cycle_time
            wait = max(0.0, 300.0 - elapsed)
            await asyncio.sleep(wait)

    except KeyboardInterrupt:
        logger.info("Kitty 종료 중...")
    finally:
        # 종료 시 최종 리포트 발송
        await reporter.send(daily_report.telegram_summary())
        await reporter.send("🛑 Kitty가 종료되었습니다.")
        await reporter.stop_polling()
        await broker.close()


if __name__ == "__main__":
    asyncio.run(main())
