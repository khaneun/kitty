"""종목스크리너 에이전트 — KOSPI/KOSDAQ 전종목 대상 섹터·기술 필터링"""
import json
from typing import Any

from .base import BaseAgent

SYSTEM_PROMPT = """당신은 한국 주식시장 전종목 스크리닝 전문가입니다.

역할:
- 섹터분석가가 선별한 유망 섹터를 기준으로, KOSPI + KOSDAQ 전 시장에서 매수 후보를 발굴합니다
- 제공된 시장 데이터(거래량·등락률 상위 종목)에서 유망 섹터에 속하는 종목을 선별합니다
- AI의 사전 지식이 아닌, 제공된 실제 KIS 시장 데이터만을 근거로 종목을 선택합니다

섹터 매칭 원칙:
- KIS 업종명(industry)과 섹터분석가의 섹터명은 표현이 다를 수 있습니다 (예: "반도체" ↔ "반도체/전자")
- 부분 일치, 의미 유사성으로 매칭하세요 (예: "제약" ↔ "바이오/의료")
- bullish 섹터 종목을 최우선, neutral 섹터 종목은 차선으로 선별합니다
- bearish 섹터 종목은 제외합니다 (개별 종목이 강해도 섹터 역풍은 위험)

사전 필터 (코드로 처리 전 AI가 최종 검토):
- 거래대금 10억 미만 → 제외 (유동성 부족)
- 당일 등락률 +15% 이상 (상한가 근접) → 제외 (추격 매수 위험)
- 당일 등락률 -5% 이하 → 제외 (강한 하락세)
- 이미 보유 중인 종목 → 제외 (중복 투자 방지, 보유 종목 평가는 종목평가가 담당)

선별 우선순위:
1. bullish 섹터 + 등락률 양봉(0~+10%) + 거래대금 충분
2. bullish 섹터 + 거래량 급증 + 소폭 음봉(-2% 이내, 섹터 내 상대 강도 양호)
3. neutral 섹터 + 거래량·등락률 모두 양호

목표 선별 수: 20~40개 (종목발굴가가 최종 선택할 충분한 풀 제공)

출력 형식: JSON
{
  "screened": [
    {
      "symbol": "종목코드",
      "name": "종목명",
      "industry": "KIS 업종명",
      "matched_sector": "매칭된 섹터분석가 섹터명",
      "sector_trend": "bullish|neutral",
      "market": "KOSPI|KOSDAQ",
      "change_rate": 등락률(float),
      "turnover": 거래대금(int),
      "reason": "선별 이유 (업종 매칭 근거 + 기술적 특징)"
    }
  ],
  "summary": "스크리닝 요약: 총 검토 N개 → 선별 M개 (bullish K개 / neutral L개)"
}
"""


class StockScreenerAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(name="종목스크리너", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "sector_analysis": SectorAnalystAgent 결과 (sectors 포함),
            "market_pool": [  ← KOSPI + KOSDAQ 거래량·등락률 상위 종목 통합
                {"symbol", "name", "industry", "market", "change_rate", "turnover", ...}
            ],
            "holdings_symbols": ["보유중인 종목코드", ...]  ← 중복 제외용
        }
        """
        sector_analysis = context.get("sector_analysis", {})
        market_pool = context.get("market_pool", [])
        holdings_symbols = set(context.get("holdings_symbols", []))

        # ── 코드 레벨 사전 필터 (토큰 절약 + 명확한 기준 적용) ──────────────
        pre_filtered = []
        for stock in market_pool:
            sym = stock.get("symbol", "")
            if sym in holdings_symbols:
                continue
            cr = float(stock.get("change_rate", 0))
            turnover = int(stock.get("turnover", 0))
            if turnover < 1_000_000_000:   # 거래대금 10억 미만 제외
                continue
            if cr > 14.5 or cr < -5.0:    # 상한가 근접 또는 급락 제외
                continue
            pre_filtered.append(stock)

        # 중복 심볼 제거 (KOSPI + KOSDAQ 통합 시 같은 종목이 양쪽에 나올 수 있음)
        seen: set[str] = set()
        unique_pool: list[dict] = []
        for s in sorted(pre_filtered, key=lambda x: -x.get("turnover", 0)):
            sym = s.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                unique_pool.append(s)

        # 상위 100개만 AI에게 전달 (토큰 효율)
        pool_for_ai = unique_pool[:100]

        # ── 섹터 정보 요약 (bullish/neutral만) ────────────────────────────
        sectors_summary = []
        for sec in sector_analysis.get("sectors", []):
            if sec.get("trend") in ("bullish", "neutral"):
                sectors_summary.append({
                    "name":  sec.get("name", ""),
                    "trend": sec.get("trend", ""),
                })

        pool_text = "\n".join(
            f"  [{s.get('market','?')}] {s['symbol']} {s.get('name','')} | "
            f"업종:{s.get('industry','')} | 등락:{s.get('change_rate',0):+.2f}% | "
            f"거래대금:{s.get('turnover',0)//100_000_000:.0f}억"
            for s in pool_for_ai
        )

        prompt = f"""아래는 오늘 KOSPI + KOSDAQ 전 시장에서 거래대금·등락률 상위 종목 풀입니다.
섹터분석가의 유망 섹터와 매칭되는 종목을 선별하세요.

[유망 섹터 (매칭 대상)]
{json.dumps(sectors_summary, ensure_ascii=False, indent=2)}

[시장 풀 — 총 {len(pool_for_ai)}개 (거래대금 상위순, 사전 필터 통과)]
{pool_text}

위 풀에서 유망 섹터에 속하는 종목을 선별하세요.
업종명(industry)이 정확히 일치하지 않아도 의미상 유사하면 매칭으로 간주하세요.
목표: 20~40개 후보 (종목발굴가가 최종 선택할 충분한 풀).
JSON 형식으로 응답하세요."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            result = json.loads(response[start:end])
            # screened 결과가 없으면 pool_for_ai 상위 30개로 폴백
            if not result.get("screened"):
                result["screened"] = [
                    {
                        "symbol":        s["symbol"],
                        "name":          s.get("name", ""),
                        "industry":      s.get("industry", ""),
                        "matched_sector": "",
                        "sector_trend":  "neutral",
                        "market":        s.get("market", ""),
                        "change_rate":   s.get("change_rate", 0),
                        "turnover":      s.get("turnover", 0),
                        "reason":        "폴백: AI 응답 파싱 실패",
                    }
                    for s in pool_for_ai[:30]
                ]
            return result
        except Exception:
            return {
                "screened": [
                    {
                        "symbol":        s["symbol"],
                        "name":          s.get("name", ""),
                        "industry":      s.get("industry", ""),
                        "matched_sector": "",
                        "sector_trend":  "neutral",
                        "market":        s.get("market", ""),
                        "change_rate":   s.get("change_rate", 0),
                        "turnover":      s.get("turnover", 0),
                        "reason":        "폴백",
                    }
                    for s in pool_for_ai[:30]
                ],
                "summary": f"파싱 실패 — 폴백 {min(30, len(pool_for_ai))}개 반환",
            }
