"""매수 실행 에이전트 - 스마트 주문 실행"""
import asyncio
import math
from typing import Any

from kitty.broker import KISBroker, OrderResult
from kitty.utils import logger

from .base import BaseAgent

# 재시도해도 해결되지 않는 에러 키워드 — 즉시 포기
_NON_RETRYABLE = ("장종료", "매매불가", "거래정지", "상장폐지", "주문불가")


def _round_to_tick(price: int) -> int:
    """KR 주식 호가 단위에 맞게 내림 처리 (KIS 규정)"""
    if price < 2_000:
        tick = 1
    elif price < 5_000:
        tick = 5
    elif price < 20_000:
        tick = 10
    elif price < 50_000:
        tick = 50
    elif price < 200_000:
        tick = 100
    elif price < 500_000:
        tick = 500
    else:
        tick = 1_000
    return (price // tick) * tick

SYSTEM_PROMPT = """당신은 한국 주식 자동매매 시스템의 매수 실행 전문가입니다.

━━━ 임무 ━━━
매수 주문을 최적의 가격으로, 시장 충격을 최소화하며 실행합니다.
무엇을 사는지는 자산운용가가 결정합니다 — 당신의 역할은 효율적 실행과 정확한 보고입니다.

━━━ 실행 전략 ━━━

지정가 우선 (기본):
  1. 현재가 대비 +0.3% 지정가 주문
  2. 5초 대기 후 체결 확인
  3. 체결 완료 → FILLED 보고
  4. 미체결 → 취소 후 1초 대기, 시장가 재시도

시장가 대체:
  - 지정가 미체결 시 사용
  - 가격 정보 없을 때 사용
  - 최대 3회 재시도

분할 매수 (수량 > 5주):
  - 2~3개 청크로 분할
  - 각 청크 독립 실행
  - 대량 주문의 시장 충격 완화

━━━ 사전 검증 (코드 수준에서 처리) ━━━
- 상한가 근접(+29.5%): 매수 스킵
- 가용현금 검증: 부족 시 수량 자동 축소 또는 스킵
- 재시도 불가 에러(장종료, 거래정지 등): 즉시 중단

━━━ 보고 ━━━
각 결과: symbol, order_id, status, quantity, price, chunk 번호.
상태값: FILLED(체결), SUBMITTED(접수), SKIPPED(사전검증 탈락), FAILED(오류).
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
        name: str = "",
    ) -> list[dict[str, Any]]:
        """
        SPLIT: divide quantity into chunks of max 3, try limit order then market fallback
        SINGLE: direct order

        For each chunk:
        1. Place limit order at current_price + 0.3% (체결률 향상)
        2. Wait 5 seconds
        3. Check fill via get_order_status()
        4. If not filled: cancel, wait 1s, retry with market order
        5. Max 3 total attempts
        """
        chunk_results: list[dict[str, Any]] = []

        # 수량 0 방어
        if quantity <= 0:
            _slabel = f"{name}({symbol})" if name else symbol
            logger.warning(f"[매수실행가] {_slabel} 주문 수량 0 — 스킵")
            return [{"symbol": symbol, "status": "SKIPPED", "reason": "주문수량 0", "quantity": 0, "chunk": 1}]

        # Determine order price — 체결 확률 높이기 위해 현재가 대비 +0.3% 설정
        order_price = price
        if order_price == 0:
            try:
                quote = await self.broker.get_quote(symbol)
                # 매수 시 약간 높은 가격으로 지정가 → 체결률 향상 (호가 단위 맞춤)
                order_price = _round_to_tick(round(quote.current_price * 1.003))
            except Exception as e:
                _slabel = f"{name}({symbol})" if name else symbol
                logger.warning(f"[매수실행가] {_slabel} 현재가 조회 실패, 시장가 사용: {e}")
                order_price = 0
        elif order_price > 0:
            order_price = _round_to_tick(order_price)

        use_split = order_type == "SPLIT" or quantity > 5

        if use_split:
            # Divide into chunks of up to 3
            num_chunks = min(3, math.ceil(quantity / max(1, quantity // 3)))
            base_qty = quantity // num_chunks
            remainder = quantity % num_chunks
            chunks = [base_qty + (1 if i < remainder else 0) for i in range(num_chunks)]
        else:
            chunks = [quantity]

        _label = f"{name}({symbol})" if name else symbol
        for i, chunk_qty in enumerate(chunks):
            chunk_label = f"{_label} 청크{i + 1}/{len(chunks)} ({chunk_qty}주)"
            success = False

            for attempt in range(3):
                try:
                    if attempt == 0 and order_price > 0:
                        # First attempt: limit order at slightly above current price
                        logger.info(f"[매수실행가] {chunk_label} 지정가 매수 시도 @ {order_price:,}원")
                        order: OrderResult = await self.broker.buy(symbol, chunk_qty, order_price, name)
                        order_id = order.order_id

                        # Wait 5 seconds for fill (8초→5초: 모멘텀 손실 방지)
                        await asyncio.sleep(5)

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
                            elif filled_qty > 0:
                                # Partially filled — record filled portion, retry remainder
                                logger.info(
                                    f"[매수실행가] {chunk_label} 부분 체결({filled_qty}주) — 잔여 {remaining_qty}주 취소 후 시장가"
                                )
                                chunk_results.append({
                                    "symbol": symbol,
                                    "order_id": order_id,
                                    "status": "FILLED",
                                    "quantity": filled_qty,
                                    "price": order_price,
                                    "chunk": i + 1,
                                })
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(1)
                                chunk_qty = remaining_qty
                                order_price = 0
                            else:
                                # Not filled — cancel and retry market
                                logger.info(
                                    f"[매수실행가] {chunk_label} 미체결 — 취소 후 시장가 재시도"
                                )
                                await self.broker.cancel_order(order_id, symbol, remaining_qty)
                                await asyncio.sleep(1)
                                order_price = 0
                        except Exception as e:
                            logger.warning(f"[매수실행가] {chunk_label} 체결 조회 실패: {e}")
                            order_price = 0
                    else:
                        # Fallback: market order
                        logger.info(f"[매수실행가] {chunk_label} 시장가 매수 시도 (attempt {attempt + 1})")
                        order = await self.broker.buy(symbol, chunk_qty, 0, name)
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
                    err_msg = str(e)
                    non_retryable = any(k in err_msg for k in _NON_RETRYABLE)
                    if non_retryable:
                        logger.warning(f"[매수실행가] {chunk_label} 재시도 불가 에러 — 즉시 중단: {err_msg}")
                    else:
                        logger.error(f"[매수실행가] {chunk_label} attempt {attempt + 1} 실패: {err_msg or '(빈 응답)'}")
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
            "quotes": 현재 주가 정보
        }
        """
        buy_orders = [
            d for d in context.get("final_orders", [])
            if d.get("action") in ("BUY", "BUY_MORE")
        ]
        all_chunk_results: list[dict[str, Any]] = []

        quote_map = {q["symbol"]: q for q in context.get("quotes", [])}
        remaining_cash = context.get("available_cash", float("inf"))

        for order in buy_orders:
            symbol = order["symbol"]
            quantity = int(order["quantity"])
            price = int(order.get("price", 0))
            order_type = order.get("order_type", "SINGLE")
            priority = order.get("priority", "NORMAL")

            # quote/name/label은 사전 체크 전에 먼저 설정
            quote = quote_map.get(symbol)
            name = quote["name"] if quote else ""
            _label = f"{name}({symbol})" if name else symbol

            # Pre-flight check: 수량 0 즉시 스킵
            if quantity <= 0:
                logger.warning(f"[매수실행가] {_label} 주문 수량 0 — 스킵 (자산운용가 오류)")
                all_chunk_results.append({"symbol": symbol, "status": "SKIPPED", "reason": "주문수량 0", "quantity": 0})
                continue

            # Pre-flight check: 상한가 근접 스킵
            if quote and quote["change_rate"] >= 29.5:
                logger.warning(f"[매수실행가] {_label} 상한가 근접 - 매수 스킵")
                all_chunk_results.append({
                    "symbol": symbol,
                    "status": "SKIPPED",
                    "reason": "상한가 근접",
                    "quantity": quantity,
                })
                continue

            # 가용현금 사전 체크 — 주문 예상 금액이 잔여 현금 초과 시 스킵
            est_price = quote.get("current_price", 0) if quote else 0
            if est_price > 0 and remaining_cash != float("inf"):
                est_amount = est_price * quantity
                if est_amount > remaining_cash:
                    # 수량 축소 시도
                    quantity = int(remaining_cash // est_price)
                    if quantity <= 0:
                        logger.warning(f"[매수실행가] {_label} 가용현금 부족 — 매수 스킵")
                        all_chunk_results.append({
                            "symbol": symbol,
                            "status": "SKIPPED",
                            "reason": "가용현금 부족",
                            "quantity": 0,
                        })
                        continue
                    logger.info(f"[매수실행가] {_label} 현금한도 적용 → {quantity}주로 축소")
                remaining_cash -= est_price * quantity

            # Use SPLIT if quantity > 5 or explicitly requested
            effective_order_type = "SPLIT" if (quantity > 5 or order_type == "SPLIT") else "SINGLE"

            try:
                chunks = await self._execute_smart_buy(
                    symbol, quantity, price, effective_order_type, priority, name
                )
                all_chunk_results.extend(chunks)
                logger.info(f"[매수실행가] {_label} 스마트 매수 완료: {len(chunks)}개 청크")
            except Exception as e:
                logger.error(f"[매수실행가] {_label} 스마트 매수 실패: {e}")
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
            # Escalate status: if any chunk failed, mark as PARTIAL
            if r.get("status") == "FAILED" and consolidated[sym]["status"] not in ("FAILED",):
                consolidated[sym]["status"] = "PARTIAL"

        buy_results = list(consolidated.values())
        return {"buy_results": buy_results, "total": len(buy_results)}
