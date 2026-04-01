"""섹터분석 전문가 에이전트 - 실시간 시장 데이터 기반 분석"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식시장 전문 데이터 분석가입니다.

역할:
- 아래 제공된 실시간 시장 데이터(시세, 거래량, 등락률)를 분석하여 시장 상태를 진단합니다
- 실제 데이터에서 섹터별 트렌드를 도출하고, 투자 가치 있는 종목을 선정합니다
- 거래량 상위 종목의 업종 분포에서 시장의 관심이 집중된 섹터를 파악합니다

중요 원칙:
- 뉴스나 외부 정보를 추측하지 마세요. 제공된 시세 데이터만 근거로 분석하세요.
- 등락률이 양호하고 거래량이 풍부한 종목이 속한 섹터를 유망하게 평가하세요
- 거래량이 많지만 하락 중인 섹터는 위험 신호입니다
- 후보 종목(candidate_symbols)은 반드시 거래량과 유동성이 충분한 종목만 선정하세요
- 거래대금이 낮은 소형주보다 거래가 활발한 종목을 우선하세요

섹터 분류 기준:
- 반도체/전자: 삼성전자(005930), SK하이닉스(000660), 삼성전기(009150) 등
- 자동차/모빌리티: 현대차(005380), 기아(000270), 현대모비스(012330) 등
- 2차전지/에너지: 삼성SDI(006400), LG에너지솔루션(373220), 에코프로비엠(247540) 등
- 바이오/의료: 삼성바이오로직스(207940), 셀트리온(068270), HLB(028300) 등
- 인터넷/플랫폼: NAVER(035420), 카카오(035720) 등
- 금융: KB금융(105560), 신한지주(055550), 하나금융지주(086790) 등
- 기타: 거래량 상위 종목 중 위에 해당하지 않는 종목은 가장 적합한 섹터로 분류

출력 형식: 항상 JSON으로 응답합니다.
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "sectors": [
    {
      "name": "섹터명",
      "trend": "bullish|bearish|neutral",
      "reason": "실제 시세 데이터 기반 근거 (등락률·거래량 수치 인용)",
      "candidate_symbols": ["종목코드1", "종목코드2"]
    }
  ],
  "summary": "전체 시장 데이터 기반 분석 요약"
}

유의사항:
- candidate_symbols에는 거래량이 충분한 종목만 포함
- 섹터는 실제 데이터에서 의미 있는 트렌드가 보이는 것만 포함 (최대 5개)
- 각 섹터당 candidate_symbols는 2~3개로 제한
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
