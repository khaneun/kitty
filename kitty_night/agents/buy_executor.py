"""Night mode buy executor — smart order execution for US stocks"""
import asyncio
import math
from typing import Any

from kitty_night.broker.kis_overseas import KISOverseasBroker
from kitty_night.utils import logger

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Buy Execution Specialist for a US stock automated trading system.

━━━ MISSION ━━━
Execute buy orders with optimal price and minimal market impact.
You do NOT decide what to buy — the Asset Manager already decided that.
Your job is to execute efficiently and report results accurately.

━━━ EXECUTION STRATEGY ━━━

Limit Order First (default):
  1. Place limit order at current market price
  2. Wait 10 seconds for fill
  3. If filled → report FILLED with actual price
  4. If not filled → cancel, wait 3s, retry as market order

Market Order Fallback:
  - Used when limit order times out
  - Used when price = 0 (no reference price available)
  - Max 3 attempts per chunk

Split Execution (for quantity > 10):
  - Divide into 2-3 equal chunks
  - Execute each chunk independently
  - Reduces market impact on larger orders

━━━ PRE-FLIGHT CHECKS (handled by code, not AI) ━━━
- Circuit breaker proximity: skip if stock up ≥ 15%
- Cash validation: skip if insufficient funds
- Quantity adjustment: reduce quantity if cash only covers partial order

━━━ REPORTING ━━━
Each execution result includes: symbol, order_id, status, quantity, price, chunk number.
Status values: FILLED (confirmed), SUBMITTED (sent), SKIPPED (pre-flight fail), FAILED (error).
"""


class NightBuyExecutorAgent(NightBaseAgent):
    def __init__(self, broker: KISOverseasBroker) -> None:
        super().__init__(name="NightBuyExecutor", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_buy(
        self,
        symbol: str,
        excd: str,
        quantity: int,
        price: float,
        order_type: str,
        priority: str,
        name: str = "",
    ) -> list[dict[str, Any]]:
        """
        SPLIT: divide into chunks of max 5, try limit then market fallback
        SINGLE: direct order

        For each chunk:
        1. Place limit order at current price
        2. Wait 10 seconds (US market needs more time)
        3. Check fill
        4. If not filled: cancel, wait 3s, retry market order
        5. Max 3 attempts
        """
        chunk_results: list[dict[str, Any]] = []
        _label = f"{name}({symbol})" if name else symbol

        order_price = price
        if order_price == 0:
            try:
                quote = await self.broker.get_quote(symbol)
                order_price = quote.current_price
            except Exception as e:
                logger.warning(f"[Night:BuyExecutor] {_label} quote fetch failed, using market order: {e}")
                order_price = 0

        use_split = order_type == "SPLIT" or quantity > 10

        if use_split:
            num_chunks = min(3, math.ceil(quantity / max(1, quantity // 3)))
            base_qty = quantity // num_chunks
            remainder = quantity % num_chunks
            chunks = [base_qty + (1 if i < remainder else 0) for i in range(num_chunks)]
        else:
            chunks = [quantity]

        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{_label} chunk {i + 1}/{len(chunks)} ({chunk_qty} shares)"
            success = False

            for attempt in range(3):
                try:
                    if attempt == 0 and order_price > 0:
                        logger.info(f"[Night:BuyExecutor] {chunk_label} limit buy @ ${order_price:,.2f}")
                        order = await self.broker.buy(symbol, excd, chunk_qty, order_price)
                        order_id = order.order_id

                        await asyncio.sleep(10)

                        try:
                            status = await self.broker.get_order_status(order_id)
                            remaining_qty = status.get("remaining_qty", chunk_qty)

                            if remaining_qty == 0:
                                logger.info(f"[Night:BuyExecutor] {chunk_label} limit order filled")
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
                                logger.info(
                                    f"[Night:BuyExecutor] {chunk_label} unfilled ({remaining_qty} remaining) — cancel & retry market"
                                )
                                await self.broker.cancel_order(order_id, excd, symbol, chunk_qty)
                                await asyncio.sleep(3)
                                order_price = 0
                        except Exception as e:
                            logger.warning(f"[Night:BuyExecutor] {chunk_label} status check failed: {e}")
                            order_price = 0
                    else:
                        logger.info(f"[Night:BuyExecutor] {chunk_label} market buy attempt {attempt + 1}")
                        order = await self.broker.buy(symbol, excd, chunk_qty, 0)
                        logger.info(f"[Night:BuyExecutor] {chunk_label} market order submitted")
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
                    logger.error(f"[Night:BuyExecutor] {chunk_label} attempt {attempt + 1} failed: {e}")
                    if attempt == 2:
                        chunk_results.append({
                            "symbol": symbol,
                            "status": "FAILED",
                            "quantity": chunk_qty,
                            "reason": str(e),
                            "chunk": i + 1,
                        })

            if not success and not any(r.get("chunk") == i + 1 for r in chunk_results):
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": chunk_qty,
                    "reason": "Max retries exceeded",
                    "chunk": i + 1,
                })

        return chunk_results

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        buy_orders = [
            d for d in context.get("final_orders", [])
            if d.get("action") in ("BUY", "BUY_MORE")
        ]
        all_chunk_results: list[dict[str, Any]] = []
        quote_map = {q["symbol"]: q for q in context.get("quotes", [])}

        # 매수 전 실제 주문 가능 금액 조회
        try:
            remaining_cash = await self.broker.get_available_usd()
            logger.info(f"[Night:BuyExecutor] available cash before buys: ${remaining_cash:,.2f}")
        except Exception as e:
            logger.warning(f"[Night:BuyExecutor] available cash query failed, using context: {e}")
            remaining_cash = float(context.get("available_cash_usd", 0))

        for order in buy_orders:
            symbol = order["symbol"]
            excd = order.get("excd", "NAS")
            quantity = int(order["quantity"])
            price = float(order.get("price", 0))
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            quote = quote_map.get(symbol)
            name = order.get("name", "") or (quote.get("name", "") if quote else "")
            _label = f"{name}({symbol})" if name else symbol

            # Pre-flight: skip if up > 15% (US circuit breaker proximity)
            if quote and quote.get("change_rate", 0) >= 15.0:
                logger.warning(f"[Night:BuyExecutor] {_label} up {quote['change_rate']:.1f}% — skip buy")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "SKIPPED",
                    "reason": f"Up {quote['change_rate']:.1f}% — circuit breaker proximity",
                    "quantity": quantity,
                })
                continue

            # Pre-flight: 주문 가능 금액 검증
            est_price = price
            if est_price == 0 and quote:
                est_price = float(quote.get("current_price", 0))
            est_cost = est_price * quantity * 1.01 if est_price > 0 else 0  # 1% 여유분 포함
            if est_cost > 0 and remaining_cash < est_cost:
                # 가능한 만큼만 수량 조정
                if est_price > 0:
                    affordable_qty = int(remaining_cash / (est_price * 1.01))
                else:
                    affordable_qty = 0
                if affordable_qty <= 0:
                    logger.info(
                        f"[Night:BuyExecutor] {_label} 잔고 부족 — "
                        f"필요 ${est_cost:,.2f} > 가용 ${remaining_cash:,.2f}, skip"
                    )
                    all_chunk_results.append({
                        "symbol": symbol,
                        "status": "SKIPPED",
                        "reason": f"Insufficient cash: need ${est_cost:,.2f}, available ${remaining_cash:,.2f}",
                        "quantity": quantity,
                    })
                    continue
                logger.info(
                    f"[Night:BuyExecutor] {_label} 잔고에 맞게 수량 조정: "
                    f"{quantity} → {affordable_qty}주 (가용 ${remaining_cash:,.2f})"
                )
                quantity = affordable_qty
                est_cost = est_price * quantity * 1.01

            effective_order_type = "SPLIT" if (quantity > 10 or order_type == "SPLIT") else "SINGLE"

            try:
                chunks = await self._execute_smart_buy(
                    symbol, excd, quantity, price, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                # 성공한 주문 금액만큼 잔고 차감
                for c in chunks:
                    if c.get("status") in ("FILLED", "SUBMITTED"):
                        c_price = c.get("price", 0) or est_price
                        remaining_cash -= c_price * c.get("quantity", 0)
                        remaining_cash = max(0, remaining_cash)
                logger.info(
                    f"[Night:BuyExecutor] {_label} smart buy done: {len(chunks)} chunks "
                    f"(remaining cash ≈ ${remaining_cash:,.2f})"
                )
            except Exception as e:
                logger.error(f"[Night:BuyExecutor] {_label} smart buy failed: {e}")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "reason": str(e),
                    "quantity": quantity,
                })

        # Consolidate per symbol
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

        buy_results = list(consolidated.values())
        return {"buy_results": buy_results, "total": len(buy_results)}
