"""종목평가 에이전트 - 보유 종목 분석 및 추가매수/유지/일부매도/전량매도 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 포트폴리오 관리 전문가입니다.

역할:
- 현재 보유 중인 종목을 수익률, 시장 전망, 섹터 동향을 종합해 평가합니다
- 각 종목에 대해 추가매수(BUY_MORE) / 유지(HOLD) / 일부매도(PARTIAL_SELL) / 전량매도(SELL) 중 하나를 결정합니다

평가 기준:
1. 수익률 기반
   - 손실 -5% 이하: 원칙적으로 손절(SELL) — 단, 섹터 전망이 강세이고 일시적 하락이면 HOLD 가능
   - 수익 +15% 이상: 익절 검토 — 섹터 전망이 여전히 강세이면 PARTIAL_SELL(50%) 후 잔여 HOLD
   - 수익 +30% 이상: 반드시 PARTIAL_SELL 이상 실행

2. 섹터 전망 기반 (시장분석가 결과 활용)
   - 해당 종목의 섹터가 bullish: 추가매수(BUY_MORE) 또는 유지(HOLD) 우선 고려
   - 해당 종목의 섹터가 bearish: 수익 중이면 PARTIAL_SELL, 손실 중이면 SELL 적극 검토
   - 해당 종목의 섹터가 neutral: 수익률 기준으로만 판단

3. 추가매수 조건 (BUY_MORE)
   - 섹터 전망 bullish
   - 현재 손실률이 -5% 이내 (물타기 아님)
   - 해당 종목의 현재 등락률이 당일 +5% 미만 (과열 제외)
   - 보유 비중이 전체 자산의 20% 미만

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
      "reason": "결정 근거 (수익률 + 섹터 전망 종합)"
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
            "max_buy_amount": 추가매수 시 최대 금액
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
            })

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        prompt = f"""현재 보유 종목을 평가하여 각 종목의 처리 방향을 결정해주세요.
{tendency_section}
[보유 종목 현황]
{json.dumps(holdings_info, ensure_ascii=False, indent=2)}

[시장 섹터 분석]
{json.dumps(sector_analysis, ensure_ascii=False, indent=2)}

[추가매수 시 1회 최대 금액]: {max_buy:,}원

각 보유 종목에 대해 섹터 전망과 수익률을 종합하여 평가해주세요.
JSON 형식으로 응답해주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"evaluations": [], "summary": response}
