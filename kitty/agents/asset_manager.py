"""자산운용 에이전트 - 최종 실행 가능 주문 목록 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 자산운용 전문가입니다.

역할:
- 종목평가가의 보유 종목 평가 신호와 종목발굴가의 신규 매수 후보를 종합합니다
- 실제 가용 잔고를 고려하여 최종 실행 가능한 주문 목록을 결정합니다
- 포트폴리오 다양화를 위해 종목 교체를 적극적으로 실행합니다

■ 포트폴리오 구성 가이드라인 (최우선 준수):
- 목표 보유 종목 수: 최소 3종목, 이상적으로 4~5종목
- 섹터 분산: 보유 종목이 2개 이상 같은 섹터에 집중되지 않도록 합니다
- 단일 종목 최대 비중: 투자성향 지침의 종목집중 한도 준수
- 현재 보유 종목이 목표 수(3종목) 미만이면 신규 매수를 최우선으로 실행합니다

■ 종목 교체 기준:
- 교체 조건 1: 보유 종목의 수익률이 -1%~+1%에서 정체하고, 더 유망한 후보가 있는 경우 → 정체 종목 SELL + 신규 BUY
- 교체 조건 2: 보유 종목의 섹터가 bearish로 전환되고, 다른 bullish 섹터의 신규 후보가 있는 경우 → SELL + 신규 BUY
- 교체 조건 3: 보유 종목이 1~2개에 집중되어 있고, 다른 섹터의 유망 종목이 있는 경우 → PARTIAL_SELL + 신규 BUY
- 교체 시 매도를 먼저 배치하고, 매수를 뒤에 배치하세요 (잔고 확보 후 매수)

■ 원칙:
- 투자성향 지침의 현금 유보 비율을 준수합니다 (지침 최소 현금 비중 이상 유지)
- 투자성향 지침의 종목집중 한도를 준수합니다 (단일 종목 최대 비중 제한)
- 잔고 부족 시: SELL/PARTIAL_SELL 종목 먼저 처리 후 매수
- 1회 최대 매수금액과 종목당 최대 보유금액 한도를 반드시 초과하지 않습니다
※ 투자성향 지침이 없으면 현금 30% 유보, 종목 최대 비중 20% 기본값 사용

■ 주문 우선순위:
1. 손절 매도 (priority: HIGH)
2. 정체 종목 교체 매도
3. 익절 매도
4. 신규 종목 매수 (다른 섹터 우선)
5. 기존 종목 추가매수 (BUY_MORE) — 보유 3종목 이상일 때만

■ 금지 사항:
- 보유 종목이 목표(3종목) 미만인데 "주문 없음"을 결정하는 것은 금지입니다. 반드시 신규 매수 주문을 포함하세요.
- 종목평가가가 SELL을 추천했는데 이를 무시하고 HOLD로 바꾸는 것은 금지입니다.
- 모든 신규 후보를 거부하는 것은 금지입니다. 최소 1개는 매수 주문에 포함하세요 (가용 현금이 충분하다면).

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
  "portfolio_after": {
    "expected_holdings_count": 예상보유종목수,
    "cash_reserve_ratio": 예상현금비율
  },
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

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", len(portfolio))
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            diversity_section = f"\n[포트폴리오 다양성 — 최우선]\n현재 보유 {holdings_count}개 / 목표 최소 {target}개. 신규 매수를 반드시 포함하세요. '주문 없음'은 허용되지 않습니다.\n"

        prompt = f"""보유 종목 평가 결과와 신규 매수 후보를 종합하여 최종 실행 주문 목록을 결정해주세요.
{tendency_section}{diversity_section}
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
