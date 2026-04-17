"""Night mode sector analyst — US market sectors analysis"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Chief Market Strategist for a US stock automated trading system.
Your analysis directly drives buy/sell decisions — accuracy is critical.

━━━ MISSION ━━━
Diagnose real-time market conditions from provided data and identify the most promising
sectors and stocks for the current session. Your output feeds directly into stock picking
and portfolio evaluation agents.

━━━ ANALYSIS FRAMEWORK (follow in order) ━━━

STEP 1: Market Direction (SPY/QQQ barometers are your primary signal)
  - SPY + QQQ both positive → market_sentiment: "bullish"
  - SPY + QQQ both negative → market_sentiment: "bearish"
  - Mixed or flat (±0.3%) → market_sentiment: "neutral"
  - Breadth check: If ≥7/10 barometers are positive → confirms bullish; ≤3/10 → confirms bearish
  - VIX adjustment: VIX > 25 → downgrade sentiment one level (bullish→neutral, neutral→bearish)

STEP 2: Risk Assessment
  - LOW:    Market bullish + breadth strong (≥7 advancing) + VIX < 15
  - MEDIUM: Mixed signals, moderate moves, or VIX 15-25
  - HIGH:   Market bearish + breadth weak (≤3 advancing) OR VIX > 25 OR any stock ≥+8%/≤-8%

STEP 3: Sector Trend Diagnosis — USE SECTOR ETF DATA FIRST
  The SPDR Sector ETF change_rate is the definitive sector signal:
  - BULLISH:  ETF change_rate > +0.3%  (sector is rising today)
  - BEARISH:  ETF change_rate < -0.3%  (sector is falling today)
  - NEUTRAL:  ETF change_rate between -0.3% and +0.3%

  Cross-check with individual stock barometers for confirmation.
  ※ When ETF data is available, ALWAYS use it as the primary signal.

STEP 4: Candidate Selection (QUALITY over quantity)
  - From bullish sectors: pick 3-5 representative stocks from the SECTOR MAP below
  - From neutral sectors: pick 2-3 individual standouts (barometer change_rate > +0.5%)
  - From bearish sectors: NO candidates (empty list)
  - Always include candidates from bullish/neutral sectors even without individual stock data
    (use the sector map to select representative high-cap stocks)
  - Prioritize sectors DIFFERENT from current holdings for diversification

━━━ SECTOR MAP ━━━
Technology (XLK):    AAPL, MSFT, NVDA, GOOGL, META, AVGO, AMD, CRM, ORCL, ADBE
Financials (XLF):    JPM, BAC, GS, MS, WFC, BLK, SCHW, AXP, V, MA
Healthcare (XLV):    UNH, JNJ, LLY, PFE, ABBV, MRK, TMO, ABT, AMGN, GILD
Energy (XLE):        XOM, CVX, COP, SLB, EOG, MPC, PSX, VLO, OXY, HAL
Consumer Disc (XLY): AMZN, TSLA, HD, MCD, NKE, SBUX, TJX, LOW, BKNG, CMG
Consumer Stpl (XLP): PG, KO, PEP, COST, WMT, PM, MO, CL, MDLZ, GIS
Industrials (XLI):   CAT, HON, UNP, GE, RTX, DE, LMT, BA, MMM, UPS
Communication (XLC): GOOGL, META, DIS, NFLX, CMCSA, T, VZ, TMUS, CHTR, EA
Materials (XLB):     LIN, APD, ECL, SHW, FCX, NEM, NUE, VMC, MLM, IFF
Utilities (XLU):     NEE, DUK, SO, AEP, D, EXC, SRE, PCG, AEE, CMS
Real Estate (XLRE):  PLD, AMT, CCI, EQIX, SPG, O, WELL, DLR, PSA, AVB

━━━ OUTPUT FORMAT (strict JSON) ━━━
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "vix_note": "VIX 18.5 (medium risk)" or "N/A",
  "market_breadth": {"advancing": N, "declining": N, "avg_change_pct": X.XX},
  "sectors": [
    {
      "name": "Sector Name",
      "etf": "XLK",
      "etf_change_pct": X.XX,
      "trend": "bullish|bearish|neutral",
      "reason": "XLK +1.2% confirms tech bullish; NVDA +2.1% on barometer",
      "candidate_symbols": ["SYM1", "SYM2", "SYM3"]
    }
  ],
  "summary": "2-3 sentence market diagnosis including VIX level"
}

━━━ HARD RULES ━━━
- Analyze ONLY provided data. ZERO speculation or news-based judgment.
- Maximum 7 sectors. 3-5 candidates per bullish sector, 2-3 per neutral, 0 for bearish.
- When sector ETF data is available, its change_rate IS the sector trend — do not override it.
- Always populate candidate_symbols for bullish/neutral sectors using the SECTOR MAP.
- Bearish sectors get NO candidate_symbols — downstream agents must not buy into weakness.
"""


class NightSectorAnalystAgent(NightBaseAgent):
    def __init__(self) -> None:
        super().__init__(name="NightSectorAnalyst", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        context:
        {
            "portfolio": current holdings,
            "current_date": "YYYY-MM-DD",
            "market_data": {
                "barometers": [quote dicts],
                "volume_leaders": [volume rank dicts],
                "sector_etfs": [{"symbol","sector","price","change_rate"}, ...],  # Yahoo Finance
                "vix": {"value": 18.5, "level": "medium"},
            }
        }
        """
        portfolio = context.get("portfolio", [])
        current_date = context.get("current_date", "")
        market_data = context.get("market_data", {})

        # ── SPDR 섹터 ETF (Yahoo Finance) ─────────────────────────────────────
        sector_etfs = market_data.get("sector_etfs", [])
        sector_etf_text = ""
        if sector_etfs:
            sorted_etfs = sorted(sector_etfs, key=lambda x: x.get("change_rate", 0), reverse=True)
            sector_etf_text = "\n[SPDR Sector ETF Performance — PRIMARY SECTOR SIGNAL]\n" + "\n".join(
                f"  {e['symbol']} ({e['sector']}): ${e['price']:.2f} "
                f"({'▲' if e['change_rate'] >= 0 else '▼'}{abs(e['change_rate']):.2f}%)"
                for e in sorted_etfs
            )

        # ── VIX 공포지수 ────────────────────────────────────────────────────────
        vix = market_data.get("vix", {})
        vix_text = ""
        if vix.get("value"):
            vix_text = (
                f"\n[VIX Fear Index]: {vix['value']:.1f} "
                f"({'HIGH RISK — reduce position sizes' if vix['level'] == 'high' else 'medium risk' if vix['level'] == 'medium' else 'low risk'})"
            )

        # ── KIS 바로미터 (개별 주식 실시간 시세) ──────────────────────────────
        barometers = market_data.get("barometers", [])
        barometer_text = ""
        if barometers:
            advancing = sum(1 for q in barometers if q.get("change_rate", 0) > 0)
            declining = len(barometers) - advancing
            avg_change = sum(q.get("change_rate", 0) for q in barometers) / max(1, len(barometers))
            barometer_text = (
                f"\n[Individual Stock Barometers — Up:{advancing} Down:{declining} Avg:{avg_change:+.2f}%]\n"
                + "\n".join(
                    f"  {q.get('name', '')}({q['symbol']}): "
                    f"${q.get('current_price', 0):,.2f} ({q.get('change_rate', 0):+.2f}%) "
                    f"Vol:{q.get('volume', 0):,}"
                    for q in barometers
                )
            )

        volume_leaders = market_data.get("volume_leaders", [])
        volume_text = ""
        if volume_leaders:
            volume_text = "\n[Volume Leaders]\n" + "\n".join(
                f"  {i + 1}. {v.get('name', '')}({v['symbol']}): "
                f"${v.get('current_price', 0):,.2f} ({v.get('change_rate', 0):+.2f}%) "
                f"Vol:{v.get('volume', 0):,} Turnover:${v.get('turnover', 0):,.0f}"
                for i, v in enumerate(volume_leaders)
            )

        holdings_text = ""
        if portfolio:
            holdings_text = "\n[Current Holdings]\n" + "\n".join(
                f"  {p.get('symbol', '')} {p.get('name', '')}: "
                f"{p.get('quantity', 0)} shares (avg ${p.get('avg_price', 0):,.2f})"
                for p in portfolio
            )

        prompt = f"""Today ({current_date}) real-time US market data. Analyze sector conditions and identify promising stocks.
{sector_etf_text}{vix_text}
{barometer_text}{volume_text}{holdings_text}

Use SECTOR ETF data as the primary signal for sector trends.
Populate candidate_symbols for all bullish/neutral sectors using the SECTOR MAP.
Respond in JSON format."""

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
