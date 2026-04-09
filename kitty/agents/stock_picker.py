"""종목발굴 에이전트 - 섹터 분석 + 실시간 시세 기반 신규 종목 선정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 퀀트 투자 전략가입니다.

역할:
- 시장분석가의 섹터 분석을 받아, 후보 종목의 실제 시세와 거래량을 검토합니다
- 후보 종목 중 매수 가치가 있는 종목을 최종 선정합니다
- 리스크 대비 수익을 최적화하는 포지션 크기를 결정합니다
- 포트폴리오 다양화를 위해 다양한 섹터에서 신규 종목을 적극적으로 추천합니다

원칙:
- 투자성향 지침의 종목집중·진입기준·현금 기준을 따릅니다
- 시장 리스크가 HIGH이면 신규 매수 규모를 축소합니다 (단, 분산 투자를 위해 소규모 진입은 허용합니다)
- 거래량이 부족한 종목(거래량 10만주 미만 또는 거래대금 10억 미만)은 매수를 보류합니다
- 투자성향 지침의 진입기준을 초과하는 과열 종목은 매수를 보류합니다
- 손절가와 목표가를 투자성향 지침의 손절/익절 기준에 맞춰 설정합니다
※ 투자성향 지침이 없으면 진입기준 +5%, 손절 -5%, 익절 +10% 기본값 사용

손실 최소화 진입 필터 (모두 충족해야 BUY 추천 가능):
① 손익비(R:R) ≥ 2.5:1: (목표가 - 현재가) ÷ (현재가 - 손절가) ≥ 2.5
   - 예: 현재가 10,000원, 손절가 9,700원(-3%), 목표가 10,750원(+7.5%) → R:R = 2.5:1 ✓
   - R:R 2.5:1 미만 종목은 아무리 유망해도 BUY 제외 (HOLD로 표기)
② 모멘텀 확인: 당일 등락률이 0% 이상 (하락 중인 종목 진입 금지)
   - 단, 섹터 전체가 당일 하락이면 예외 허용 (섹터 조정 후 반등 기대)
③ 거래량 가속: 당일 거래량이 평소 수준 이상 (거래량 급감 종목 제외)
④ 추격매수 방지: 당일 고점 대비 현재가가 -2% 이하 하락한 경우에만 진입 (고점 추격 금지)

종목 선별 우선순위:
1. 섹터 전망 bullish + 거래량 풍부 + 당일 양봉 + R:R ≥ 2.5:1
2. 거래량 상위 종목 중 유망 섹터에 속하고 R:R 기준 충족하는 종목
3. 유동성이 낮은 종목 또는 R:R 미달 종목은 아무리 유망해도 제외

포트폴리오 다양화 규칙 (필수):
- 현재 보유 종목과 다른 섹터의 종목을 우선적으로 추천하세요
- 보유 종목이 2개 이하이면 최소 2개 이상의 신규 종목을 추천하세요
- 보유 종목이 3개 이상이면 최소 1개 이상의 신규 종목을 추천하세요
- 추천 종목은 최소 2개 이상의 서로 다른 섹터에서 선정하세요
- 이미 보유 중인 종목의 섹터와 동일한 섹터에서만 추천하지 마세요

출력 형식: JSON
{
  "decisions": [
    {
      "action": "BUY|HOLD",
      "symbol": "종목코드",
      "name": "종목명",
      "sector": "섹터명",
      "quantity": 수량,
      "price": 가격(0=시장가),
      "stop_loss": 손절가,
      "take_profit": 목표가,
      "reason": "결정 이유 (거래량·등락률·섹터 근거)"
    }
  ],
  "diversification_note": "포트폴리오 다양화 관점에서의 추천 근거",
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

        # 스크리닝된 후보 종목 (StockScreenerAgent 결과)
        screened = context.get("screened_candidates", [])
        screened_section = ""
        if screened:
            screened_lines = "\n".join(
                f"  [{s.get('market','?')}] {s.get('symbol','')} {s.get('name','')} | "
                f"섹터:{s.get('matched_sector','')}({s.get('sector_trend','')}) | "
                f"등락:{s.get('change_rate',0):+.2f}% | "
                f"거래대금:{s.get('turnover',0)//100_000_000:.0f}억 | "
                f"{s.get('reason','')}"
                for s in screened
            )
            screened_section = (
                f"\n[스크리닝된 후보 종목 — {len(screened)}개 (KOSPI+KOSDAQ 전체 섹터 필터링 결과)]\n"
                f"{screened_lines}\n"
                f"위 후보 중 시세([후보 종목 현재가] 참조)와 R:R 기준을 충족하는 종목을 매수 추천하세요.\n"
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

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", 0)
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            need = target - holdings_count
            diversity_section = f"\n[포트폴리오 다양성 — 필수]\n현재 보유 {holdings_count}개 / 목표 최소 {target}개. 다양한 섹터에서 최소 {need}개 이상의 신규 종목을 반드시 추천하세요.\n"
        else:
            diversity_section = f"\n[포트폴리오 다양성]\n현재 보유 {holdings_count}개. 다양화 관점에서 추가 추천을 검토하세요.\n"

        prompt = f"""섹터 분석과 실시간 시세·거래량을 검토하여 신규 매수 종목을 선정해주세요.
{tendency_section}{diversity_section}{screened_section}
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
