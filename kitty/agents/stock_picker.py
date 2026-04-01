"""종목발굴 에이전트 - 섹터 분석 기반 신규 종목 선정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 퀀트 투자 전략가입니다.

역할:
- 시장분석가의 섹터 거시 분석을 받아, 각 섹터 후보 종목의 실제 시세를 검토합니다
- 후보 종목 중 현재 매수 가치가 있는 종목을 최종 선정합니다
- 보유 종목에 대해 손절/익절 조건을 확인하고 매도 여부를 결정합니다
- 리스크 대비 수익을 최적화하는 포지션 크기를 결정합니다

원칙:
- 한 종목에 전체 자산의 20% 이상 투자하지 않습니다
- 시장 리스크가 HIGH이면 신규 매수를 하지 않습니다
- 손절 기준: 매수 평균가 대비 -5%
- 익절 기준: 매수 평균가 대비 +15%
- 유망 섹터에서도 시세가 과열된 종목(당일 +5% 이상)은 매수를 보류합니다

출력 형식: JSON
{
  "decisions": [
    {
      "action": "BUY|SELL|HOLD",
      "symbol": "종목코드",
      "name": "종목명",
      "quantity": 수량,
      "price": 가격(0=시장가),
      "stop_loss": 손절가,
      "take_profit": 목표가,
      "reason": "결정 이유 (섹터 전망 + 종목 선정 근거)"
    }
  ],
  "strategy_summary": "전략 요약"
}
"""


class StockPickerAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="종목발굴가", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "analysis": SectorAnalystAgent 결과 (섹터 분석),
            "quotes": 후보 종목 + 보유 종목 현재가 목록,
            "portfolio": 현재 보유 종목,
            "available_cash": 사용 가능 현금,
            "max_buy_amount": 최대 매수금액
        }
        """
        analysis = context.get("analysis", {})
        available_cash = context.get("available_cash", 0)
        max_buy = context.get("max_buy_amount", 1_000_000)
        quotes = context.get("quotes", [])
        tendency_directive = context.get("tendency_directive", "")

        quotes_text = "\n".join(
            f"- {q['symbol']} {q['name']}: {q['current_price']:,}원 "
            f"({q['change_rate']:+.2f}%) 거래량:{q['volume']:,}"
            for q in quotes
        )

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        prompt = f"""섹터 거시 분석과 후보 종목 시세를 검토하여 매매 전략을 수립해주세요.
{tendency_section}
[섹터 거시 분석]
{json.dumps(analysis, ensure_ascii=False, indent=2)}

[후보 종목 및 보유 종목 현재가]
{quotes_text}

[현재 보유 종목 상세]
{json.dumps(context.get('portfolio', []), ensure_ascii=False, indent=2)}

[가용 현금]: {available_cash:,}원
[1회 최대 매수금액]: {max_buy:,}원

유망 섹터 후보 중 실제 매수할 종목을 선정하고, 보유 종목의 손절/익절 여부를 판단하여 JSON 형식으로 알려주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"decisions": [], "strategy_summary": response}
