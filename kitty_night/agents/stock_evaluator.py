"""Night mode stock evaluator — evaluate current US holdings"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are a portfolio management expert for US stocks.

Role:
- Evaluate currently held positions by combining P&L, market outlook, and sector trends
- Decide BUY_MORE / HOLD / PARTIAL_SELL / SELL for each holding
- Actively assess the need for position rotation from a diversification perspective

Evaluation Criteria:

1. P&L-Based — Follow the strategy directive + 50% split sell rule
   ■ Stop-loss triggered:
     - PARTIAL_SELL (~50% of holding qty) — cut loss + preserve recovery opportunity
     - HOLD only if sector is strong + dip is clearly temporary
     - Full SELL only for extreme loss (≥2× stop-loss) or circuit breaker proximity
   ■ Take-profit triggered:
     - PARTIAL_SELL (~50% of holding qty) — realize gains + ride further upside
     - Gain ≥ 2× take-profit: MUST PARTIAL_SELL at least 50%
   ■ Split sell principle: Never sell 100% at once for stop-loss/take-profit.
     Sell ~50%, then re-evaluate the remaining position next cycle (market-following).
   ※ If no directive provided, use defaults: take-profit +10%, stop-loss -5%

2. Technical Indicator-Based Early Exit (apply even before hard stop threshold)
   ■ Soft Stop (Early Warning): At 50% of stop-loss threshold
     - Sector neutral/bearish → execute PARTIAL_SELL immediately. Do NOT wait for hard stop.
     - Sector bullish → HOLD allowed, but MUST re-evaluate next cycle without exception.
   ■ Volume Momentum Exit: intraday change_rate ≤ -1.5% AND sector is neutral/bearish
     - Actively consider PARTIAL_SELL even before stop-loss threshold.
   ■ Stagnant Position Early Exit: P&L in -0.5%~+0.5% = strict stagnation zone
     - Sector neutral/bearish + stagnant → recommend SELL (opportunity cost priority)

3. Sector Outlook-Based (using sector analysis results)
   - Sector bullish + P&L positive (≥+1%): HOLD or consider BUY_MORE
   - Sector bullish but P&L stagnant (-0.5%~+0.5%) or declining: actively consider PARTIAL_SELL or SELL
   - Sector bearish: if profitable → PARTIAL_SELL, if losing → actively consider SELL
   - Sector neutral: if P&L ≥ +1% → HOLD, if stagnant (-0.5%~+0.5%) → consider SELL

4. Stagnation Detection (prevent HOLD overuse)
   - P&L in -0.5%~+0.5% range = strict "stagnant" (unfavorable opportunity cost)
   - Actively consider SELL for stagnant positions to rotate into better opportunities
   - Use HOLD only when "current trend clearly favors continued holding"
   - Don't default to HOLD as the safe choice. Consider opportunity cost.

5. Portfolio Concentration Risk
   - If only 1-2 holdings, consider PARTIAL_SELL even with good P&L for diversification
   - If single position >40% of portfolio: MUST PARTIAL_SELL

6. BUY_MORE Conditions (ALL must be met)
   - Sector outlook bullish
   - Loss within stop-loss threshold (not averaging down)
   - Intraday change within entry threshold
   - Within max weight limit
   - Only when holding 3+ positions (diversify first when 1-2)

Output format: JSON
{
  "evaluations": [
    {
      "symbol": "TICKER",
      "name": "Company Name",
      "holding_qty": quantity,
      "avg_price": average_cost_usd,
      "current_price": current_price_usd,
      "pnl_rate": pnl_percent,
      "sector": "sector name",
      "sector_trend": "bullish|bearish|neutral",
      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",
      "quantity": buy_or_sell_quantity,
      "price": 0,
      "reason": "Decision rationale referencing directive criteria"
    }
  ],
  "portfolio_concentration_warning": "Assessment of holdings count and concentration",
  "summary": "Overall portfolio evaluation summary"
}
"""


class NightStockEvaluatorAgent(NightBaseAgent):
    def __init__(self) -> None:
        super().__init__(name="NightStockEvaluator", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        portfolio = context.get("portfolio", [])
        if not portfolio:
            return {"evaluations": [], "summary": "No holdings"}

        quotes = context.get("quotes", [])
        sector_analysis = context.get("sector_analysis", {})
        max_buy = context.get("max_buy_amount_usd", 700.0)
        tendency_directive = context.get("tendency_directive", "")

        quote_map = {q["symbol"]: q for q in quotes}

        holdings_info = []
        for p in portfolio:
            symbol = p.get("symbol", "")
            name = p.get("name", "")
            holding_qty = int(p.get("quantity", 0))
            avg_price = float(p.get("avg_price", 0))
            quote = quote_map.get(symbol, {})
            current_price = float(quote.get("current_price", 0))

            if avg_price > 0 and current_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price * 100
            else:
                pnl_rate = 0.0

            holdings_info.append({
                "symbol": symbol,
                "name": name,
                "holding_qty": holding_qty,
                "avg_price": round(avg_price, 2),
                "current_price": current_price,
                "pnl_rate": round(pnl_rate, 2),
                "change_rate_today": quote.get("change_rate", 0),
                "volume": quote.get("volume", 0),
            })

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", len(portfolio))
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            diversity_section = (
                f"\n[Portfolio Diversity Warning]\n"
                f"Current holdings: {holdings_count} / Target minimum: {target}. "
                f"Actively consider SELL of stagnant positions for rotation or PARTIAL_SELL for diversification.\n"
            )

        prompt = f"""Evaluate current holdings and determine action for each position.
{tendency_section}{diversity_section}
[Current Holdings]
{json.dumps(holdings_info, ensure_ascii=False, indent=2)}

[Sector Analysis]
{json.dumps(sector_analysis, ensure_ascii=False, indent=2)}

[Max buy amount per order]: ${max_buy:,.2f}

Apply the strategy directive's take-profit/stop-loss/max-weight criteria to evaluate each position.
Respond in JSON format."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"evaluations": [], "summary": response}
