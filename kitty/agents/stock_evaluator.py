"""종목평가 에이전트 - 보유 종목 분석 및 추가매수/유지/일부매도/전량매도 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 포트폴리오 관리 전문가입니다.

역할:
- 현재 보유 중인 종목을 수익률, 시장 전망, 섹터 동향을 종합해 평가합니다
- 각 종목에 대해 추가매수(BUY_MORE) / 유지(HOLD) / 일부매도(PARTIAL_SELL) / 전량매도(SELL) 중 하나를 결정합니다

평가 기준:
1. 수익률 기반 — 투자성향 지침의 익절/손절 기준을 따릅니다
   - 지침의 손절 기준 이상 손실: SELL 적극 검토 (섹터 강세 + 일시적 하락이 명확할 때만 HOLD)
   - 지침의 익절 기준 이상 수익: PARTIAL_SELL 또는 SELL 검토
   - 익절 기준의 2배 이상 수익: 반드시 PARTIAL_SELL 이상 실행
   ※ 투자성향 지침이 제공되지 않으면 익절 +10%, 손절 -5% 기본값 사용

2. 섹터 전망 기반 (시장분석가 결과 활용)
   - 해당 종목의 섹터가 bullish: BUY_MORE 또는 HOLD 우선 고려
   - 해당 종목의 섹터가 bearish: 수익 중이면 PARTIAL_SELL, 손실 중이면 SELL 적극 검토
   - 해당 종목의 섹터가 neutral: 수익률 기준으로만 판단

3. 추가매수 조건 (BUY_MORE) — 아래 모두 충족 시
   - 섹터 전망 bullish
   - 손절 기준 이내의 손실 (물타기 아님)
   - 당일 등락률이 투자성향 지침의 진입기준 이내 (과열 제외)
   - 투자성향 지침의 종목집중 비중 한도 이내

출력 형식: JSON
{
  "evaluations": [
    {
      "symbol": "종목코드",
      "name": "종목명",
      "holding_qty": 보유수량,
      "avg_price": 평균매수가,
      "current_price": 현재가,
      "pnl_rate": 수익률(소수, 예: -3.4),
      "sector": "해당 섹터명",
      "sector_trend": "bullish|bearish|neutral",
      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",
      "quantity": 추가매수 또는 매도 수량(HOLD이면 0),
      "price": 0,
      "reason": "결정 근거 (투자성향 지침의 어떤 기준에 해당하는지 명시)"
    }
  ],
  "summary": "전체 포트폴리오 평가 요약"
}
"""


class StockEvaluatorAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="종목평가가", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "portfolio": 보유 종목 목록 (KIS balance output1),
            "quotes": 보유 종목 현재가 목록,
            "sector_analysis": SectorAnalystAgent 결과,
            "max_buy_amount": 추가매수 시 최대 금액,
            "tendency_directive": 투자성향 지침 텍스트
        }
        """
        portfolio = context.get("portfolio", [])
        if not portfolio:
            return {"evaluations": [], "summary": "보유 종목 없음"}

        quotes = context.get("quotes", [])
        sector_analysis = context.get("sector_analysis", {})
        max_buy = context.get("max_buy_amount", 1_000_000)
        tendency_directive = context.get("tendency_directive", "")

        quote_map = {q["symbol"]: q for q in quotes}

        # 보유 종목별 손익 계산
        holdings_info = []
        for p in portfolio:
            symbol = p.get("pdno", "")
            name = p.get("prdt_name", "")
            holding_qty = int(p.get("hldg_qty", 0))
            avg_price = float(p.get("pchs_avg_pric", 0))
            quote = quote_map.get(symbol, {})
            current_price = quote.get("current_price", 0)

            if avg_price > 0 and current_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price * 100
            else:
                pnl_rate = 0.0

            holdings_info.append({
                "symbol": symbol,
                "name": name,
                "holding_qty": holding_qty,
                "avg_price": int(avg_price),
                "current_price": current_price,
                "pnl_rate": round(pnl_rate, 2),
                "change_rate_today": quote.get("change_rate", 0),
                "volume": quote.get("volume", 0),
            })

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        prompt = f"""현재 보유 종목을 평가하여 각 종목의 처리 방향을 결정해주세요.
{tendency_section}
[보유 종목 현황]
{json.dumps(holdings_info, ensure_ascii=False, indent=2)}

[시장 섹터 분석]
{json.dumps(sector_analysis, ensure_ascii=False, indent=2)}

[추가매수 시 1회 최대 금액]: {max_buy:,}원

투자성향 지침에 명시된 익절/손절/종목집중 기준을 적용하여 각 종목을 평가하세요.
JSON 형식으로 응답해주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"evaluations": [], "summary": response}
