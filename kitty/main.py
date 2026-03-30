"""Kitty - 한국투자증권 멀티 에이전트 자동 매매 시스템"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import holidays

_KST = ZoneInfo("Asia/Seoul")
_kr_holidays = holidays.KR()

from kitty.agents import (
    AssetManagerAgent,
    BuyExecutorAgent,
    SectorAnalystAgent,
    SellExecutorAgent,
    StockEvaluatorAgent,
    StockPickerAgent,
)
from kitty.broker import KISBroker
from kitty.config import settings
from kitty.report import DailyReport
from kitty.telegram import TelegramReporter
from kitty.utils import logger, print_portfolio_and_balance, setup_logger


def _is_pre_market_or_market() -> bool:
    """분석 시작 시간 여부 (평일 + 공휴일 제외 8:50~15:30 KST)"""
    now = datetime.now(_KST)
    if now.weekday() >= 5 or now.date() in _kr_holidays:
        return False
    minutes = now.hour * 60 + now.minute
    return 8 * 60 + 50 <= minutes < 15 * 60 + 30


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
    reporter: TelegramReporter,
    daily_report: DailyReport,
) -> None:
    """매매 사이클 1회 실행"""
    logger.info("=== 매매 사이클 시작 ===")
    daily_report.begin_cycle()

    # 1. 잔고 + 가용현금 조회
    balance_data = await broker.get_balance()
    portfolio = balance_data.get("output1", [])
    available_cash = await broker.get_available_cash()
    balance_summary = balance_data.get("output2", [{}])[0]
    total_asset_value = int(balance_summary.get("tot_evlu_amt", 0))
    logger.info(f"보유종목: {len(portfolio)}개 | 가용현금: {available_cash:,}원 | 총자산: {total_asset_value:,}원")

    # 2. 섹터분석 (SectorAnalystAgent)
    current_date = datetime.now(_KST).strftime("%Y-%m-%d")
    analysis = await sector_analyst.run({"portfolio": portfolio, "current_date": current_date})
    daily_report.record_analysis(analysis)
    reporter.update_analysis(analysis)

    # 3. 후보 종목 + 보유 종목 시세 조회
    candidate_symbols: set[str] = set()
    for sector in analysis.get("sectors", []):
        for symbol in sector.get("candidate_symbols", []):
            candidate_symbols.add(symbol)
    for holding in portfolio:
        pdno = holding.get("pdno", "")
        if pdno:
            candidate_symbols.add(pdno)

    logger.info(f"시세 조회 대상: {sorted(candidate_symbols)}")
    quotes = []
    for symbol in sorted(candidate_symbols):
        try:
            q = await broker.get_quote(symbol)
            quotes.append(q.model_dump())
        except Exception as e:
            logger.error(f"주가 조회 실패 {symbol}: {e}")

    # 4. 종목평가 (StockEvaluatorAgent) - 보유 종목
    stock_evaluation = await stock_evaluator.run({
        "portfolio": portfolio,
        "quotes": quotes,
        "sector_analysis": analysis,
        "max_buy_amount": settings.max_buy_amount,
    })
    daily_report.record_stock_evaluation(stock_evaluation)
    reporter.update_evaluation(stock_evaluation)

    # 5. 종목발굴 (StockPickerAgent) - 신규 후보
    new_candidates = await stock_picker.run({
        "analysis": analysis,
        "quotes": quotes,
        "portfolio": portfolio,
        "available_cash": available_cash,
        "max_buy_amount": settings.max_buy_amount,
    })
    daily_report.record_stock_picks(new_candidates)

    # 6. 자산운용 (AssetManagerAgent) - 최종 주문 결정
    asset_plan = await asset_manager.run({
        "stock_evaluation": stock_evaluation,
        "new_candidates": new_candidates,
        "quotes": quotes,
        "portfolio": portfolio,
        "available_cash": available_cash,
        "total_asset_value": total_asset_value,
        "max_buy_amount": settings.max_buy_amount,
    })
    daily_report.record_asset_management(asset_plan)

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

    reporter = TelegramReporter().build()
    reporter.set_broker(broker)

    daily_report = DailyReport()

    async def _cycle_now() -> None:
        await run_trading_cycle(
            broker,
            sector_analyst,
            stock_evaluator,
            stock_picker,
            asset_manager,
            buy_executor,
            sell_executor,
            reporter,
            daily_report,
        )

    reporter.set_daily_report(daily_report)
    reporter.set_cycle_callback(_cycle_now)

    await reporter.start_polling()
    await reporter.send(f"🐱 Kitty 시작! 모드: `{settings.trading_mode.value}`")
    await print_portfolio_and_balance(broker, label="시작")
    last_report_date = daily_report.date

    try:
        while True:
            now = datetime.now(_KST)

            # 날짜가 바뀌면 일일 리포트 텔레그램 발송 후 새 리포트 시작
            today = now.strftime("%Y-%m-%d")
            if today != last_report_date:
                await reporter.send(daily_report.telegram_summary())
                daily_report = DailyReport()
                last_report_date = today

            # 8:50 이전 또는 15:30 이후에는 사이클 건너뜀 (모의/실전 공통)
            if not _is_pre_market_or_market():
                logger.info("장 외 시간 - 대기 중")
            elif not reporter.is_paused:
                try:
                    await run_trading_cycle(
                        broker,
                        sector_analyst,
                        stock_evaluator,
                        stock_picker,
                        asset_manager,
                        buy_executor,
                        sell_executor,
                        reporter,
                        daily_report,
                    )
                except Exception as e:
                    logger.error(f"매매 사이클 오류: {e}")
                    await reporter.report_error(str(e))
            else:
                logger.info("매매 일시정지 중...")

            # 5분마다 실행
            await asyncio.sleep(300)

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
