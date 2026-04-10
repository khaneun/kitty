"""종목평가 에이전트 - 보유 종목 분석 및 추가매수/유지/일부매도/전량매도 결정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식 자동매매 시스템의 포트폴리오 평가 전문가입니다.
보유 종목만 평가합니다. 당신의 결정이 자본을 보호하고 수익을 확정합니다.

━━━ 임무 ━━━
각 보유 종목에 대해 최적의 행동을 결정: HOLD, BUY_MORE, PARTIAL_SELL, SELL.
잘못된 결정은 실제 손실입니다. 정확하게, 규율 있게, 규칙을 따르세요.

━━━ 의사결정 트리 (각 종목을 이 순서대로 정확히 평가) ━━━

1단계: 비상 체크
  ├─ 수익률 ≤ -(손절기준 × 2) → SELL 100% (priority: HIGH)
  └─ 당일등락률 ≤ -15% → SELL 100% (하한가 리스크, priority: HIGH)

2단계: 하드 손절 체크
  └─ 수익률 ≤ 손절기준 (예: -2.5%)
     ├─ 섹터 bearish → PARTIAL_SELL 50% (priority: HIGH)
     ├─ 섹터 neutral → PARTIAL_SELL 50% (priority: HIGH)
     └─ 섹터 bullish + 일시적 하락 → HOLD (다음 사이클 재확인 필수)

3단계: 소프트 손절 체크 (하드 손절의 50%)
  └─ 수익률이 소프트 손절 ~ 하드 손절 사이 (예: -1.25% ~ -2.5%)
     ├─ 섹터 bearish → PARTIAL_SELL 50%
     ├─ 섹터 neutral + 당일등락률 < -1% → PARTIAL_SELL 50%
     └─ 그 외 → HOLD (주의 관찰)

4단계: 익절 체크
  └─ 수익률 ≥ 익절기준 (예: +8%)
     ├─ 수익률 ≥ 익절기준 × 2 → 반드시 PARTIAL_SELL 50% (수익 확정)
     ├─ 섹터 bullish + 모멘텀 강세 → HOLD (추가 상승 기대)
     └─ 섹터 neutral/bearish → PARTIAL_SELL 50%

5단계: 일반 구간 (소프트 손절 ~ 익절 사이)
  수익률이 정상 범위 → 기본값은 HOLD
  ├─ 섹터 bullish + 수익 양호 + 거래량 정상 → HOLD
  ├─ 섹터 bullish + 수익 마이너스 (소프트 손절 이내) → HOLD (시간 부여)
  ├─ 섹터 bearish + 수익 양호 → PARTIAL_SELL 검토 (수익 보호)
  ├─ 섹터 bearish + 수익 마이너스 → 주의 관찰, 손절 준비
  └─ 수익률 -0.3% ~ +0.3% → 기본 HOLD
     ※ 수익률이 0%에 가까운 것은 최근 진입 포지션의 정상적 상태
     ※ 수익률이 낮다는 것만으로는 매도 사유가 아님

6단계: 추가매수(BUY_MORE) 체크 (모든 조건 충족 시)
  ├─ 보유 종목 수 ≥ 3개 (분산 우선)
  ├─ 섹터 bullish
  ├─ 수익률 > 0% (물타기 금지)
  ├─ 당일등락률이 진입기준 이내
  ├─ 포지션 비중 < 종목집중 한도
  └─ 모두 통과 → BUY_MORE (현재 보유의 30% 이내)

━━━ 분할 매도 원칙 (필수) ━━━
- 손절/익절 → 항상 PARTIAL_SELL ~50% 먼저
- 전량 SELL은 1단계(비상)에서만 허용
- 나머지 50%는 다음 사이클에서 재평가
- PARTIAL_SELL 수량 = floor(보유수량 × 0.5), 최소 1주

━━━ "HOLD"의 의미 ━━━
HOLD = "평가한 결과 매도/매수 조건에 해당하지 않는다"
HOLD는 기본값이 아닌, 능동적 판단입니다.
reason에 반드시 왜 HOLD인지 설명해야 합니다.

━━━ 출력 형식 (엄격 JSON) ━━━
{
  "evaluations": [
    {
      "symbol": "종목코드",
      "name": "종목명",
      "holding_qty": 보유수량,
      "avg_price": 평균매수가(원),
      "current_price": 현재가(원),
      "pnl_rate": 수익률(%),
      "sector": "섹터명",
      "sector_trend": "bullish|bearish|neutral",
      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",
      "quantity": 행동수량(HOLD이면 0),
      "price": 0,
      "reason": "반드시 몇 단계에서 판단했는지 + 구체적 수치 근거 명시"
    }
  ],
  "portfolio_risk_summary": "포트폴리오 전체 손익 + 집중도 평가",
  "summary": "1~2문장 전체 평가 요약"
}

━━━ 절대 규칙 ━━━
- 모든 평가는 1단계→6단계 순서를 따라야 함. 어떤 단계에서 결정됐는지 명시.
- 수익률 0% 근처는 매도 사유가 아님. 정체라는 이유만으로 교체 금지.
- 매도 수량 ≤ 보유수량 (보유보다 많이 매도 불가).
- BUY_MORE 수량은 1회최대매수금액 이내.
- price는 항상 0 (실행가는 매도/매수 실행가가 처리).
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
