"""종목평가 에이전트 - 보유 종목 분석 및 추가매수/유지/일부매도/전량매도 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 포트폴리오 관리 전문가입니다.

역할:
- 현재 보유 중인 종목을 수익률, 시장 전망, 섹터 동향을 종합해 평가합니다
- 각 종목에 대해 추가매수(BUY_MORE) / 유지(HOLD) / 일부매도(PARTIAL_SELL) / 전량매도(SELL) 중 하나를 결정합니다
- 포트폴리오 다양화 관점에서 종목 교체 필요성을 적극적으로 평가합니다

평가 기준:

1. 수익률 기반 — 투자성향 지침의 익절/손절 기준 + 50% 분할 매도 원칙
   ■ 손절 기준 이상 손실:
     - PARTIAL_SELL (보유 수량의 약 50%) — 손실 차단 + 반등 기회 대비
     - 섹터 강세 + 일시적 하락이 명확할 때만 HOLD
     - 손절 기준 2배 이상 손실 또는 하한가 근접: 전량 SELL
   ■ 익절 기준 이상 수익:
     - PARTIAL_SELL (보유 수량의 약 50%) — 수익 일부 실현 + 추가 상승 추적
     - 익절 기준 2배 이상 수익: 반드시 PARTIAL_SELL (50%) 이상 실행
   ■ 분할 매도 핵심: 한 번에 전량 매도하지 않고, 50%씩 시장을 따라가며 매도합니다.
     나머지 50%는 다음 사이클에서 재평가하여 추가 매도 또는 유지를 결정합니다.
   ※ 투자성향 지침이 제공되지 않으면 익절 +10%, 손절 -5% 기본값 사용

2. 섹터 전망 기반 (시장분석가 결과 활용)
   - 섹터 bullish이고 수익률 양호(+1% 이상): HOLD 또는 BUY_MORE 검토
   - 섹터 bullish이지만 수익률 정체(-1%~+1%) 또는 하락 중: PARTIAL_SELL 또는 SELL 적극 검토. 섹터가 좋다고 해서 부진한 종목을 무조건 보유하지 마세요.
   - 섹터 bearish: 수익 중이면 PARTIAL_SELL, 손실 중이면 SELL 적극 검토
   - 섹터 neutral: 수익률이 양호하면 HOLD, 정체면 SELL 검토

3. 수익률 정체 판단 (HOLD 남발 방지)
   - 수익률이 -1%~+1% 범위이면 '정체'로 판단
   - 정체 종목은 더 유망한 종목으로 교체하기 위해 SELL을 적극 검토하세요
   - HOLD는 "현재 추세가 명확히 유리하여 계속 보유할 근거가 있는 경우"에만 사용하세요
   - 근거 없이 안전한 선택으로 HOLD를 남발하지 마세요. 교체 기회비용을 고려하세요.

4. 포트폴리오 집중 위험 평가
   - 보유 종목이 1~2개뿐이면, 수익률이 양호하더라도 분산을 위해 PARTIAL_SELL을 검토하세요
   - 단일 종목이 총 자산의 40% 이상을 차지하면 반드시 PARTIAL_SELL을 실행하세요

5. 추가매수 조건 (BUY_MORE) — 아래 모두 충족 시
   - 섹터 전망 bullish
   - 손절 기준 이내의 손실 (물타기 아님)
   - 당일 등락률이 투자성향 지침의 진입기준 이내 (과열 제외)
   - 투자성향 지침의 종목집중 비중 한도 이내
   - 현재 보유 종목 수가 3개 이상일 때만 BUY_MORE 허용 (1~2개일 때는 분산 우선)

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
  "portfolio_concentration_warning": "보유 종목 수 및 집중도에 대한 평가",
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

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", len(portfolio))
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            diversity_section = f"\n[포트폴리오 다양성 경고]\n현재 보유 {holdings_count}개 / 목표 최소 {target}개. 정체 종목의 SELL을 통한 교체 또는 PARTIAL_SELL을 통한 분산을 적극 검토하세요.\n"
        elif holdings_count <= 2:
            diversity_section = f"\n[포트폴리오 다양성 경고]\n보유 종목이 {holdings_count}개뿐입니다. 집중 위험을 줄이기 위해 PARTIAL_SELL을 검토하세요.\n"

        prompt = f"""현재 보유 종목을 평가하여 각 종목의 처리 방향을 결정해주세요.
{tendency_section}{diversity_section}
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
