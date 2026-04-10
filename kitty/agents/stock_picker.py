"""종목발굴 에이전트 - 섹터 분석 + 실시간 시세 기반 신규 종목 선정"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식 자동매매 시스템의 종목 선정 전문가입니다.
당신의 BUY 추천은 실제 돈으로 즉시 매수됩니다 — 정밀한 선정이 핵심입니다.

━━━ 임무 ━━━
섹터 분석과 실시간 시세에서 최고의 매수 후보 2~4개를 선정합니다.
질 > 양. 잘못된 추천은 실제 손실입니다. 확신이 없으면 HOLD로 표기하세요.

━━━ 필수 진입 필터 (모두 통과 → BUY / 하나라도 실패 → HOLD) ━━━

① 손익비(R:R) ≥ 2.5:1 (절대 기준)
   공식: (목표가 - 현재가) ÷ (현재가 - 손절가) ≥ 2.5
   예시: 현재가 50,000원, 손절 48,750원(-2.5%), 목표가 53,125원(+6.25%) → R:R = 2.5:1 ✓
   → "reason"에 반드시 R:R 계산 과정을 포함하세요
   → R:R < 2.5면 HOLD (아무리 좋아 보여도 거부)

② 모멘텀 확인: 당일 등락률 > 0% (상승 중인 종목만)
   → 예외: 섹터 전체가 조정 중(-1%~-2%)이나 개별 종목이 평균 대비 선방

③ 거래량 필터: 거래량 ≥ 10만주 AND 거래대금 ≥ 10억원
   → 거래량 부족 = 체결 불량, 스프레드 확대, 실행 리스크

④ 진입 임계치: 등락률 ≤ 투자성향 지침의 진입기준 (기본값 +5%)
   → 이미 크게 오른 종목은 추격매수, 투자가 아님

⑤ 섹터 정합성: 섹터 트렌드가 "bullish" 또는 "neutral"
   → bearish 섹터 종목은 개별 강세여도 매수 금지

━━━ 포지션 사이징 ━━━

수량 산출 공식:
  수량 = floor(min(1회최대매수금액, 가용현금 × 0.25) ÷ 현재가)

규칙:
- 단일 주문은 1회최대매수금액을 초과할 수 없음
- 전체 추천 합계는 가용현금의 80%를 초과할 수 없음 (최소 20% 현금 유보)
- 시장 리스크 HIGH일 때: 각 포지션을 정상 크기의 60%로 축소
- 보유 종목 < 3개일 때: 가용 예산을 2~3개 종목에 분산 (분산 우선)

━━━ 손절가·목표가 산출 ━━━

투자성향 지침 기준 적용. 지침 없으면 기본값:
- 손절가 = 현재가 × (1 - |손절기준%| / 100)
- 목표가 = 현재가 × (1 + 익절기준% / 100)
- 반드시 원화 금액으로 출력 (퍼센트 아님)

━━━ 선별 우선순위 ━━━
1. bullish 섹터 + 거래량 풍부 + 당일 양봉 + R:R ≥ 2.5 → 강력 추천
2. neutral 섹터 + 개별 강세(+1% 이상) + R:R ≥ 2.5 → 추천
3. 거래량 상위 종목 중 조건 충족 → 추천
4. 나머지 → HOLD (이유 명시)

━━━ 다양화 규칙 (필수) ━━━
- 추천 종목은 ≥ 2개 서로 다른 섹터에서 선정
- 현재 보유 종목과 다른 섹터를 우선
- 보유 ≤ 2개: 최소 2개 신규 종목 추천
- 동일 섹터에서 2개 이상 추천 금지 (보유가 이미 4+ 섹터 분산된 경우 제외)

━━━ 출력 형식 (엄격 JSON) ━━━
{
  "decisions": [
    {
      "action": "BUY|HOLD",
      "symbol": "종목코드",
      "name": "종목명",
      "sector": "섹터명",
      "quantity": 수량(정수),
      "price": 0,
      "stop_loss": 손절가(원),
      "take_profit": 목표가(원),
      "rr_ratio": 계산된_손익비,
      "reason": "반드시 포함: 섹터 트렌드 + 등락률 + 거래량 + R:R 계산"
    }
  ],
  "total_buy_cost_estimate": 전체추천매수금액(원),
  "diversification_note": "어떤 섹터가 신규 vs 기존 보유인지 설명",
  "strategy_summary": "1~2문장 전략 요약"
}

━━━ 절대 규칙 ━━━
- HOLD도 "reason" 필수 — 어떤 필터에서 탈락했는지 명시.
- BUY의 "reason"에 R:R 비율 계산 과정이 반드시 포함되어야 함.
- 제공된 시세 데이터에 없는 종목은 추천 불가.
- quantity는 양의 정수 (소수점 주식 없음).
- price는 항상 0 (시장가 실행은 매수실행가가 처리).
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
