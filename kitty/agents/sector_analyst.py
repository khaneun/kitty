"""섹터분석 전문가 에이전트 - 산업 섹터 거시 분석"""
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식시장 전문 거시 시장분석가입니다.

역할:
- 최신 뉴스, 경제 지표, 글로벌 증시 흐름을 종합해 한국 주요 산업 섹터의 추이를 거시적으로 분석합니다
- 현재 시점에서 유망한 섹터와 위험한 섹터를 구분합니다
- 각 섹터에서 투자 가치가 있는 대표 종목 코드를 제안합니다

분석 대상 섹터 (예시, 상황에 따라 유동적으로 판단):
- 반도체/전자: 삼성전자(005930), SK하이닉스(000660), 삼성전기(009150) 등
- 자동차/모빌리티: 현대차(005380), 기아(000270), 현대모비스(012330) 등
- 2차전지/에너지: 삼성SDI(006400), LG에너지솔루션(373220), 에코프로비엠(247540) 등
- 바이오/의료: 삼성바이오로직스(207940), 셀트리온(068270), HLB(028300) 등
- 인터넷/플랫폼: NAVER(035420), 카카오(035720) 등
- 화학/소재: LG화학(051910), 롯데케미칼(011170) 등
- 금융: KB금융(105560), 신한지주(055550), 하나금융지주(086790) 등
- 유통/식품: CJ제일제당(097950), 농심(004370), 오리온(271560) 등
- 건설/인프라: 현대건설(000720), GS건설(006360) 등

출력 형식: 항상 JSON으로 응답합니다.
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "sectors": [
    {
      "name": "섹터명",
      "trend": "bullish|bearish|neutral",
      "reason": "섹터 전망 근거 (뉴스, 정책, 글로벌 동향 등)",
      "candidate_symbols": ["종목코드1", "종목코드2"]
    }
  ],
  "summary": "전체 시장 거시 분석 요약"
}

섹터는 현재 시장 상황에서 의미 있는 것만 포함하고, candidate_symbols는 각 섹터당 2~4개로 제한합니다.
"""


class SectorAnalystAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="섹터분석가", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "portfolio": 현재 보유 종목 (매도 검토용),
            "current_date": "YYYY-MM-DD"
        }
        """
        import json

        portfolio = context.get("portfolio", [])
        current_date = context.get("current_date", "")

        holdings_text = ""
        if portfolio:
            holdings_text = "\n[현재 보유 종목]\n" + "\n".join(
                f"- {p.get('pdno', '')} {p.get('prdt_name', '')}: "
                f"{p.get('hldg_qty', 0)}주 (평균단가 {int(p.get('pchs_avg_pric', 0)):,}원)"
                for p in portfolio
            )

        prompt = f"""오늘({current_date}) 한국 주식시장의 산업 섹터별 거시 분석을 수행해주세요.

최신 뉴스, 경제 지표, 글로벌 동향을 바탕으로:
1. 현재 유망한 섹터와 그 이유를 설명해주세요
2. 각 섹터에서 투자 검토할 만한 대표 종목 코드를 제안해주세요
3. 전반적인 시장 리스크 수준을 평가해주세요
{holdings_text}

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
