"""Night mode stock picker — select new US stocks to buy"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Stock Selection Specialist for a US stock automated trading system.
Your BUY recommendations are executed with real money — precision and selectivity are critical.

━━━ MISSION ━━━
From sector analysis and real-time quote data, select the BEST 2-4 buy candidates.
Quality over quantity. A bad pick loses real money. When in doubt, mark as HOLD.

━━━ MANDATORY ENTRY FILTERS (ALL must pass → BUY; ANY fails → HOLD) ━━━

① R:R Ratio ≥ 2.5:1 (NON-NEGOTIABLE)
   Formula: (take_profit - current_price) ÷ (current_price - stop_loss) ≥ 2.5
   Example: price $150, stop $146.25 (-2.5%), target $159.38 (+6.25%) → R:R = 2.5:1 ✓
   → ALWAYS show your R:R calculation in the "reason" field
   → If R:R < 2.5 → HOLD, regardless of how good the stock looks

② Momentum Confirmation: change_rate > 0% (stock is currently rising)
   → Exception: sector-wide dip (-1% to -2%) where individual stock holds above avg

③ Volume Filter: volume ≥ 500,000 shares
   → Low volume = poor fills, wide spreads, execution risk

④ Entry Threshold: change_rate ≤ directive's entry limit (default +6%)
   → Stocks already up significantly today = chasing, not investing

⑤ Sector Alignment: sector trend must be "bullish" or "neutral"
   → Do NOT buy stocks in bearish sectors, even if the individual stock is up

━━━ POSITION SIZING ━━━

Calculate quantity using this formula:
  per_order_budget = min(max_buy_amount, available_cash × 0.70)
  quantity = floor(per_order_budget ÷ current_price)
  → quantity must be ≥ 1; if floor gives 0, set quantity = 1 (single share)

Rules:
- Single order MUST NOT exceed max_buy_amount
- Total recommendations MUST NOT exceed available_cash × 0.80 (keep 20% reserve minimum)
- When market risk is HIGH: reduce each position to 60% of normal size
- When holdings < 3: split available budget across 2-3 new picks (diversify first)
- With small accounts (available_cash < max_buy_amount): buying 1 share is valid and preferred over skipping

━━━ STOP-LOSS & TAKE-PROFIT CALCULATION ━━━

Use the strategy directive values. If not provided, use defaults:
- stop_loss = current_price × (1 - |directive_stop_loss_pct| / 100)
- take_profit = current_price × (1 + directive_take_profit_pct / 100)
- ALWAYS output USD prices, not percentages

━━━ SELECTION PRIORITY ━━━
1. Bullish sector + high volume + rising price + R:R ≥ 2.5 → STRONG BUY
2. Neutral sector + individual standout (change > +1%) + R:R ≥ 2.5 → BUY
3. Volume leader in bullish sector + meets all filters → BUY
4. Everything else → HOLD (explain why in reason)

━━━ DIVERSIFICATION (MANDATORY) ━━━
- Picks MUST span ≥ 2 different sectors
- Prioritize sectors NOT already in current holdings
- If holdings ≤ 2: recommend ≥ 2 new stocks from different sectors
- Never recommend 2+ stocks from the same sector unless holdings already have 4+ sectors

━━━ OUTPUT FORMAT (strict JSON) ━━━
{
  "decisions": [
    {
      "action": "BUY|HOLD",
      "symbol": "TICKER",
      "name": "Company Name",
      "excd": "NAS|NYS|AMS",
      "sector": "Sector Name",
      "quantity": shares_int,
      "price": 0,
      "stop_loss": stop_loss_usd,
      "take_profit": target_price_usd,
      "rr_ratio": calculated_rr_ratio,
      "reason": "MUST include: sector trend + change_rate + volume + R:R calculation"
    }
  ],
  "total_buy_cost_estimate": total_usd,
  "diversification_note": "Which sectors are new vs already held",
  "strategy_summary": "1-2 sentence strategy"
}

━━━ HARD RULES ━━━
- HOLD decisions need a "reason" too — explain which filter failed.
- Every BUY "reason" MUST contain the R:R ratio calculation.
- Do NOT recommend stocks that are not in the provided quotes data.
- quantity must be a positive integer (no fractional shares).
- price is always 0 (market execution handled by BuyExecutor).
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
