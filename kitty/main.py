"""Kitty - 한국투자증권 멀티 에이전트 자동 매매 시스템"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_MODE_REQ          = Path("commands/mode_request.json")
_CHAT_DIR          = Path("commands/chat")
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
    TendencyAgent,
)
from kitty.broker import KISBroker
from kitty.config import settings
from kitty.evaluator import PerformanceEvaluator
from kitty.report import DailyReport
from kitty.telegram import TelegramReporter
from kitty.utils import logger, print_portfolio_and_balance, setup_logger


async def _collect_market_data(broker: KISBroker) -> dict:
    """실시간 시장 지표 + 거래량 상위 종목 수집"""
    # 시장 지표 (ETF + 대형주)
    barometers: list[dict] = []
    for sym in _BAROMETER_SYMBOLS:
        try:
            q = await broker.get_quote(sym)
            barometers.append(q.model_dump())
        except Exception as e:
            logger.debug(f"시장지표 {sym} 조회 실패: {e}")
        await asyncio.sleep(0.2)  # KIS API rate limit 방지

    # 거래량 상위 종목
    volume_leaders: list[dict] = []
    try:
        volume_leaders = await broker.get_volume_rank(20)
    except Exception as e:
        logger.warning(f"거래량순위 조회 실패 (무시): {e}")

    return {"barometers": barometers, "volume_leaders": volume_leaders}


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
    daily_report.begin_cycle()

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

    # 1.5. 실시간 시장 데이터 수집 (지표 + 거래량 순위)
    market_data = await _collect_market_data(broker)
    logger.info(
        f"시장데이터: 지표 {len(market_data['barometers'])}개 | "
        f"거래량순위 {len(market_data['volume_leaders'])}개"
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

    # 3. 후보 종목 + 보유 종목 + 거래량 상위 종목 시세 조회
    candidate_symbols: set[str] = set()
    for sector in analysis.get("sectors", []):
        for symbol in sector.get("candidate_symbols", []):
            candidate_symbols.add(symbol)
    for holding in portfolio:
        pdno = holding.get("pdno", "")
        if pdno:
            candidate_symbols.add(pdno)
    # 거래량 상위 종목도 후보에 추가 (유동성 있는 종목 보장)
    for vl in market_data.get("volume_leaders", []):
        sym = vl.get("symbol", "")
        if sym:
            candidate_symbols.add(sym)

    logger.info(f"시세 조회 대상: {sorted(candidate_symbols)}")
    quotes = []
    for symbol in sorted(candidate_symbols):
        try:
            q = await broker.get_quote(symbol)
            quotes.append(q.model_dump())
        except Exception as e:
            logger.error(f"주가 조회 실패 {symbol}: {e}")
        await asyncio.sleep(0.2)  # KIS API rate limit 방지 (초당 5건)

    # 4. 종목평가 (StockEvaluatorAgent) - 보유 종목
    stock_evaluation = await stock_evaluator.run({
        "portfolio": portfolio,
        "quotes": quotes,
        "sector_analysis": analysis,
        "max_buy_amount": settings.max_buy_amount,
        "tendency_directive": tendency_directive,
    })
    daily_report.record_stock_evaluation(stock_evaluation)
    reporter.update_evaluation(stock_evaluation)
    _save_agent_context("종목평가가", stock_evaluation)

    # 5. 종목발굴 (StockPickerAgent) - 신규 후보 + 거래량 데이터
    new_candidates = await stock_picker.run({
        "analysis": analysis,
        "quotes": quotes,
        "portfolio": portfolio,
        "available_cash": available_cash,
        "max_buy_amount": settings.max_buy_amount,
        "tendency_directive": tendency_directive,
        "volume_leaders": market_data.get("volume_leaders", []),
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

    # 8. 매수/매도 실행
    buy_result = await buy_executor.run({"final_orders": final_orders, "quotes": quotes})
    sell_result = await sell_executor.run({
        "final_orders": final_orders,
        "portfolio": portfolio,
        "quotes": quotes,
    })

    buy_results = buy_result.get("buy_results", [])
    sell_results = sell_result.get("sell_results", [])
    daily_report.record_executions(buy_results, sell_results)
    _save_agent_context("매수실행가", {"buy_results": buy_results})
    _save_agent_context("매도실행가", {"sell_results": sell_results})

    # 텔레그램 체결 보고
    for r in buy_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            await reporter.report_trade("BUY", r["symbol"], r["quantity"], r.get("price", 0), "전략 매수")

    for r in sell_results:
        if r.get("status") not in ("SKIPPED", "FAILED"):
            await reporter.report_trade("SELL", r["symbol"], r["quantity"], r.get("price", 0), "전략 매도")

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
    logger.info(f"🐱 Kitty 시작 - 모드: {settings.trading_mode.value}")

    broker = KISBroker()
    sector_analyst = SectorAnalystAgent()
    stock_evaluator = StockEvaluatorAgent()
    stock_picker = StockPickerAgent()
    asset_manager = AssetManagerAgent()
    buy_executor = BuyExecutorAgent(broker)
    sell_executor = SellExecutorAgent(broker)
    tendency_agent = TendencyAgent(profile_name="aggressive")

    reporter = TelegramReporter().build()
    reporter.set_broker(broker)

    daily_report = DailyReport()

    _last_cycle_time: float = 0.0

    async def _cycle_now() -> None:
        nonlocal _last_cycle_time
        _last_cycle_time = time.monotonic()
        await run_trading_cycle(
            broker,
            sector_analyst,
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
        "종목평가가": stock_evaluator,
        "종목발굴가": stock_picker,
        "자산운용가": asset_manager,
        "매수실행가": buy_executor,
        "매도실행가": sell_executor,
        "투자성향관리자": tendency_agent,
    }
    asyncio.create_task(_chat_handler(_agents_map))

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

    await reporter.send(f"🐱 Kitty 시작! 모드: `{settings.trading_mode.value}`")

    try:
        await print_portfolio_and_balance(broker, label="시작")
    except Exception as e:
        logger.warning(f"시작 시 잔고 조회 실패 (무시): {e}")
    last_report_date = daily_report.date
    last_eval_date: str = ""
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

            # 장 마감 직후 성과 평가 (15:35~16:00, 하루 1회)
            if _is_post_market_eval_window() and last_eval_date != today:
                last_eval_date = today
                try:
                    results = await evaluator.run(daily_report)
                    if results:
                        # 에이전트 system_prompt 즉시 갱신
                        for agent in [sector_analyst, stock_evaluator, stock_picker,
                                      asset_manager, buy_executor, sell_executor,
                                      tendency_agent]:
                            agent.reload_feedback()
                        await reporter.send(_format_eval_summary(results))

                        # 투자성향관리자: 성과 분석 기반 내일 레벨 결정
                        try:
                            new_profile = await tendency_agent.update_strategy(results)
                            _save_agent_context("투자성향관리자", new_profile)
                            await reporter.send(_format_tendency_update(new_profile))
                        except Exception as te:
                            logger.error(f"투자성향 업데이트 오류: {te}")
                except Exception as e:
                    logger.error(f"성과 평가 오류: {e}")

            # 8:50 이전 또는 15:30 이후에는 사이클 건너뜀 (모의/실전 공통)
            if not _is_pre_market_or_market():
                logger.info("장 외 시간 - 대기 중")
            elif not reporter.is_paused:
                try:
                    _last_cycle_time = time.monotonic()
                    await run_trading_cycle(
                        broker,
                        sector_analyst,
                        stock_evaluator,
                        stock_picker,
                        asset_manager,
                        buy_executor,
                        sell_executor,
                        tendency_agent,
                        reporter,
                        daily_report,
                    )
                except Exception as e:
                    logger.error(f"매매 사이클 오류: {e}")
                    await reporter.report_error(str(e))
            else:
                logger.info("매매 일시정지 중...")

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
