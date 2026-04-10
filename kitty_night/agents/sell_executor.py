"""Night mode sell executor — smart order execution for US stocks"""
import asyncio
import math
from typing import Any

from kitty_night.broker.kis_overseas import KISOverseasBroker
from kitty_night.utils import logger

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Sell Execution Specialist for a US stock automated trading system.

━━━ MISSION ━━━
Execute sell orders with speed (for stop-loss) or optimal price (for take-profit).
You do NOT decide what to sell — the Asset Manager already decided that.
Your job is to execute efficiently and report results accurately.

━━━ EXECUTION STRATEGY BY PRIORITY ━━━

HIGH Priority (stop-loss / emergency):
  → Immediate market order, NO splitting, NO waiting
  → Speed > price. Get out NOW. 3 retry attempts.
  → Used for: hard stop, emergency stop, extreme drops

NORMAL Priority (take-profit / rotation):
  → Limit order at current price × 0.998 (slight discount for fill certainty)
  → Wait 10 seconds, check fill
  → If not filled → cancel, retry as market order
  → Split into 2-3 chunks for quantity > 10 shares

━━━ PRE-FLIGHT CHECKS (handled by code, not AI) ━━━
- Holding validation: verify shares are actually held before selling
- Quantity cap: never sell more than actual holding quantity
- Double-sell prevention: track cumulative sold quantity per symbol
- Extreme drop override: stock down ≥ 15% → force HIGH priority market sell

━━━ REPORTING ━━━
Each execution result includes: symbol, order_id, status, quantity, price, chunk number.
Status values: FILLED (confirmed), SUBMITTED (sent), SKIPPED (pre-flight fail), FAILED (error).
"""


class NightSellExecutorAgent(NightBaseAgent):
    def __init__(self, broker: KISOverseasBroker) -> None:
        super().__init__(name="NightSellExecutor", system_prompt=SYSTEM_PROMPT)
        self.broker = broker

    async def _execute_smart_sell(
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
        HIGH priority (stop-loss): immediate market order, no split
        NORMAL priority:
        - SPLIT: divide into chunks, try limit, fallback to market
        - SINGLE: limit order slightly below market, fallback after 10s
        """
        chunk_results: list[dict[str, Any]] = []
        _label = f"{name}({symbol})" if name else symbol

        # HIGH priority: immediate market order (모의투자는 시세 조회 실패 시 재시도)
        if priority == "HIGH":
            logger.info(f"[Night:SellExecutor] {_label} URGENT stop-loss — market sell {quantity} shares")
            last_exc: Exception | None = None
            for _attempt in range(3):
                try:
                    order = await self.broker.sell(symbol, excd, quantity, 0)
                    chunk_results.append({
                        "symbol": symbol,
                        "order_id": order.order_id,
                        "status": "SUBMITTED",
                        "quantity": quantity,
                        "price": 0,
                        "chunk": 1,
                    })
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    if _attempt < 2:
                        logger.warning(
                            f"[Night:SellExecutor] {_label} urgent sell attempt {_attempt + 1} failed: {e} — retrying"
                        )
                        await asyncio.sleep(2.0)
                    else:
                        logger.error(f"[Night:SellExecutor] {_label} urgent sell failed: {e}")
            if last_exc is not None:
                chunk_results.append({
                    "symbol": symbol,
                    "status": "FAILED",
                    "quantity": quantity,
                    "reason": str(last_exc),
                    "chunk": 1,
                })
            return chunk_results

        # NORMAL priority
        use_split = order_type == "SPLIT" or quantity > 10

        if use_split:
            num_chunks = min(3, math.ceil(quantity / max(1, quantity // 3)))
            base_qty = quantity // num_chunks
            remainder = quantity % num_chunks
            chunks = [base_qty + (1 if i < remainder else 0) for i in range(num_chunks)]
        else:
            chunks = [quantity]

        # Determine limit price for SINGLE
        limit_price = price
        if limit_price == 0 and not use_split:
            try:
                quote = await self.broker.get_quote(symbol)
                # Slightly below current price for better fill
                limit_price = round(quote.current_price * 0.998, 2)
            except Exception as e:
                logger.warning(f"[Night:SellExecutor] {_label} quote fetch failed, using market: {e}")
                limit_price = 0

        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{_label} chunk {i + 1}/{len(chunks)} ({chunk_qty} shares)"

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
                        logger.info(f"[Night:SellExecutor] {chunk_label} limit sell @ ${chunk_price:,.2f}")
                        order = await self.broker.sell(symbol, excd, chunk_qty, chunk_price)
                        order_id = order.order_id

                        await asyncio.sleep(10)

                        try:
                            status = await self.broker.get_order_status(order_id)
                            remaining_qty = status.get("remaining_qty", chunk_qty)

                            if remaining_qty == 0:
                                logger.info(f"[Night:SellExecutor] {chunk_label} limit order filled")
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
                                    f"[Night:SellExecutor] {chunk_label} unfilled ({remaining_qty} remaining) — cancel & retry market"
                                )
                                await self.broker.cancel_order(order_id, excd, symbol, chunk_qty)
                                await asyncio.sleep(3)
                                chunk_price = 0
                        except Exception as e:
                            logger.warning(f"[Night:SellExecutor] {chunk_label} status check failed: {e}")
                            chunk_price = 0
                    else:
                        logger.info(f"[Night:SellExecutor] {chunk_label} market sell attempt {attempt + 1}")
                        order = await self.broker.sell(symbol, excd, chunk_qty, 0)
                        logger.info(f"[Night:SellExecutor] {chunk_label} market sell submitted")
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
                    logger.error(f"[Night:SellExecutor] {chunk_label} attempt {attempt + 1} failed: {e}")
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
            price = float(order.get("price", 0))
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
                    f"[Night:SellExecutor] {_label} 보유수량 없음 (보유 {held}, 기매도 {already_sold}), skip"
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

            # Extreme drop: force market sell
            if quote and quote.get("change_rate", 0) <= -15.0:
                logger.warning(f"[Night:SellExecutor] {_label} down {quote['change_rate']:.1f}% — force market sell")
                price = 0
                priority = "HIGH"

            if priority != "HIGH":
                effective_order_type = "SPLIT" if (quantity > 10 or order_type == "SPLIT") else "SINGLE"
            else:
                effective_order_type = "SINGLE"

            try:
                chunks = await self._execute_smart_sell(
                    symbol, excd, quantity, price, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                # 성공한 매도 수량 누적 (동일 종목 중복 매도 방지)
                for c in chunks:
                    if c.get("status") in ("FILLED", "SUBMITTED"):
                        sold_qty_map[symbol] = sold_qty_map.get(symbol, 0) + c.get("quantity", 0)
                logger.info(f"[Night:SellExecutor] {_label} smart sell done: {len(chunks)} chunks")
            except Exception as e:
                logger.error(f"[Night:SellExecutor] {_label} smart sell failed: {e}")
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

        sell_results = list(consolidated.values())
        return {"sell_results": sell_results, "total": len(sell_results)}
