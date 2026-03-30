"""매도 실행 에이전트 - 스마트 주문 실행"""
import asyncio
import math
from typing import Any

from kitty.broker import KISBroker, OrderResult
from kitty.utils import logger

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 주식 매도 전문가입니다.

역할:
- 자산운용가의 매도 지시를 실행합니다
- 손절 조건(stop-loss) 달성 시 즉시 매도를 실행합니다
- 목표가(take-profit) 도달 시 익절합니다
- 분할 매도가 유리한 경우 나눠서 매도합니다

원칙:
- 하한가 종목은 다음날 매도를 고려합니다
- 손절은 감정 없이 기계적으로 실행합니다
- 거래량 없는 종목은 호가 조정 후 매도합니다
"""


class SellExecutorAgent(BaseAgent):
    def __init__(self, broker: KISBroker) -> None:
        super().__init__(name="매도실행가", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_sell(
        self,
        symbol: str,
        quantity: int,
        price: int,
        order_type: str,
        priority: str,
    ) -> list[dict[str, Any]]:
        """
        HIGH priority (stop-loss): always immediate market order, no split
        NORMAL priority:
        - SPLIT: divide into chunks, try limit at current price, fallback to market
        - SINGLE: limit order slightly above market, fallback to market after 8s
        """
        chunk_results: list[dict[str, Any]] = []

        # HIGH priority: immediate market order, no split
        if priority == "HIGH":
            logger.info(f"[매도실행가] {symbol} 긴급 손절 — 시장가 즉시 매도 {quantity}주")
            try:
                order: OrderResult = await self.broker.sell(symbol, quantity, 0)
                chunk_results.append({
                    "symbol": symbol,
                    "order_id": order.order_id,
                    "status": "SUBMITTED",
                    "quantity": quantity,
                    "price": 0,
                    "chunk": 1,
                })
            except Exception as e:
                logger.error(f"[매도실행가] {symbol} 긴급 매도 실패: {e}")
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": quantity,
                    "reason": str(e),
                    "chunk": 1,
                })
            return chunk_results

        # NORMAL priority: determine chunks
        use_split = order_type == "SPLIT" or quantity > 5

        if use_split:
            num_chunks = min(3, math.ceil(quantity / max(1, quantity // 3)))
            base_qty = quantity // num_chunks
            remainder = quantity % num_chunks
            chunks = [base_qty + (1 if i < remainder else 0) for i in range(num_chunks)]
        else:
            chunks = [quantity]

        # Determine limit price for SINGLE: slightly above market
        limit_price = price
        if limit_price == 0 and not use_split:
            try:
                quote = await self.broker.get_quote(symbol)
                # Offer slightly above current price to improve fill odds
                limit_price = int(quote.current_price * 1.002)
            except Exception as e:
                logger.warning(f"[매도실행가] {symbol} 현재가 조회 실패, 시장가 사용: {e}")
                limit_price = 0

        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{symbol} 청크{i + 1}/{len(chunks)} ({chunk_qty}주)"

            # For split chunks, always use current price
            if use_split:
                try:
                    quote = await self.broker.get_quote(symbol)
                    chunk_price = quote.current_price
                except Exception:
                    chunk_price = 0
            else:
                chunk_price = limit_price

            success = False

            for attempt in range(3):
                try:
                    if attempt == 0 and chunk_price > 0:
                        # First attempt: limit order
                        logger.info(f"[매도실행가] {chunk_label} 지정가 매도 시도 @ {chunk_price:,}원")
                        order = await self.broker.sell(symbol, chunk_qty, chunk_price)
                        order_id = order.order_id

                        # Wait 8 seconds for fill
                        await asyncio.sleep(8)

                        # Check fill status
                        try:
                            status = await self.broker.get_order_status(order_id)
                            remaining_qty = status.get("remaining_qty", chunk_qty)

                            if remaining_qty == 0:
                                logger.info(f"[매도실행가] {chunk_label} 지정가 체결 완료")
                                chunk_results.append({
                                    "symbol": symbol,
                                    "order_id": order_id,
                                    "status": "FILLED",
                                    "quantity": chunk_qty,
                                    "price": chunk_price,
                                    "chunk": i + 1,
                                })
                                success = True
                                break
                            else:
                                logger.info(
                                    f"[매도실행가] {chunk_label} 미체결({remaining_qty}주 잔여) — 취소 후 시장가 재시도"
                                )
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(2)
                                chunk_price = 0
                        except Exception as e:
                            logger.warning(f"[매도실행가] {chunk_label} 체결 조회 실패: {e}")
                            chunk_price = 0
                    else:
                        # Fallback: market order
                        logger.info(f"[매도실행가] {chunk_label} 시장가 매도 시도 (attempt {attempt + 1})")
                        order = await self.broker.sell(symbol, chunk_qty, 0)
                        logger.info(f"[매도실행가] {chunk_label} 시장가 매도 주문 완료")
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
                    logger.error(f"[매도실행가] {chunk_label} attempt {attempt + 1} 실패: {e}")
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
            "portfolio": 현재 보유 종목,
            "quotes": 현재 주가 정보
        }
        """
        sell_orders = [
            d for d in context.get("final_orders", [])
            if d.get("action") in ("SELL", "PARTIAL_SELL")
        ]
        all_chunk_results: list[dict[str, Any]] = []

        quote_map = {q["symbol"]: q for q in context.get("quotes", [])}

        for order in sell_orders:
            symbol = order["symbol"]
            quantity = order["quantity"]
            price = order.get("price", 0)
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            # Lower limit check
            quote = quote_map.get(symbol)
            if quote and quote["change_rate"] <= -29.5:
                logger.warning(f"[매도실행가] {symbol} 하한가 근접 — 시장가 즉시 매도 강행")
                price = 0
                priority = "HIGH"

            # Use SPLIT if quantity > 5 or explicitly requested (and not HIGH priority)
            if priority != "HIGH":
                effective_order_type = "SPLIT" if (quantity > 5 or order_type == "SPLIT") else "SINGLE"
            else:
                effective_order_type = "SINGLE"

            try:
                chunks = await self._execute_smart_sell(
                    symbol, quantity, price, effective_order_type, priority
                )
                all_chunk_results.extend(chunks)
                logger.info(f"[매도실행가] {symbol} 스마트 매도 완료: {len(chunks)}개 청크")
            except Exception as e:
                logger.error(f"[매도실행가] {symbol} 스마트 매도 실패: {e}")
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
            if r.get("status") == "FAILED" and consolidated[sym]["status"] not in ("FAILED",):
                consolidated[sym]["status"] = "PARTIAL"

        sell_results = list(consolidated.values())
        return {"sell_results": sell_results, "total": len(sell_results)}
