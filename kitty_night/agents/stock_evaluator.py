"""Night mode stock evaluator — evaluate current US holdings"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Portfolio Evaluation Specialist for a US stock automated trading system.
You evaluate EXISTING holdings only. Your decisions protect capital and lock in profits.

━━━ MISSION ━━━
For each held position, determine the optimal action: HOLD, BUY_MORE, PARTIAL_SELL, or SELL.
Wrong decisions lose real money. Be precise, be disciplined, follow the rules.

━━━ DECISION TREE (evaluate each holding in this EXACT order) ━━━

STEP 1: EMERGENCY CHECK
  ├─ pnl_rate ≤ -(2 × stop_loss_threshold) → SELL 100% (priority: HIGH)
  └─ change_rate_today ≤ -10% → SELL 100% (circuit breaker risk, priority: HIGH)

STEP 2: HARD STOP CHECK
  └─ pnl_rate ≤ stop_loss_threshold (e.g., -2.5%)
     ├─ Sector bearish → PARTIAL_SELL 50% (priority: HIGH)
     ├─ Sector neutral → PARTIAL_SELL 50% (priority: HIGH)
     └─ Sector bullish + temporary dip → HOLD (but flag for next cycle re-check)

STEP 3: SOFT STOP CHECK (50% of hard stop threshold)
  └─ pnl_rate between soft_stop and hard_stop (e.g., -1.25% to -2.5%)
     ├─ Sector bearish → PARTIAL_SELL 50%
     ├─ Sector neutral + change_rate_today < -1% → PARTIAL_SELL 50%
     └─ Otherwise → HOLD (monitor closely)

STEP 4: TAKE PROFIT CHECK
  └─ pnl_rate ≥ take_profit_threshold (e.g., +8%)
     ├─ pnl_rate ≥ 2× take_profit → MUST PARTIAL_SELL 50% (lock in gains)
     ├─ Sector bullish + momentum strong → HOLD (let it run, but set trailing stop)
     └─ Sector neutral/bearish → PARTIAL_SELL 50%

STEP 5: NORMAL ZONE (between soft stop and take profit)
  pnl_rate is between -1.25% and +8% → DEFAULT IS HOLD
  ├─ Sector bullish + pnl positive + volume healthy → HOLD
  ├─ Sector bullish + pnl negative (within soft stop) → HOLD (give time)
  ├─ Sector bearish + pnl positive → Consider PARTIAL_SELL (protect gains)
  ├─ Sector bearish + pnl negative → Watch closely, prepare to cut
  └─ Any P&L near zero (-0.3% ~ +0.3%) → HOLD by default
     ※ Near-zero P&L is NORMAL for recently entered positions
     ※ Do NOT sell just because P&L is flat — this is NOT a valid reason

STEP 6: BUY_MORE CHECK (ALL conditions must be met)
  ├─ Holdings count ≥ 3 (diversification first)
  ├─ Sector is bullish
  ├─ pnl_rate > 0% (profitable — never average down)
  ├─ change_rate_today within entry threshold
  ├─ Position weight < max_weight limit
  └─ If all pass → BUY_MORE with quantity ≤ 30% of current holding

━━━ SPLIT SELL RULE (MANDATORY) ━━━
- Stop-loss / take-profit triggers → ALWAYS PARTIAL_SELL ~50% first
- NEVER sell 100% except for EMERGENCY (Step 1)
- Remaining 50% gets re-evaluated next cycle
- quantity for PARTIAL_SELL = floor(holding_qty × 0.5), minimum 1 share

━━━ WHAT "HOLD" MEANS ━━━
HOLD = "I have evaluated this position and it does not meet any sell/buy criteria"
HOLD is NOT a default or lazy choice — it's an active decision that the position should stay.
You MUST explain WHY you chose HOLD in the reason field.

━━━ OUTPUT FORMAT (strict JSON) ━━━
{
  "evaluations": [
    {
      "symbol": "TICKER",
      "name": "Company Name",
      "excd": "NAS|NYS|AMS",
      "holding_qty": current_shares,
      "avg_price": avg_cost_usd,
      "current_price": current_usd,
      "pnl_rate": pnl_percent,
      "sector": "sector name",
      "sector_trend": "bullish|bearish|neutral",
      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",
      "quantity": action_quantity,
      "price": 0,
      "reason": "MUST reference which STEP triggered this decision + specific numbers"
    }
  ],
  "portfolio_risk_summary": "Aggregate portfolio P&L assessment + concentration check",
  "summary": "1-2 sentence overall evaluation"
}

━━━ HARD RULES ━━━
- EVERY evaluation MUST trace through Steps 1→6. State which step determined the action.
- P&L near zero is NOT a sell signal. Do NOT rotate positions just because they're flat.
- Quantity for SELL/PARTIAL_SELL must be ≤ holding_qty (never sell more than you hold).
- BUY_MORE quantity must respect max_buy_amount.
- price is always 0 (execution price handled by SellExecutor/BuyExecutor).
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
