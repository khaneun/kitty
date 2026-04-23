"""Night mode sell executor — all limit orders, no market orders"""
import asyncio
import math
from typing import Any

from kitty_night.broker.kis_overseas import KISOverseasBroker
from kitty_night.utils import logger

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Sell Execution Specialist for a US stock automated trading system.

━━━ MISSION ━━━
Execute sell orders using limit orders only. No market orders.
Your job is to execute efficiently and report results accurately.

━━━ EXECUTION STRATEGY ━━━

HIGH Priority (stop-loss / emergency) — aggressive limit:
  Attempt 1: current_price × 0.995  (-0.5%)
  Attempt 2: current_price × 0.990  (-1.0%)
  Attempt 3: current_price × 0.980  (-2.0%)
  No splitting. Each attempt: place limit → wait 10s → check fill → cancel if unfilled → retry.

NORMAL Priority (take-profit / rotation) — standard limit:
  Attempt 1: current_price × 1.000  (spot)
  Attempt 2: current_price × 0.998  (-0.2%)
  Attempt 3: current_price × 0.995  (-0.5%)
  Split into 2-3 chunks for quantity > 10 shares. Fresh quote per chunk.

If all 3 attempts are unfilled → FAILED (do NOT fall back to market order).

━━━ REPORTING ━━━
Status: FILLED (confirmed fill), FAILED (all attempts exhausted or error).
price: the actual limit price the order was placed at (NOT 0).
"""

# Price adjustment multipliers per attempt
_SELL_HIGH_ADJ  = [0.995, 0.990, 0.980]   # stop-loss: step down aggressively
_SELL_NORMAL_ADJ = [1.000, 0.998, 0.995]   # take-profit: gentle step down


class NightSellExecutorAgent(NightBaseAgent):
    def __init__(self, broker: KISOverseasBroker) -> None:
        super().__init__(name="NightSellExecutor", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_sell(
        self,
        symbol: str,
        excd: str,
        quantity: int,
        order_type: str,
        priority: str,
        name: str = "",
    ) -> list[dict[str, Any]]:
        """All limit orders. Unfilled → cancel → retry at adjusted price (max 3 attempts)."""
        chunk_results: list[dict[str, Any]] = []
        _label = f"{name}({symbol})" if name else symbol

        price_adj = _SELL_HIGH_ADJ if priority == "HIGH" else _SELL_NORMAL_ADJ

        # HIGH priority: single order, no split
        if priority == "HIGH":
            try:
                quote = await self.broker.get_quote(symbol)
                base_price = quote.current_price
            except Exception as e:
                logger.error(f"[Night:SellExecutor] {_label} HIGH quote failed: {e}")
                return [{
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": quantity,
                    "reason": f"Quote fetch failed: {e}",
                    "chunk": 1,
                }]

            success = False
            for attempt in range(3):
                order_price = round(base_price * price_adj[attempt], 2)
                try:
                    logger.info(
                        f"[Night:SellExecutor] {_label} HIGH limit sell @ ${order_price:,.2f} "
                        f"(attempt {attempt + 1}, adj {price_adj[attempt]:.3f})"
                    )
                    order = await self.broker.sell(symbol, excd, quantity, order_price)
                    order_id = order.order_id

                    await asyncio.sleep(10)

                    try:
                        status = await self.broker.get_order_status(order_id)
                        remaining_qty = status.get("remaining_qty", quantity)

                        if remaining_qty == 0:
                            logger.info(
                                f"[Night:SellExecutor] {_label} HIGH FILLED @ ${order_price:,.2f}"
                            )
                            chunk_results.append({
                                "symbol": symbol,
                                "order_id": order_id,
                                "status": "FILLED",
                                "quantity": quantity,
                                "price": order_price,
                                "chunk": 1,
                            })
                            success = True
                            break

                        logger.info(
                            f"[Night:SellExecutor] {_label} HIGH unfilled — cancel & retry lower"
                        )
                        try:
                            await self.broker.cancel_order(order_id, excd, symbol, quantity)
                        except Exception as ce:
                            logger.warning(
                                f"[Night:SellExecutor] {_label} cancel failed: {ce}"
                            )
                        await asyncio.sleep(3)

                    except Exception as se:
                        logger.warning(
                            f"[Night:SellExecutor] {_label} HIGH status check failed: {se} — cancel & retry"
                        )
                        try:
                            await self.broker.cancel_order(order_id, excd, symbol, quantity)
                        except Exception:
                            pass
                        await asyncio.sleep(3)

                except Exception as e:
                    logger.error(
                        f"[Night:SellExecutor] {_label} HIGH attempt {attempt + 1} failed: {e}"
                    )
                    if attempt == 2:
                        chunk_results.append({
                            "symbol": symbol,
                            "status": "FAILED",
                            "quantity": quantity,
                            "reason": str(e),
                            "chunk": 1,
                        })

            if not success and not chunk_results:
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": quantity,
                    "reason": "All 3 HIGH priority limit attempts unfilled",
                    "chunk": 1,
                })
            return chunk_results

        # NORMAL priority: split allowed
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

            # Fresh quote per chunk
            try:
                quote = await self.broker.get_quote(symbol)
                base_price = quote.current_price
            except Exception as e:
                logger.error(f"[Night:SellExecutor] {chunk_label} quote failed: {e}")
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": chunk_qty,
                    "reason": f"Quote fetch failed: {e}",
                    "chunk": i + 1,
                })
                continue

            success = False

            for attempt in range(3):
                order_price = round(base_price * price_adj[attempt], 2)
                try:
                    logger.info(
                        f"[Night:SellExecutor] {chunk_label} limit sell @ ${order_price:,.2f} "
                        f"(attempt {attempt + 1}, adj {price_adj[attempt]:.3f})"
                    )
                    order = await self.broker.sell(symbol, excd, chunk_qty, order_price)
                    order_id = order.order_id

                    await asyncio.sleep(10)

                    try:
                        status = await self.broker.get_order_status(order_id)
                        remaining_qty = status.get("remaining_qty", chunk_qty)

                        if remaining_qty == 0:
                            logger.info(
                                f"[Night:SellExecutor] {chunk_label} FILLED @ ${order_price:,.2f}"
                            )
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

                        logger.info(
                            f"[Night:SellExecutor] {chunk_label} unfilled — cancel & retry lower"
                        )
                        try:
                            await self.broker.cancel_order(order_id, excd, symbol, chunk_qty)
                        except Exception as ce:
                            logger.warning(
                                f"[Night:SellExecutor] {chunk_label} cancel failed: {ce}"
                            )
                        await asyncio.sleep(3)

                    except Exception as se:
                        logger.warning(
                            f"[Night:SellExecutor] {chunk_label} status check failed: {se} — cancel & retry"
                        )
                        try:
                            await self.broker.cancel_order(order_id, excd, symbol, chunk_qty)
                        except Exception:
                            pass
                        await asyncio.sleep(3)

                except Exception as e:
                    logger.error(
                        f"[Night:SellExecutor] {chunk_label} attempt {attempt + 1} failed: {e}"
                    )
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
                    "reason": "All 3 limit attempts unfilled",
                    "chunk": i + 1,
                })

        return chunk_results

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        sell_orders = [
            d for d in context.get("final_orders", [])
            if d.get("action") in ("SELL", "PARTIAL_SELL")
        ]
        all_chunk_results: list[dict[str, Any]] = []
        quote_map = {q["symbol"]: q for q in context.get("quotes", [])}

        # 보유수량 맵 구성 (매도 검증용)
        portfolio = context.get("portfolio", [])
        holding_qty_map: dict[str, int] = {
            p.get("symbol", ""): int(p.get("quantity", 0)) for p in portfolio
        }
        # 동일 종목 복수 매도 주문 시 누적 차감 추적
        sold_qty_map: dict[str, int] = {}

        for order in sell_orders:
            symbol = order["symbol"]
            excd = order.get("excd", "NAS")
            quantity = int(order["quantity"])
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            quote = quote_map.get(symbol)
            name = order.get("name", "") or (quote.get("name", "") if quote else "")
            _label = f"{name}({symbol})" if name else symbol

            # Pre-flight: 보유수량 검증
            held = holding_qty_map.get(symbol, 0)
            already_sold = sold_qty_map.get(symbol, 0)
            available_qty = held - already_sold
            if available_qty <= 0:
                logger.info(
                    f"[Night:SellExecutor] {_label} 보유수량 없음 "
                    f"(보유 {held}, 기매도 {already_sold}), skip"
                )
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "SKIPPED",
                    "reason": f"No available shares: held {held}, already queued {already_sold}",
                    "quantity": quantity,
                })
                continue
            if quantity > available_qty:
                logger.info(
                    f"[Night:SellExecutor] {_label} 매도수량 조정: "
                    f"{quantity} → {available_qty}주 (보유 {held}, 기매도 {already_sold})"
                )
                quantity = available_qty

            # Extreme drop: force HIGH priority (still limit, just more aggressive)
            if quote and quote.get("change_rate", 0) <= -15.0:
                logger.warning(
                    f"[Night:SellExecutor] {_label} down {quote['change_rate']:.1f}% "
                    f"— force HIGH priority sell"
                )
                priority = "HIGH"

            if priority != "HIGH":
                effective_order_type = (
                    "SPLIT" if (quantity > 10 or order_type == "SPLIT") else "SINGLE"
                )
            else:
                effective_order_type = "SINGLE"

            try:
                chunks = await self._execute_smart_sell(
                    symbol, excd, quantity, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                for c in chunks:
                    if c.get("status") == "FILLED":
                        sold_qty_map[symbol] = (
                            sold_qty_map.get(symbol, 0) + c.get("quantity", 0)
                        )
                logger.info(
                    f"[Night:SellExecutor] {_label} smart sell done: {len(chunks)} chunks"
                )
            except Exception as e:
                logger.error(f"[Night:SellExecutor] {_label} smart sell failed: {e}")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "reason": str(e),
                    "quantity": quantity,
                })

        # Consolidate per symbol — weighted average price
        consolidated: dict[str, dict[str, Any]] = {}
        for r in all_chunk_results:
            sym = r["symbol"]
            if sym not in consolidated:
                consolidated[sym] = {
                    "symbol": sym,
                    "name": quote_map.get(sym, {}).get("name", ""),
                    "status": r.get("status", "UNKNOWN"),
                    "quantity": 0,
                    "price": 0,
                    "order_id": r.get("order_id", ""),
                    "chunks": [],
                }
            consolidated[sym]["quantity"] += r.get("quantity", 0)
            consolidated[sym]["chunks"].append(r)
            if r.get("status") == "FAILED" and consolidated[sym]["status"] != "FAILED":
                consolidated[sym]["status"] = "PARTIAL"

        # Compute weighted average exec price per symbol
        for entry in consolidated.values():
            filled = [
                (c.get("quantity", 0), c.get("price", 0))
                for c in entry["chunks"]
                if c.get("status") == "FILLED" and c.get("price", 0) > 0
            ]
            if filled:
                total_qty = sum(q for q, _ in filled)
                total_cost = sum(q * p for q, p in filled)
                entry["price"] = round(total_cost / total_qty, 4) if total_qty > 0 else 0

        sell_results = list(consolidated.values())
        return {"sell_results": sell_results, "total": len(sell_results)}
