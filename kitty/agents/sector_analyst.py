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

3단계: 섹터 트렌드 진단 — KRX 업종 지수를 PRIMARY 신호로 사용
  KRX 업종 지수 등락률이 해당 섹터의 확정 신호입니다:
  - BULLISH: 업종 지수 등락률 > +0.3%
  - BEARISH: 업종 지수 등락률 < -0.3%
  - NEUTRAL:  업종 지수 등락률 -0.3% ~ +0.3%

  개별 종목 바로미터와 거래량 데이터로 교차 확인.
  ※ KRX 업종 지수 데이터가 있을 때는 반드시 이를 PRIMARY 신호로 사용.

4단계: 외국인/기관 순매수 가중치 적용
  - 외국인 or 기관 순매수 상위 종목이 속한 섹터 → bullish/neutral 판정 시 가중치 부여
  - 순매수 상위 종목은 candidate_symbols 우선순위 상단에 배치

5단계: 후보 종목 선정 (질 > 양)
  - 거래량 10만주 이상, 거래대금 10억 이상 종목만 포함
  - bullish 섹터: 상승 중인 종목 3~5개 (외국인/기관 순매수 종목 우선)
  - neutral 섹터: 개별 강세 종목(등락률 +1% 이상)만 포함
  - bearish 섹터: 후보 없음 (빈 리스트)
  - 현재 보유 종목과 다른 섹터를 우선 발굴 (분산투자)
  - KRX 데이터가 없더라도 섹터 분류표에서 대표 종목을 후보로 포함

━━━ 섹터 분류 ━━━
반도체/전자: 삼성전자(005930), SK하이닉스(000660), 삼성전기(009150) 등
자동차/모빌리티: 현대차(005380), 기아(000270), 현대모비스(012330) 등
2차전지/화학: 삼성SDI(006400), LG에너지솔루션(373220), 에코프로비엠(247540), LG화학(051910) 등
바이오/의약품: 삼성바이오로직스(207940), 셀트리온(068270), 유한양행(000100) 등
의료정밀: HLB(028300), 레고켐바이오(141080) 등
인터넷/서비스: NAVER(035420), 카카오(035720) 등
금융: KB금융(105560), 신한지주(055550), 하나금융지주(086790) 등
건설/인프라: 현대건설(000720), 대우건설(047040) 등

━━━ 출력 형식 (엄격 JSON) ━━━
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "market_breadth": {"advancing": N, "declining": N, "avg_change_pct": X.XX},
  "sectors": [
    {
      "name": "섹터명",
      "krx_change_pct": X.XX,
      "trend": "bullish|bearish|neutral",
      "reason": "KRX 업종지수 +1.2% / 외국인 순매수 삼성전자 500억원 / SK하이닉스 +2.1%",
      "candidate_symbols": ["종목코드1", "종목코드2", "종목코드3"]
    }
  ],
  "summary": "2~3문장 시장 진단 요약 (KRX 업종 지수 방향 포함)"
}

━━━ 절대 규칙 ━━━
- 제공된 데이터만 분석. 추측·뉴스 기반 판단 절대 금지.
- 최대 7개 섹터. bullish/neutral 섹터당 3~5개 후보. bearish 섹터는 후보 0개.
- KRX 업종 지수 데이터가 있으면 그 등락률이 섹터 트렌드의 확정 신호 — 개별 종목 신호로 재정의하지 마세요.
- "reason"에 반드시 실제 수치 인용 (예: "KRX 전기전자 지수 +1.8%, 삼성전자 +2.1%").
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
                "sector_indices": [{"ticker","sector","close","change_rate","date"}, ...],  # KRX
                "foreign_net":    [{"symbol","net_buy_krw"}, ...],                         # KRX
                "inst_net":       [{"symbol","net_buy_krw"}, ...],                         # KRX
                "kr_market_date": "20260417",
            }
        }
        """
        portfolio = context.get("portfolio", [])
        current_date = context.get("current_date", "")
        market_data = context.get("market_data", {})

        # ── KRX 업종 지수 (PRIMARY 섹터 신호) ─────────────────────────────────
        sector_indices = market_data.get("sector_indices", [])
        kr_market_date = market_data.get("kr_market_date", "")
        sector_idx_text = ""
        if sector_indices:
            sorted_idx = sorted(sector_indices, key=lambda x: x.get("change_rate", 0), reverse=True)
            sector_idx_text = (
                f"\n[KRX 업종 지수 — PRIMARY 섹터 신호 (기준일: {kr_market_date})]\n"
                + "\n".join(
                    f"  {s['sector']}: {'▲' if s['change_rate'] >= 0 else '▼'}"
                    f"{abs(s['change_rate']):.2f}%  (종가 {s['close']:,})"
                    for s in sorted_idx
                )
            )

        # ── 외국인 순매수 상위 ─────────────────────────────────────────────────
        foreign_net = market_data.get("foreign_net", [])
        foreign_text = ""
        if foreign_net:
            foreign_text = "\n[외국인 순매수 상위 (KOSPI)]\n" + "\n".join(
                f"  {i + 1}. {f['symbol']}: {f['net_buy_krw'] / 1e8:,.0f}억원"
                for i, f in enumerate(foreign_net[:10])
            )

        # ── 기관 순매수 상위 ──────────────────────────────────────────────────
        inst_net = market_data.get("inst_net", [])
        inst_text = ""
        if inst_net:
            inst_text = "\n[기관 순매수 상위 (KOSPI)]\n" + "\n".join(
                f"  {i + 1}. {f['symbol']}: {f['net_buy_krw'] / 1e8:,.0f}억원"
                for i, f in enumerate(inst_net[:10])
            )

        # ── 시장 지표 (ETF + 대형주) ────────────────────────────────────────
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

        # ── 거래량 상위 종목 ───────────────────────────────────────────────
        volume_leaders = market_data.get("volume_leaders", [])
        volume_text = ""
        if volume_leaders:
            volume_text = "\n[거래량 상위 종목]\n" + "\n".join(
                f"  {i + 1}. {v.get('name', '')}({v['symbol']}): "
                f"{v.get('current_price', 0):,}원 ({v.get('change_rate', 0):+.2f}%) "
                f"거래량:{v.get('volume', 0):,} 거래대금:{v.get('turnover', 0):,}"
                for i, v in enumerate(volume_leaders)
            )

        # ── 보유 종목 ─────────────────────────────────────────────────────
        holdings_text = ""
        if portfolio:
            holdings_text = "\n[현재 보유 종목]\n" + "\n".join(
                f"  {p.get('pdno', '')} {p.get('prdt_name', '')}: "
                f"{p.get('hldg_qty', 0)}주 (평균단가 {int(float(p.get('pchs_avg_pric', 0))):,}원)"
                for p in portfolio
            )

        prompt = f"""오늘({current_date}) 실시간 시장 데이터입니다. 이 데이터를 분석하여 시장 상태를 진단하고 유망 섹터·종목을 선정하세요.
{sector_idx_text}{foreign_text}{inst_text}
{barometer_text}
{volume_text}
{holdings_text}

KRX 업종 지수를 PRIMARY 섹터 신호로 사용하세요.
외국인/기관 순매수 상위 종목이 속한 섹터에 가중치를 부여하세요.
bullish/neutral 섹터는 섹터 분류표에서 후보 종목을 반드시 포함하세요.
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
