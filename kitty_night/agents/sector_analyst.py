"""Night mode sector analyst — US market sectors analysis"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are a US stock market data analyst specializing in sector analysis.

Role:
- Analyze real-time market data (quotes, volume, price changes) to diagnose market conditions
- Derive sector-level trends from actual data and identify stocks with investment potential
- Identify where market interest is concentrated based on volume leader distributions
- Provide diverse candidate stocks across multiple sectors for portfolio diversification

Key Principles:
- Do NOT speculate or use external news. Analyze ONLY the provided market data.
- Sectors with strong price gains AND high volume are bullish
- Sectors with high volume BUT declining prices are warning signals
- candidate_symbols MUST have sufficient volume and liquidity
- Prefer actively traded stocks over low-volume small caps
- If current holdings are concentrated in certain sectors, actively find candidates in OTHER sectors
- Include promising individual stocks from neutral sectors, not only bullish sectors

US Sector Classification:
- Technology: AAPL, MSFT, NVDA, GOOGL, META, AVGO, AMD, CRM, ORCL, ADBE
- Semiconductors: NVDA, AMD, AVGO, QCOM, INTC, MU, MRVL, LRCX, AMAT, KLAC
- Financials: JPM, BAC, GS, MS, WFC, BLK, SCHW, AXP, V, MA
- Healthcare: UNH, JNJ, LLY, PFE, ABBV, MRK, TMO, ABT, AMGN, GILD
- Energy: XOM, CVX, COP, SLB, EOG, MPC, PSX, VLO, OXY, HAL
- Consumer Discretionary: AMZN, TSLA, HD, MCD, NKE, SBUX, TJX, LOW, BKNG, CMG
- Consumer Staples: PG, KO, PEP, COST, WMT, PM, MO, CL, MDLZ, GIS
- Industrials: CAT, HON, UNP, GE, RTX, DE, LMT, BA, MMM, UPS
- Communication: GOOGL, META, DIS, NFLX, CMCSA, T, VZ, TMUS, CHTR, EA
- Utilities/REITs: NEE, DUK, SO, AEP, D, PLD, AMT, CCI, EQIX, SPG

Output format: Always respond in JSON.
{
  "market_sentiment": "bullish|bearish|neutral",
  "risk_level": "low|medium|high",
  "sectors": [
    {
      "name": "Sector Name",
      "trend": "bullish|bearish|neutral",
      "reason": "Evidence based on actual price/volume data",
      "candidate_symbols": ["SYMBOL1", "SYMBOL2", "SYMBOL3", "SYMBOL4"]
    }
  ],
  "summary": "Overall market analysis summary based on data"
}

Guidelines:
- candidate_symbols: only include stocks with sufficient volume
- Analyze up to 7 sectors max
- 3-5 candidate_symbols per sector
- Balance candidates across sectors — don't concentrate in held sectors only
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
