"""매수 실행 에이전트 - 스마트 주문 실행"""
import asyncio
import math
from typing import Any

from kitty.broker import KISBroker, OrderResult
from kitty.utils import logger

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 주식 매수 전문가입니다.

역할:
- 자산운용가의 매수 지시를 실행합니다
- 호가를 분석해 최적의 매수 타이밍과 가격을 결정합니다
- 분할 매수가 필요한지 판단합니다
- 실행 후 결과를 보고합니다

원칙:
- 상한가 종목은 매수하지 않습니다
- 거래량이 평균의 50% 미만이면 매수를 보류합니다
- 전일 대비 +10% 이상 급등 종목은 신중하게 접근합니다
"""


class BuyExecutorAgent(BaseAgent):
    def __init__(self, broker: KISBroker) -> None:
        super().__init__(name="매수실행가", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_buy(
        self,
        symbol: str,
        quantity: int,
        price: int,
        order_type: str,
        priority: str,
    ) -> list[dict[str, Any]]:
        """
        SPLIT: divide quantity into chunks of max 3, try limit order then market fallback
        SINGLE: direct order

        For each chunk:
        1. Place limit order at current_price (or price if specified)
        2. Wait 8 seconds
        3. Check fill via get_order_status()
        4. If not filled: cancel, wait 2s, retry with market order
        5. Max 3 total attempts
        """
        chunk_results: list[dict[str, Any]] = []

        # Determine order price (use current quote if price is 0)
        order_price = price
        if order_price == 0:
            try:
                quote = await self.broker.get_quote(symbol)
                order_price = quote.current_price
            except Exception as e:
                logger.warning(f"[매수실행가] {symbol} 현재가 조회 실패, 시장가 사용: {e}")
                order_price = 0

        use_split = order_type == "SPLIT" or quantity > 5

        if use_split:
            # Divide into chunks of up to 3
            num_chunks = min(3, math.ceil(quantity / max(1, quantity // 3)))
            base_qty = quantity // num_chunks
            remainder = quantity % num_chunks
            chunks = [base_qty + (1 if i < remainder else 0) for i in range(num_chunks)]
        else:
            chunks = [quantity]

        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{symbol} 청크{i + 1}/{len(chunks)} ({chunk_qty}주)"
            success = False

            for attempt in range(3):
                try:
                    if attempt == 0 and order_price > 0:
                        # First attempt: limit order at current price
                        logger.info(f"[매수실행가] {chunk_label} 지정가 매수 시도 @ {order_price:,}원")
                        order: OrderResult = await self.broker.buy(symbol, chunk_qty, order_price)
                        order_id = order.order_id

                        # Wait 8 seconds for fill
                        await asyncio.sleep(8)

                        # Check fill status
                        try:
                            status = await self.broker.get_order_status(order_id)
                            filled_qty = status.get("filled_qty", 0)
                            remaining_qty = status.get("remaining_qty", chunk_qty)

                            if remaining_qty == 0:
                                # Fully filled
                                logger.info(f"[매수실행가] {chunk_label} 지정가 체결 완료")
                                chunk_results.append({
                                    "symbol": symbol,
                                    "order_id": order_id,
                                    "status": "FILLED",
                                    "quantity": chunk_qty,
                                    "price": order_price,
                                    "chunk": i + 1,
                                })
                                success = True
                                break
                            else:
                                # Partially filled or not filled — cancel remaining
                                logger.info(
                                    f"[매수실행가] {chunk_label} 미체결({remaining_qty}주 잔여) — 취소 후 시장가 재시도"
                                )
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(2)
                                # Fall through to market order on next attempt
                                order_price = 0
                        except Exception as e:
                            logger.warning(f"[매수실행가] {chunk_label} 체결 조회 실패: {e}")
                            # Fall through to market order
                            order_price = 0
                    else:
                        # Fallback: market order
                        logger.info(f"[매수실행가] {chunk_label} 시장가 매수 시도 (attempt {attempt + 1})")
                        order = await self.broker.buy(symbol, chunk_qty, 0)
                        logger.info(f"[매수실행가] {chunk_label} 시장가 매수 주문 완료")
                        chunk_results.append({
                            "symbol": symbol,
                            "order_id": order.order_id,
                            "status": "SUBMITTED",
                            "quantity": chunk_qty,
                            "price": 0,
                            "chunk": i + 1,
                        })
                        success = True
                        break

                except Exception as e:
                    logger.error(f"[매수실행가] {chunk_label} attempt {attempt + 1} 실패: {e}")
                    if attempt == 2:
                        chunk_results.append({
                            "symbol": symbol,
                            "status": "FAILED",
                            "quantity": chunk_qty,
                            "reason": str(e),
                            "chunk": i + 1,
                        })

            if not success and not any(
                r.get("chunk") == i + 1 for r in chunk_results
            ):
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": chunk_qty,
                    "reason": "최대 재시도 초과",
                    "chunk": i + 1,
                })

        return chunk_results

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "final_orders": 자산운용가의 최종 주문 목록,
            "quotes": 현재 주가 정보
        }
        """
        buy_orders = [
            d for d in context.get("final_orders", [])
            if d.get("action") in ("BUY", "BUY_MORE")
        ]
        all_chunk_results: list[dict[str, Any]] = []

        quote_map = {q["symbol"]: q for q in context.get("quotes", [])}

        for order in buy_orders:
            symbol = order["symbol"]
            quantity = int(order["quantity"])
            price = int(order.get("price", 0))
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            # Pre-flight check: skip near upper limit
            quote = quote_map.get(symbol)
            if quote and quote["change_rate"] >= 29.5:
                logger.warning(f"[매수실행가] {symbol} 상한가 근접 - 매수 스킵")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "SKIPPED",
                    "reason": "상한가 근접",
                    "quantity": quantity,
                })
                continue

            # Use SPLIT if quantity > 5 or explicitly requested
            effective_order_type = "SPLIT" if (quantity > 5 or order_type == "SPLIT") else "SINGLE"

            try:
                chunks = await self._execute_smart_buy(
                    symbol, quantity, price, effective_order_type, priority
                )
                all_chunk_results.extend(chunks)
                logger.info(f"[매수실행가] {symbol} 스마트 매수 완료: {len(chunks)}개 청크")
            except Exception as e:
                logger.error(f"[매수실행가] {symbol} 스마트 매수 실패: {e}")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "reason": str(e),
                    "quantity": quantity,
                })

        # Consolidate results per symbol for reporting
        consolidated: dict[str, dict[str, Any]] = {}
        for r in all_chunk_results:
            sym = r["symbol"]
            if sym not in consolidated:
                consolidated[sym] = {
                    "symbol": sym,
                    "status": r.get("status", "UNKNOWN"),
                    "quantity": 0,
                    "price": r.get("price", 0),
                    "order_id": r.get("order_id", ""),
                    "chunks": [],
                }
            consolidated[sym]["quantity"] += r.get("quantity", 0)
            consolidated[sym]["chunks"].append(r)
            # Escalate status: if any chunk failed, mark as PARTIAL
            if r.get("status") == "FAILED" and consolidated[sym]["status"] not in ("FAILED",):
                consolidated[sym]["status"] = "PARTIAL"

        buy_results = list(consolidated.values())
        return {"buy_results": buy_results, "total": len(buy_results)}
