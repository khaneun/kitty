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

STEP 2: Risk Assessment
  - LOW: Market bullish + breadth strong (≥7 advancing) + avg change < +3%
  - MEDIUM: Mixed signals, moderate moves, or narrow breadth
  - HIGH: Market bearish + breadth weak (≤3 advancing) OR any stock ≥+8% or ≤-8% (extreme vol)

STEP 3: Sector Trend Diagnosis (use QUANTITATIVE criteria)
  - BULLISH: Sector's representative stocks show avg change > +0.5% AND volume is healthy
  - BEARISH: Sector's representative stocks show avg change < -0.5% AND selling volume elevated
  - NEUTRAL: Avg change between -0.5% and +0.5%, or insufficient data
  ※ Volume without price direction = uncertainty, NOT bullishness

STEP 4: Candidate Selection (QUALITY over quantity)
  - ONLY include stocks with volume ≥ 100,000 shares
  - Prefer stocks with positive price action in bullish sectors
  - In neutral sectors: include ONLY individual standouts (change_rate > +1%)
  - In bearish sectors: do NOT recommend candidates (empty list)
  - Prioritize sectors DIFFERENT from current holdings for diversification

━━━ SECTOR MAP ━━━
Technology: AAPL, MSFT, NVDA, GOOGL, META, AVGO, AMD, CRM, ORCL, ADBE
Semiconductors: NVDA, AMD, AVGO, QCOM, INTC, MU, MRVL, LRCX, AMAT, KLAC
Financials: JPM, BAC, GS, MS, WFC, BLK, SCHW, AXP, V, MA
Healthcare: UNH, JNJ, LLY, PFE, ABBV, MRK, TMO, ABT, AMGN, GILD
Energy: XOM, CVX, COP, SLB, EOG, MPC, PSX, VLO, OXY, HAL
Consumer Discretionary: AMZN, TSLA, HD, MCD, NKE, SBUX, TJX, LOW, BKNG, CMG
Consumer Staples: PG, KO, PEP, COST, WMT, PM, MO, CL, MDLZ, GIS
Industrials: CAT, HON, UNP, GE, RTX, DE, LMT, BA, MMM, UPS
Communication: GOOGL, META, DIS, NFLX, CMCSA, T, VZ, TMUS, CHTR, EA
Utilities/REITs: NEE, DUK, SO, AEP, D, PLD, AMT, CCI, EQIX, SPG

━━━ OUTPUT FORMAT (strict JSON) ━━━
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "market_breadth": {"advancing": N, "declining": N, "avg_change_pct": X.XX},
  "sectors": [
    {
      "name": "Sector Name",
      "trend": "bullish|bearish|neutral",
      "avg_change_pct": X.XX,
      "reason": "MUST cite specific stock prices/volumes as evidence",
      "candidate_symbols": ["SYM1", "SYM2", "SYM3"]
    }
  ],
  "summary": "2-3 sentence market diagnosis"
}

━━━ HARD RULES ━━━
- Analyze ONLY provided data. ZERO speculation or news-based judgment.
- Maximum 7 sectors. 3-5 candidates per bullish/neutral sector. 0 for bearish.
- Every "reason" MUST reference actual numbers (e.g., "NVDA +2.3% on 45M vol").
- Do NOT label a sector "bullish" if its stocks are flat or declining.
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
            }
        }
        """
        portfolio = context.get("portfolio", [])
        current_date = context.get("current_date", "")
        market_data = context.get("market_data", {})

        barometers = market_data.get("barometers", [])
        barometer_text = ""
        if barometers:
            advancing = sum(1 for q in barometers if q.get("change_rate", 0) > 0)
            declining = len(barometers) - advancing
            avg_change = sum(q.get("change_rate", 0) for q in barometers) / max(1, len(barometers))
            barometer_text = (
                f"\n[Market Barometers — Up:{advancing} Down:{declining} Avg:{avg_change:+.2f}%]\n"
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

        prompt = f"""Today ({current_date}) real-time US market data. Analyze market conditions and identify promising sectors/stocks.
{barometer_text}
{volume_text}
{holdings_text}

Analyze ONLY the data above. No speculation or news-based judgments.
Derive sector trends from price/volume patterns. Only select stocks with sufficient liquidity.
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
