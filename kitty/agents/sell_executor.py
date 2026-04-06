"""매도 실행 에이전트 - 스마트 주문 실행"""
import asyncio
import math
from typing import Any

from kitty.broker import KISBroker, OrderResult
from kitty.utils import logger

from .base import BaseAgent

# 재시도해도 해결되지 않는 에러 키워드 — 즉시 포기
_NON_RETRYABLE = ("장종료", "매매불가", "거래정지", "상장폐지", "주문불가")

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
        name: str = "",
    ) -> list[dict[str, Any]]:
        """
        HIGH priority (stop-loss): always immediate market order, no split
        NORMAL priority:
        - SPLIT: divide into chunks, try limit at current price, fallback to market
        - SINGLE: limit order slightly above market, fallback to market after 8s
        """
        chunk_results: list[dict[str, Any]] = []

        _label = f"{name}({symbol})" if name else symbol

        # HIGH priority: immediate market order, no split
        if priority == "HIGH":
            logger.info(f"[매도실행가] {_label} 긴급 손절 — 시장가 즉시 매도 {quantity}주")
            try:
                order: OrderResult = await self.broker.sell(symbol, quantity, 0, name)
                chunk_results.append({
                    "symbol": symbol,
                    "order_id": order.order_id,
                    "status": "SUBMITTED",
                    "quantity": quantity,
                    "price": 0,
                    "chunk": 1,
                })
            except Exception as e:
                err_msg = str(e) or "KIS 빈 응답"
                logger.error(f"[매도실행가] {_label} 긴급 매도 실패: {err_msg}")
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": quantity,
                    "reason": err_msg,
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

        # Determine limit price for SINGLE: slightly below market to improve fill
        limit_price = price
        if limit_price == 0 and not use_split:
            try:
                quote = await self.broker.get_quote(symbol)
                # 매도 시 현재가 대비 -0.2%로 지정 → 체결 확률 향상
                limit_price = round(quote.current_price * 0.998)
            except Exception as e:
                logger.warning(f"[매도실행가] {_label} 현재가 조회 실패, 시장가 사용: {e}")
                limit_price = 0

        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{_label} 청크{i + 1}/{len(chunks)} ({chunk_qty}주)"

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
                        order = await self.broker.sell(symbol, chunk_qty, chunk_price, name)
                        order_id = order.order_id

                        # Wait 5 seconds for fill (8초→5초)
                        await asyncio.sleep(5)

                        # Check fill status
                        try:
                            status = await self.broker.get_order_status(order_id)
                            filled_qty = status.get("filled_qty", 0)
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
                            elif filled_qty > 0:
                                # Partially filled — record filled portion, retry remainder
                                logger.info(
                                    f"[매도실행가] {chunk_label} 부분 체결({filled_qty}주) — 잔여 {remaining_qty}주 시장가"
                                )
                                chunk_results.append({
                                    "symbol": symbol,
                                    "order_id": order_id,
                                    "status": "FILLED",
                                    "quantity": filled_qty,
                                    "price": chunk_price,
                                    "chunk": i + 1,
                                })
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(1)
                                chunk_qty = remaining_qty
                                chunk_price = 0
                            else:
                                logger.info(
                                    f"[매도실행가] {chunk_label} 미체결 — 취소 후 시장가 재시도"
                                )
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(1)
                                chunk_price = 0
                        except Exception as e:
                            logger.warning(f"[매도실행가] {chunk_label} 체결 조회 실패: {e}")
                            chunk_price = 0
                    else:
                        # Fallback: market order
                        logger.info(f"[매도실행가] {chunk_label} 시장가 매도 시도 (attempt {attempt + 1})")
                        order = await self.broker.sell(symbol, chunk_qty, 0, name)
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
                    err_msg = str(e)
                    non_retryable = any(k in err_msg for k in _NON_RETRYABLE)
                    if non_retryable:
                        logger.warning(f"[매도실행가] {chunk_label} 재시도 불가 에러 — 즉시 중단: {err_msg}")
                    else:
                        logger.error(f"[매도실행가] {chunk_label} attempt {attempt + 1} 실패: {err_msg or '(빈 응답)'}")
                    if attempt == 2 or non_retryable:
                        chunk_results.append({
                            "symbol": symbol,
                            "status": "FAILED",
                            "quantity": chunk_qty,
                            "reason": err_msg or "KIS 빈 응답",
                            "chunk": i + 1,
                        })
                        break

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
            quantity = int(order["quantity"])
            price = int(order.get("price", 0))
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            # Lower limit check
            quote = quote_map.get(symbol)
            name = quote["name"] if quote else ""
            _label = f"{name}({symbol})" if name else symbol
            if quote and quote["change_rate"] <= -29.5:
                logger.warning(f"[매도실행가] {_label} 하한가 근접 — 시장가 즉시 매도 강행")
                price = 0
                priority = "HIGH"

            # Use SPLIT if quantity > 5 or explicitly requested (and not HIGH priority)
            if priority != "HIGH":
                effective_order_type = "SPLIT" if (quantity > 5 or order_type == "SPLIT") else "SINGLE"
            else:
                effective_order_type = "SINGLE"

            try:
                chunks = await self._execute_smart_sell(
                    symbol, quantity, price, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                logger.info(f"[매도실행가] {_label} 스마트 매도 완료: {len(chunks)}개 청크")
            except Exception as e:
                logger.error(f"[매도실행가] {_label} 스마트 매도 실패: {e}")
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
                    "name": quote_map.get(sym, {}).get("name", ""),
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
