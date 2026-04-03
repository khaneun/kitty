"""Night mode buy executor — smart order execution for US stocks"""
import asyncio
import math
from typing import Any

from kitty_night.broker.kis_overseas import KISOverseasBroker
from kitty_night.utils import logger

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are a US stock buy execution specialist.

Role:
- Execute buy orders from the Asset Manager
- Analyze order book to determine optimal buy timing and price
- Decide whether split buying is needed
- Report execution results

Principles:
- Do not buy stocks that are halted or circuit-breaker triggered
- Skip stocks with volume < 50% of average
- Approach stocks up > +10% intraday with caution
"""


class NightBuyExecutorAgent(NightBaseAgent):
    def __init__(self, broker: KISOverseasBroker) -> None:
        super().__init__(name="NightBuyExecutor", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_buy(
        self,
        symbol: str,
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
                        order = await self.broker.buy(symbol, chunk_qty, order_price)
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
                                await self.broker.cancel_order(order_id)
                                await asyncio.sleep(3)
                                order_price = 0
                        except Exception as e:
                            logger.warning(f"[Night:BuyExecutor] {chunk_label} status check failed: {e}")
                            order_price = 0
                    else:
                        logger.info(f"[Night:BuyExecutor] {chunk_label} market buy attempt {attempt + 1}")
                        order = await self.broker.buy(symbol, chunk_qty, 0)
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

        for order in buy_orders:
            symbol = order["symbol"]
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

            effective_order_type = "SPLIT" if (quantity > 10 or order_type == "SPLIT") else "SINGLE"

            try:
                chunks = await self._execute_smart_buy(
                    symbol, quantity, price, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                logger.info(f"[Night:BuyExecutor] {_label} smart buy done: {len(chunks)} chunks")
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
