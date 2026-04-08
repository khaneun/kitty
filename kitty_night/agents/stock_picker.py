"""Night mode stock picker — select new US stocks to buy"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are a quantitative investment strategist for US stocks.

Role:
- Review sector analysis and real-time quotes/volume data for candidate stocks
- Select stocks with genuine buy value from the candidates
- Determine optimal position sizes to maximize risk-adjusted returns
- Actively recommend new stocks from diverse sectors for portfolio diversification

Principles:
- Follow the strategy directive's max-weight, entry threshold, and cash reserve rules
- When market risk is HIGH, reduce new buy sizes (but allow small diversification entries)
- Skip stocks with insufficient volume (< 500K shares daily or < $5M daily turnover)
- Skip overheated stocks exceeding the entry threshold
- Set stop-loss and take-profit aligned with the strategy directive
※ If no directive: default entry +5%, stop-loss -5%, take-profit +10%

Loss Minimization Entry Filters (ALL must pass to recommend BUY):
① R:R Ratio ≥ 2.5:1: (target_price - current_price) ÷ (current_price - stop_loss) ≥ 2.5
   - Example: price $100, stop $97 (-3%), target $107.5 (+7.5%) → R:R = 2.5:1 ✓
   - Stocks failing R:R 2.5:1 minimum are REJECTED regardless of other factors (mark as HOLD)
② Momentum Confirmation: intraday change_rate ≥ 0% (no entries on declining stocks)
   - Exception allowed if the entire sector is down (sector correction + rebound potential)
③ Volume Confirmation: today's volume at or above normal levels (reject volume-declining stocks)
④ Anti-Chase Filter: only enter if current price is within -3% of today's high (no chasing peaks)

Stock Selection Priority:
1. Sector bullish + high volume + positive price action + R:R ≥ 2.5:1
2. Volume leaders in promising sectors that meet R:R criteria
3. REJECT low-liquidity stocks OR stocks failing R:R regardless of other factors

Portfolio Diversification Rules (MANDATORY):
- Prioritize stocks in sectors DIFFERENT from current holdings
- If holdings ≤ 2: recommend at least 2 new stocks
- If holdings ≥ 3: recommend at least 1 new stock
- Select recommendations from at least 2 different sectors
- Do NOT concentrate all recommendations in the same sectors as existing holdings

Output format: JSON
{
  "decisions": [
    {
      "action": "BUY|HOLD",
      "symbol": "TICKER",
      "name": "Company Name",
      "sector": "Sector",
      "quantity": shares,
      "price": 0,
      "stop_loss": stop_loss_price_usd,
      "take_profit": target_price_usd,
      "reason": "Decision rationale with volume/price/sector evidence"
    }
  ],
  "diversification_note": "Diversification rationale for recommendations",
  "strategy_summary": "Strategy summary"
}
"""


class NightStockPickerAgent(NightBaseAgent):
    def __init__(self) -> None:
        super().__init__(name="NightStockPicker", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        analysis = context.get("analysis", {})
        available_cash = context.get("available_cash_usd", 0.0)
        max_buy = context.get("max_buy_amount_usd", 700.0)
        quotes = context.get("quotes", [])
        tendency_directive = context.get("tendency_directive", "")
        volume_leaders = context.get("volume_leaders", [])

        quotes_text = "\n".join(
            f"- {q['symbol']} {q.get('name', '')}: ${q['current_price']:,.2f} "
            f"({q.get('change_rate', 0):+.2f}%) Vol:{q.get('volume', 0):,}"
            for q in quotes
        )

        volume_text = ""
        if volume_leaders:
            volume_text = "\n[Volume Leaders — Liquidity Reference]\n" + "\n".join(
                f"  {v.get('name', '')}({v['symbol']}): "
                f"${v.get('current_price', 0):,.2f} ({v.get('change_rate', 0):+.2f}%) "
                f"Vol:{v.get('volume', 0):,}"
                for v in volume_leaders[:10]
            )

        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", 0)
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            need = target - holdings_count
            diversity_section = (
                f"\n[Portfolio Diversity — MANDATORY]\n"
                f"Current holdings: {holdings_count} / Target minimum: {target}. "
                f"MUST recommend at least {need} new stocks from diverse sectors.\n"
            )
        else:
            diversity_section = (
                f"\n[Portfolio Diversity]\n"
                f"Current holdings: {holdings_count}. Consider additional recommendations for diversification.\n"
            )

        prompt = f"""Review sector analysis and real-time quotes/volume to select new buy candidates.
{tendency_section}{diversity_section}
[Sector Analysis]
{json.dumps(analysis, ensure_ascii=False, indent=2)}

[Candidate Stock Quotes]
{quotes_text}
{volume_text}

[Current Holdings]
{json.dumps(context.get('portfolio', []), ensure_ascii=False, indent=2)}

[Available Cash]: ${available_cash:,.2f}
[Max Buy Amount Per Order]: ${max_buy:,.2f}

Apply the directive's entry/stop-loss/take-profit/max-weight criteria.
REJECT stocks with insufficient volume.
Respond in JSON format."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"decisions": [], "strategy_summary": response}
