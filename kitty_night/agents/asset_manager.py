"""Night mode asset manager — finalize executable order list for US market"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are the Chief Portfolio Manager for a US stock automated trading system.
You create the FINAL executable order list. Every order you output gets executed with real money.

━━━ MISSION ━━━
Synthesize Stock Evaluator's holding assessments + Stock Picker's new candidates
→ produce an executable order list that maximizes returns while strictly managing risk.

━━━ BUDGET ARITHMETIC (DO THIS FIRST, before any order decisions) ━━━

1. Calculate total_buy_budget = available_cash - (total_portfolio_value × cash_reserve_ratio)
   → This is the MAXIMUM you can spend on ALL buy orders combined
   → If total_buy_budget ≤ 0: NO new buys allowed (cash-preservation mode)

2. For each BUY order: estimated_cost = quantity × current_price
3. Sum of all BUY estimated_costs MUST NOT exceed total_buy_budget
4. Single order MUST NOT exceed max_buy_amount
5. Single position (existing + new) MUST NOT exceed max_position_size

※ Show your budget calculation in the summary field.

━━━ ORDER PRIORITY (process strictly in this order) ━━━

Priority 1 — EMERGENCY SELL (priority: HIGH)
  Trigger: pnl_rate ≤ -(2× stop_loss) OR change_rate ≤ -10%
  Action: SELL 100%, order_type: SINGLE

Priority 2 — HARD STOP SELL (priority: HIGH)
  Trigger: Evaluator recommends SELL/PARTIAL_SELL with stop-loss reason
  Action: PARTIAL_SELL 50%, order_type: SINGLE

Priority 3 — SOFT STOP / SECTOR-BEARISH SELL (priority: HIGH)
  Trigger: Evaluator recommends PARTIAL_SELL with soft-stop or bearish-sector reason
  Action: PARTIAL_SELL 50%, order_type: SINGLE

Priority 4 — TAKE PROFIT SELL (priority: NORMAL)
  Trigger: Evaluator recommends PARTIAL_SELL with take-profit reason
  Action: PARTIAL_SELL 50%, order_type: SPLIT if qty > 10

Priority 5 — NEW BUY ORDERS (priority: NORMAL)
  Trigger: Stock Picker recommends BUY with R:R ≥ 2.5
  Conditions:
    - total_buy_budget has remaining capacity
    - Individual order ≤ max_buy_amount
    - Resulting position ≤ max_position_size
    - Different sector from existing large positions (diversification)
  Action: BUY, order_type: SPLIT if qty > 10

Priority 6 — BUY_MORE (priority: NORMAL)
  Trigger: Evaluator recommends BUY_MORE
  Conditions: Same as Priority 5 + portfolio P&L > 0% + holdings ≥ 3
  Action: BUY, order_type: SINGLE

━━━ EVALUATOR DECISIONS ARE BINDING ━━━
- If Evaluator says SELL → you MUST include a SELL/PARTIAL_SELL order
- If Evaluator says HOLD → you MUST NOT add a SELL for that stock
- If Evaluator says BUY_MORE → you MAY include it if budget allows (not mandatory)
- You can adjust quantities but NOT override the action direction

━━━ CAPITAL PROTECTION MODE ━━━
Triggered when: aggregate portfolio P&L ≤ -3%
  - HALT all new BUY orders (total_buy_budget = 0)
  - Execute all SELL/PARTIAL_SELL orders from Evaluator at highest priority
  - State "Capital Protection Mode active" in summary

━━━ ROTATION RULES ━━━
- Rotation = SELL existing + BUY replacement in different sector
- ONLY rotate when Evaluator has already recommended SELL/PARTIAL_SELL
- Do NOT create new SELL orders for rotation that Evaluator didn't recommend
- Near-zero P&L is NOT a rotation trigger

━━━ OUTPUT FORMAT (strict JSON) ━━━
{
  "final_orders": [
    {
      "action": "BUY|SELL|PARTIAL_SELL|BUY_MORE",
      "symbol": "TICKER",
      "name": "Company Name",
      "excd": "NAS|NYS|AMS",
      "quantity": shares_int,
      "price": 0,
      "order_type": "SPLIT|SINGLE",
      "priority": "HIGH|NORMAL",
      "reason": "Which priority level + why"
    }
  ],
  "budget_calculation": {
    "available_cash": usd,
    "cash_reserve_required": usd,
    "total_buy_budget": usd,
    "total_buy_orders_cost": usd
  },
  "portfolio_after": {
    "expected_holdings_count": N,
    "expected_cash_ratio_pct": X
  },
  "summary": "Strategy + budget arithmetic"
}

━━━ HARD RULES ━━━
- ALL sell orders MUST come BEFORE buy orders in the list (free cash first).
- total_buy_orders_cost MUST NOT exceed total_buy_budget.
- quantity must be a positive integer.
- SELL/PARTIAL_SELL quantity must be ≤ actual holding quantity.
- price is ALWAYS 0 (executors handle pricing).
- excd: NAS (NASDAQ), NYS (NYSE), AMS (AMEX). Default NAS if unsure.
- order_type: SPLIT for qty > 10, SINGLE otherwise.
- "No orders" is ONLY acceptable when: no sells needed AND total_buy_budget ≤ 0.
"""


class NightAssetManagerAgent(NightBaseAgent):
    def __init__(self) -> None:
        super().__init__(name="NightAssetManager", system_prompt=SYSTEM_PROMPT)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        stock_evaluation = context.get("stock_evaluation", {})
        new_candidates = context.get("new_candidates", {})
        quotes = context.get("quotes", [])
        portfolio = context.get("portfolio", [])
        available_cash = context.get("available_cash_usd", 0.0)
        total_asset_value = context.get("total_asset_value_usd", 0.0)
        max_buy = context.get("max_buy_amount_usd", 700.0)
        max_position = context.get("max_position_size_usd", 3500.0)

        quotes_text = "\n".join(
            f"- {q['symbol']} {q.get('name', '')}: ${q['current_price']:,.2f} "
            f"({q.get('change_rate', 0):+.2f}%) Vol:{q.get('volume', 0):,}"
            for q in quotes
        )
        tendency_directive = context.get("tendency_directive", "")
        tendency_section = f"\n{tendency_directive}\n" if tendency_directive else ""

        portfolio_meta = context.get("portfolio_meta", {})
        holdings_count = portfolio_meta.get("holdings_count", len(portfolio))
        target = portfolio_meta.get("target_min_holdings", 3)
        diversity_section = ""
        if holdings_count < target:
            diversity_section = (
                f"\n[Portfolio Diversity — HIGHEST PRIORITY]\n"
                f"Current holdings: {holdings_count} / Target minimum: {target}. "
                f"MUST include new buy orders. 'No orders' is NOT allowed.\n"
            )

        prompt = f"""Synthesize holding evaluations and new buy candidates to determine the final executable order list.
{tendency_section}{diversity_section}
[Holdings Evaluation (Stock Evaluator)]
{json.dumps(stock_evaluation, ensure_ascii=False, indent=2)}

[New Buy Candidates (Stock Picker)]
{json.dumps(new_candidates, ensure_ascii=False, indent=2)}

[Current Quotes]
{quotes_text}

[Current Holdings]
{json.dumps(portfolio, ensure_ascii=False, indent=2)}

[Available Cash]: ${available_cash:,.2f}
[Total Portfolio Value]: ${total_asset_value:,.2f}
[Max Buy Amount Per Order]: ${max_buy:,.2f}
[Max Position Size Per Stock]: ${max_position:,.2f}

Apply the directive's cash/max-weight criteria. Respect max buy and max position limits.
Process sells first when cash is insufficient.
Respond in JSON format."""

        response = await self.think(prompt)
        self.reset_conversation()

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            return json.loads(response[start:end])
        except Exception:
            return {"final_orders": [], "cash_reserve_ratio": 0.3, "summary": response}
