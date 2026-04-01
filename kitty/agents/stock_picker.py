"""종목발굴 에이전트 - 섹터 분석 + 실시간 시세 기반 신규 종목 선정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 퀀트 투자 전략가입니다.

역할:
- 시장분석가의 섹터 분석을 받아, 후보 종목의 실제 시세와 거래량을 검토합니다
- 후보 종목 중 매수 가치가 있는 종목을 최종 선정합니다
- 리스크 대비 수익을 최적화하는 포지션 크기를 결정합니다

원칙:
- 투자성향 지침의 종목집중·진입기준·현금 기준을 따릅니다
- 시장 리스크가 HIGH이면 신규 매수를 하지 않습니다
- 거래량이 부족한 종목(거래량 10만주 미만 또는 거래대금 10억 미만)은 매수를 보류합니다
- 투자성향 지침의 진입기준을 초과하는 과열 종목은 매수를 보류합니다
- 손절가와 목표가를 투자성향 지침의 손절/익절 기준에 맞춰 설정합니다
※ 투자성향 지침이 없으면 진입기준 +5%, 손절 -5%, 익절 +10% 기본값 사용

종목 선별 우선순위:
1. 섹터 전망 bullish + 거래량 풍부 + 등락률 양호
2. 거래량 상위 종목 중 유망 섹터에 속한 종목
3. 유동성이 낮은 종목은 아무리 유망해도 제외

출력 형식: JSON
{
  "decisions": [
    {
      "action": "BUY|HOLD",
      "symbol": "종목코드",
      "name": "종목명",
      "quantity": 수량,
      "price": 가격(0=시장가),
      "stop_loss": 손절가,
      "take_profit": 목표가,
      "reason": "결정 이유 (거래량·등락률·섹터 근거)"
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
            "analysis": SectorAnalystAgent 결과,
            "quotes": 후보 종목 + 보유 종목 현재가,
            "portfolio": 현재 보유 종목,
            "available_cash": 사용 가능 현금,
            "max_buy_amount": 최대 매수금액,
            "tendency_directive": 투자성향 지침,
            "volume_leaders": 거래량 상위 종목 (optional),
        }
        """
        analysis = context.get("analysis", {})
        available_cash = context.get("available_cash", 0)
        max_buy = context.get("max_buy_amount", 1_000_000)
        quotes = context.get("quotes", [])
        tendency_directive = context.get("tendency_directive", "")
        volume_leaders = context.get("volume_leaders", [])

        quotes_text = "\n".join(
            f"- {q['symbol']} {q['name']}: {q['current_price']:,}원 "
            f"({q['change_rate']:+.2f}%) 거래량:{q['volume']:,}"
            for q in quotes
        )

        # 거래량 상위 종목 (유동성 참고용)
        volume_text = ""
        if volume_leaders:
            volume_text = "\n[거래량 상위 종목 — 유동성 참고]\n" + "\n".join(
                f"  {v.get('name', '')}({v['symbol']}): "
                f"{v.get('current_price', 0):,}원 ({v.get('change_rate', 0):+.2f}%) "
                f"거래량:{v.get('volume', 0):,}"
                for v in volume_leaders[:10]
            )

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        prompt = f"""섹터 분석과 실시간 시세·거래량을 검토하여 신규 매수 종목을 선정해주세요.
{tendency_section}
[섹터 분석]
{json.dumps(analysis, ensure_ascii=False, indent=2)}

[후보 종목 현재가]
{quotes_text}
{volume_text}

[현재 보유 종목]
{json.dumps(context.get('portfolio', []), ensure_ascii=False, indent=2)}

[가용 현금]: {available_cash:,}원
[1회 최대 매수금액]: {max_buy:,}원

투자성향 지침의 진입기준·손절·익절·종목집중 기준에 맞춰 종목을 선정하세요.
거래량이 부족한 종목은 반드시 제외하세요.
JSON 형식으로 응답해주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"decisions": [], "strategy_summary": response}
