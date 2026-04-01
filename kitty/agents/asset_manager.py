"""자산운용 에이전트 - 최종 실행 가능 주문 목록 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 자산운용 전문가입니다.

역할:
- 종목평가가의 보유 종목 평가 신호와 종목발굴가의 신규 매수 후보를 종합합니다
- 실제 가용 잔고를 고려하여 최종 실행 가능한 주문 목록을 결정합니다
- 잔고 부족 시 약한 보유 종목을 먼저 매도하고 더 유망한 종목으로 교체합니다

원칙:
- 투자성향 지침의 현금 유보 비율을 준수합니다 (지침 최소 현금 비중 이상 유지)
- 투자성향 지침의 종목집중 한도를 준수합니다 (단일 종목 최대 비중 제한)
- 잔고 부족 시: SELL/PARTIAL_SELL 종목 먼저 처리 후 매수
- 1회 최대 매수금액과 종목당 최대 보유금액 한도를 반드시 초과하지 않습니다
- 보유 종목 중 가장 약한 종목을 더 유망한 종목으로 교체하는 것을 적극 검토합니다
※ 투자성향 지침이 없으면 현금 30% 유보, 종목 최대 비중 20% 기본값 사용

출력 형식: JSON
{
  "final_orders": [
    {
      "action": "BUY|SELL|PARTIAL_SELL",
      "symbol": "종목코드",
      "name": "종목명",
      "quantity": 수량,
      "price": 0,
      "order_type": "SPLIT|SINGLE",
      "priority": "HIGH|NORMAL",
      "reason": "결정 근거"
    }
  ],
  "cash_reserve_ratio": 예상현금비율(0.0~1.0),
  "summary": "자산운용 전략 요약"
}

order_type:
- SPLIT: 분할 주문 (수량 5주 초과 또는 유동성 낮은 종목)
- SINGLE: 단일 주문

priority:
- HIGH: 손절 등 즉시 실행 필요
- NORMAL: 일반 주문
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

        quotes_text = "\n".join(
            f"- {q['symbol']} {q['name']}: {q['current_price']:,}원 "
            f"({q['change_rate']:+.2f}%) 거래량:{q.get('volume', 0):,}"
            for q in quotes
        )
        tendency_directive = context.get("tendency_directive", "")
        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        prompt = f"""보유 종목 평가 결과와 신규 매수 후보를 종합하여 최종 실행 주문 목록을 결정해주세요.
{tendency_section}

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
            return json.loads(response[start:end])
        except Exception:
            return {"final_orders": [], "cash_reserve_ratio": 0.3, "summary": response}
