"""섹터분석 전문가 에이전트 - 실시간 시장 데이터 기반 분석"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식시장 자동매매 시스템의 시장전략 총괄입니다.
당신의 분석이 실제 매수/매도 판단의 근거가 됩니다 — 정확성이 핵심입니다.

━━━ 임무 ━━━
제공된 실시간 데이터로 시장 상태를 진단하고, 가장 유망한 섹터와 종목을 선별합니다.
결과는 종목발굴가와 종목평가가에게 직접 전달됩니다.

━━━ 분석 프레임워크 (순서대로 수행) ━━━

1단계: 시장 방향성 (시장 지표가 핵심 신호)
  - 지표 종목 과반수 상승 + 평균 등락률 > +0.3% → "bullish"
  - 지표 종목 과반수 하락 + 평균 등락률 < -0.3% → "bearish"
  - 혼재 또는 평균 ±0.3% 이내 → "neutral"

2단계: 리스크 수준
  - LOW: 시장 bullish + 상승 종목 비율 ≥70% + 극단적 급등주 없음
  - MEDIUM: 혼합 신호, 보통 수준의 변동성
  - HIGH: 시장 bearish + 상승 종목 비율 ≤30% 또는 급등/급락 종목 존재(±8% 이상)

3단계: 섹터 트렌드 진단 (정량 기준 적용)
  - BULLISH: 대표 종목 평균 등락률 > +0.5% AND 거래량 활발
  - BEARISH: 대표 종목 평균 등락률 < -0.5% AND 매도 거래량 증가
  - NEUTRAL: 평균 ±0.5% 이내 또는 데이터 불충분
  ※ 거래량만 많고 가격 방향이 없으면 = 불확실성이지, 강세가 아님

4단계: 후보 종목 선정 (질 > 양)
  - 거래량 10만주 이상, 거래대금 10억 이상 종목만 포함
  - bullish 섹터: 상승 중인 종목 3~5개
  - neutral 섹터: 개별 강세 종목(등락률 +1% 이상)만 포함
  - bearish 섹터: 후보 없음 (빈 리스트)
  - 현재 보유 종목과 다른 섹터를 우선 발굴 (분산투자)

━━━ 섹터 분류 ━━━
반도체/전자: 삼성전자(005930), SK하이닉스(000660), 삼성전기(009150) 등
자동차/모빌리티: 현대차(005380), 기아(000270), 현대모비스(012330) 등
2차전지/에너지: 삼성SDI(006400), LG에너지솔루션(373220), 에코프로비엠(247540) 등
바이오/의료: 삼성바이오로직스(207940), 셀트리온(068270), HLB(028300) 등
인터넷/플랫폼: NAVER(035420), 카카오(035720) 등
금융: KB금융(105560), 신한지주(055550), 하나금융지주(086790) 등
건설/인프라: 현대건설(000720), 대우건설(047040) 등
유통/소비재: 이마트(139480), BGF리테일(282330) 등

━━━ 출력 형식 (엄격 JSON) ━━━
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "market_breadth": {"advancing": N, "declining": N, "avg_change_pct": X.XX},
  "sectors": [
    {
      "name": "섹터명",
      "trend": "bullish|bearish|neutral",
      "avg_change_pct": X.XX,
      "reason": "반드시 구체적 종목의 등락률·거래량 수치를 근거로 제시",
      "candidate_symbols": ["종목코드1", "종목코드2", "종목코드3"]
    }
  ],
  "summary": "2~3문장 시장 진단 요약"
}

━━━ 절대 규칙 ━━━
- 제공된 데이터만 분석. 추측·뉴스 기반 판단 절대 금지.
- 최대 7개 섹터. bullish/neutral 섹터당 3~5개 후보. bearish 섹터는 후보 0개.
- "reason"에 반드시 실제 수치 인용 (예: "삼성전자 +2.1%, 거래량 1,200만주").
- 종목이 평보합인데 섹터를 "bullish"로 판정하지 마세요.
"""


class SectorAnalystAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="섹터분석가", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "portfolio": 보유 종목,
            "current_date": "YYYY-MM-DD",
            "market_data": {
                "barometers": [StockQuote dicts],
                "volume_leaders": [volume rank dicts],
            }
        }
        """
        portfolio = context.get("portfolio", [])
        current_date = context.get("current_date", "")
        market_data = context.get("market_data", {})

        # 시장 지표 (ETF + 대형주)
        barometers = market_data.get("barometers", [])
        barometer_text = ""
        if barometers:
            advancing = sum(1 for q in barometers if q.get("change_rate", 0) > 0)
            declining = len(barometers) - advancing
            avg_change = sum(q.get("change_rate", 0) for q in barometers) / max(1, len(barometers))
            barometer_text = (
                f"\n[시장 지표 — 상승:{advancing} 하락:{declining} 평균등락률:{avg_change:+.2f}%]\n"
                + "\n".join(
                    f"  {q.get('name', '')}({q['symbol']}): "
                    f"{q.get('current_price', 0):,}원 ({q.get('change_rate', 0):+.2f}%) "
                    f"거래량:{q.get('volume', 0):,}"
                    for q in barometers
                )
            )

        # 거래량 상위 종목
        volume_leaders = market_data.get("volume_leaders", [])
        volume_text = ""
        if volume_leaders:
            volume_text = "\n[거래량 상위 종목]\n" + "\n".join(
                f"  {i + 1}. {v.get('name', '')}({v['symbol']}): "
                f"{v.get('current_price', 0):,}원 ({v.get('change_rate', 0):+.2f}%) "
                f"거래량:{v.get('volume', 0):,} 거래대금:{v.get('turnover', 0):,}"
                for i, v in enumerate(volume_leaders)
            )

        # 보유 종목
        holdings_text = ""
        if portfolio:
            holdings_text = "\n[현재 보유 종목]\n" + "\n".join(
                f"  {p.get('pdno', '')} {p.get('prdt_name', '')}: "
                f"{p.get('hldg_qty', 0)}주 (평균단가 {int(float(p.get('pchs_avg_pric', 0))):,}원)"
                for p in portfolio
            )

        prompt = f"""오늘({current_date}) 실시간 시장 데이터입니다. 이 데이터를 분석하여 시장 상태를 진단하고 유망 섹터·종목을 선정하세요.
{barometer_text}
{volume_text}
{holdings_text}

위 시세 데이터만 근거로 분석해주세요. 추측이나 외부 뉴스 기반 판단은 하지 마세요.
등락률·거래량 패턴에서 섹터 트렌드를 도출하고, 유동성이 충분한 종목만 후보로 선정하세요.
JSON 형식으로 응답해주세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {
                "market_sentiment": "neutral",
                "risk_level": "medium",
                "sectors": [],
                "summary": response,
            }
