"""자산운용 에이전트 - 최종 실행 가능 주문 목록 결정"""
import json
from typing import Any

from kitty.utils import logger

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식 자동매매 시스템의 포트폴리오 총괄 매니저입니다.
최종 실행 주문 목록을 생성합니다. 모든 주문은 실제 돈으로 즉시 실행됩니다.

━━━ 임무 ━━━
종목평가가의 보유종목 평가 + 종목발굴가의 신규 후보를 종합하여
실행 가능한 최종 주문 목록을 만듭니다. 리스크 관리가 최우선입니다.

━━━ 예산 산술 (주문 결정 전 반드시 먼저 계산) ━━━

1. 총_매수예산 = 가용현금 - (총자산평가액 × 현금유보비율)
   → 모든 매수 주문 합계의 상한선
   → 총_매수예산 ≤ 0이면: 신규 매수 금지 (현금 보전 모드)

2. 각 BUY 주문: 예상금액 = 수량 × 현재가
3. 전체 BUY 예상금액 합계가 총_매수예산을 초과할 수 없음
4. 단일 주문은 1회최대매수금액을 초과할 수 없음
5. 단일 종목(기존 + 신규)은 종목당최대보유금액을 초과할 수 없음

※ summary에 예산 계산 과정을 반드시 포함하세요.

━━━ 주문 우선순위 (이 순서대로 엄격히 처리) ━━━

우선순위 1 — 비상 매도 (priority: HIGH)
  조건: 수익률 ≤ -(손절기준 × 2) 또는 등락률 ≤ -15%
  행동: SELL 100%, order_type: SINGLE

우선순위 2 — 하드 손절 (priority: HIGH)
  조건: 평가가가 손절 사유로 SELL/PARTIAL_SELL 추천
  행동: PARTIAL_SELL 50%, order_type: SINGLE

우선순위 3 — 소프트 손절 / 섹터 약세 매도 (priority: HIGH)
  조건: 평가가가 소프트 손절 또는 섹터 약세 사유로 PARTIAL_SELL 추천
  행동: PARTIAL_SELL 50%, order_type: SINGLE

우선순위 4 — 익절 매도 (priority: NORMAL)
  조건: 평가가가 익절 사유로 PARTIAL_SELL 추천
  행동: PARTIAL_SELL 50%, order_type: SPLIT (5주 초과 시)

우선순위 5 — 신규 매수 (priority: NORMAL)
  조건: 발굴가가 R:R ≥ 2.5로 BUY 추천
  조건:
    - 총_매수예산에 여력 있음
    - 단일 주문 ≤ 1회최대매수금액
    - 결과 포지션 ≤ 종목당최대보유금액
    - 기존 대형 포지션과 다른 섹터 우선 (분산)
  행동: BUY, order_type: SPLIT (5주 초과 시)

우선순위 6 — 추가매수 (priority: NORMAL)
  조건: 평가가가 BUY_MORE 추천
  추가 조건: 위와 동일 + 포트폴리오 전체 P&L > 0% + 보유 ≥ 3종목
  행동: BUY, order_type: SINGLE

━━━ 평가가 결정은 구속력 있음 ━━━
- 평가가가 SELL → 반드시 SELL/PARTIAL_SELL 주문 포함
- 평가가가 HOLD → 해당 종목 매도 주문 추가 금지
- 평가가가 BUY_MORE → 예산 허용 시 포함 가능 (의무 아님)
- 수량은 조정 가능하나, 행동 방향을 뒤집을 수 없음

━━━ 자본보호 모드 ━━━
발동 조건: 포트폴리오 합산 손익 ≤ -3%
  - 모든 신규 매수 즉시 중단 (총_매수예산 = 0)
  - 평가가의 모든 SELL/PARTIAL_SELL을 최우선 실행
  - summary에 "자본보호 모드 발동" 명시

━━━ 교체 규칙 ━━━
- 교체 = 기존 매도 + 다른 섹터 신규 매수
- 평가가가 이미 SELL/PARTIAL_SELL 추천한 경우에만 교체 실행
- 평가가가 추천하지 않은 종목에 대해 새로운 SELL 주문 생성 금지
- 수익률이 0%에 가깝다는 이유만으로 교체 금지

━━━ 출력 형식 (엄격 JSON) ━━━
{
  "final_orders": [
    {
      "action": "BUY|SELL|PARTIAL_SELL|BUY_MORE",
      "symbol": "종목코드",
      "name": "종목명",
      "quantity": 수량(정수),
      "price": 0,
      "order_type": "SPLIT|SINGLE",
      "priority": "HIGH|NORMAL",
      "reason": "어떤 우선순위 + 왜"
    }
  ],
  "budget_calculation": {
    "available_cash": 원,
    "cash_reserve_required": 원,
    "total_buy_budget": 원,
    "total_buy_orders_cost": 원
  },
  "portfolio_after": {
    "expected_holdings_count": N,
    "expected_cash_ratio_pct": X
  },
  "summary": "전략 + 예산 산술"
}

━━━ 절대 규칙 ━━━
- 모든 매도 주문을 매수 주문보다 앞에 배치 (잔고 확보 우선).
- 전체 매수금액 합계가 총_매수예산을 초과할 수 없음.
- quantity는 양의 정수.
- 매도 수량 ≤ 실제 보유수량.
- price는 항상 0 (실행가는 매수/매도 실행가가 처리).
- order_type: 5주 초과이면 SPLIT, 이하이면 SINGLE.
- "주문 없음"은 매도 불필요 AND 총_매수예산 ≤ 0일 때만 허용.

━━━ 재매수 절대 금지 (시스템 최우선 규칙) ━━━
recent_sold_symbols가 context에 주어지면:
  - 해당 종목에 대해 BUY, BUY_MORE 주문 생성 절대 금지
  - 이 규칙은 다른 어떤 우선순위보다 높음
  - 위반 시 시스템이 자동으로 해당 주문을 제거함

왜 이 규칙이 중요한가:
  비싸게 매수 → 싸게 손절 → 같은 날 다시 비싸게 매수 = 손실 × 2
  이 패턴이 반복되면 포트폴리오가 지속 하락합니다.
  당일 매도 종목은 절대 재매수하지 말고 다른 종목으로 분산하세요.
"""


class AssetManagerAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="자산운용가", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "stock_evaluation": StockEvaluatorAgent 결과,
            "new_candidates": StockPickerAgent 결과,
            "quotes": 현재가 목록,
            "portfolio": 보유 종목,
            "available_cash": 가용 현금,
            "total_asset_value": 총 자산 평가액,
            "max_buy_amount": 1회 최대 매수금액,
            "max_position_size": 종목당 최대 보유금액,
            "tendency_directive": 투자성향 지침
        }
        """
        stock_evaluation = context.get("stock_evaluation", {})
        new_candidates = context.get("new_candidates", {})
        quotes = context.get("quotes", [])
        portfolio = context.get("portfolio", [])
        available_cash = context.get("available_cash", 0)
        total_asset_value = context.get("total_asset_value", 0)
        max_buy = context.get("max_buy_amount", 1_000_000)
        max_position = context.get("max_position_size", 5_000_000)

        recent_sold = context.get("recent_sold_symbols", {})

        quotes_text = "\n".join(
            f"- {q['symbol']} {q['name']}: {q['current_price']:,}원 "
            f"({q['change_rate']:+.2f}%) 거래량:{q.get('volume', 0):,}"
            for q in quotes
        )
        tendency_directive = context.get("tendency_directive", "")
        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", len(portfolio))
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            diversity_section = f"\n[포트폴리오 다양성 — 최우선]\n현재 보유 {holdings_count}개 / 목표 최소 {target}개. 신규 매수를 반드시 포함하세요. '주문 없음'은 허용되지 않습니다.\n"

        # 당일 매도 종목 차단 섹션
        sold_section = ""
        if recent_sold:
            q_map = {q["symbol"]: q for q in quotes}
            sold_lines = []
            for sym, sell_price in recent_sold.items():
                q = q_map.get(sym, {})
                current = q.get("current_price", 0)
                name = q.get("name", sym)
                sold_lines.append(f"  - {name}({sym}): 매도가 {int(sell_price):,}원 / 현재가 {int(current):,}원")
            sold_section = (
                "\n⛔ [당일 매도 종목 — BUY/BUY_MORE 절대 금지]\n"
                "아래 종목은 오늘 이미 매도되었습니다. 어떤 이유로도 재매수 주문 불가:\n"
                + "\n".join(sold_lines)
                + "\n재매수 시도 즉시 시스템이 자동 차단하며, 이 패턴은 심각한 손실을 유발합니다.\n"
            )

        prompt = f"""보유 종목 평가 결과와 신규 매수 후보를 종합하여 최종 실행 주문 목록을 결정해주세요.
{tendency_section}{diversity_section}{sold_section}
[보유 종목 평가 (종목평가가 결과)]
{json.dumps(stock_evaluation, ensure_ascii=False, indent=2)}

[신규 매수 후보 (종목발굴가 결과)]
{json.dumps(new_candidates, ensure_ascii=False, indent=2)}

[현재가 목록]
{quotes_text}

[현재 보유 종목]
{json.dumps(portfolio, ensure_ascii=False, indent=2)}

[가용 현금]: {available_cash:,}원
[총 자산 평가액]: {total_asset_value:,}원
[1회 최대 매수금액]: {max_buy:,}원
[종목당 최대 보유금액]: {max_position:,}원

투자성향 지침의 현금/종목집중 기준을 적용하고, 1회 최대 매수금액과 종목당 최대 보유금액 한도를 반드시 지켜주세요.
잔고 부족 시 매도를 우선 처리한 후 매수하세요.
JSON 형식으로 응답해주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            result = json.loads(response[start:end])
        except Exception:
            return {"final_orders": [], "cash_reserve_ratio": 0.3, "summary": response}

        # ── 하드코딩 가드레일: AI 응답 사후 검증 ──
        result["final_orders"] = self._validate_orders(
            result.get("final_orders", []),
            available_cash=available_cash,
            total_asset_value=total_asset_value,
            max_buy=max_buy,
            max_position=max_position,
            portfolio=portfolio,
            quote_map={q["symbol"]: q for q in quotes},
            recent_sold_symbols=recent_sold,
        )
        return result

    @staticmethod
    def _validate_orders(
        orders: list[dict],
        available_cash: int,
        total_asset_value: int,
        max_buy: int,
        max_position: int,
        portfolio: list[dict],
        quote_map: dict,
        recent_sold_symbols: dict[str, float] | None = None,
    ) -> list[dict]:
        """AI 주문에 대한 하드 리밋 검증 — 위반 주문 제거 또는 수량 조정"""
        # 현재 보유 금액 맵
        holding_value: dict[str, int] = {}
        for p in portfolio:
            sym = p.get("pdno", "")
            qty = int(p.get("hldg_qty", 0))
            avg = float(p.get("pchs_avg_pric", 0))
            holding_value[sym] = int(qty * avg)

        recent_sold = recent_sold_symbols or {}
        validated: list[dict] = []
        remaining_cash = available_cash

        # 매도 주문은 먼저 통과 (현금 회수)
        for order in orders:
            if order.get("action") in ("SELL", "PARTIAL_SELL"):
                validated.append(order)
                sym = order.get("symbol", "")
                q = quote_map.get(sym, {})
                price = q.get("current_price", 0)
                remaining_cash += int(order.get("quantity", 0)) * price

        # 매수 주문 검증
        for order in orders:
            if order.get("action") not in ("BUY", "BUY_MORE"):
                continue

            sym = order.get("symbol", "")
            # 하드가드: 당일 매도 종목 재매수 절대 차단
            if sym and sym in recent_sold:
                logger.warning(f"[가드레일] {sym} 당일 매도 종목 재매수 시도 차단 (매도가: {int(recent_sold[sym]):,}원)")
                continue

            qty = int(order.get("quantity", 0))
            q = quote_map.get(sym, {})
            price = q.get("current_price", 0)
            if price <= 0:
                validated.append(order)
                continue

            order_amount = qty * price

            # 1) 1회 최대 매수금액 초과 → 수량 축소
            if order_amount > max_buy:
                qty = max_buy // price
                order_amount = qty * price
                if qty <= 0:
                    logger.warning(f"[가드레일] {sym} 1회 최대 매수금액 초과 — 주문 제거")
                    continue
                logger.info(f"[가드레일] {sym} 1회 매수한도 적용 → {qty}주로 축소")

            # 2) 종목당 최대 보유금액 초과 → 수량 축소
            current_value = holding_value.get(sym, 0)
            if current_value + order_amount > max_position:
                allowed = max_position - current_value
                if allowed <= 0:
                    logger.warning(f"[가드레일] {sym} 이미 최대 보유금액 도달 — 주문 제거")
                    continue
                qty = allowed // price
                order_amount = qty * price
                if qty <= 0:
                    continue
                logger.info(f"[가드레일] {sym} 포지션한도 적용 → {qty}주로 축소")

            # 3) 가용현금 초과 → 수량 축소
            if order_amount > remaining_cash:
                qty = remaining_cash // price
                order_amount = qty * price
                if qty <= 0:
                    logger.warning(f"[가드레일] 현금 부족으로 {sym} 매수 제거")
                    continue
                logger.info(f"[가드레일] {sym} 현금한도 적용 → {qty}주로 축소")

            order["quantity"] = qty
            remaining_cash -= order_amount
            holding_value[sym] = current_value + order_amount
            validated.append(order)

        return validated
