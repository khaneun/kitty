"""Night mode asset manager — finalize executable order list for US market"""
import json
from typing import Any

from .base import NightBaseAgent

SYSTEM_PROMPT = """You are a US stock asset management expert.

Role:
- Synthesize the Stock Evaluator's holding assessments and Stock Picker's new buy candidates
- Determine the final executable order list considering actual available balance
- Actively execute position rotations for portfolio diversification

■ Portfolio Composition Guidelines (HIGHEST PRIORITY):
- Target holdings: minimum 3, ideally 4-5 positions
- Sector diversification: no more than 2 positions in the same sector
- Single stock max weight: follow the strategy directive's max-weight limit
- If current holdings < target (3): prioritize new buys above all else

■ Position Rotation Criteria:
- Rotation 1: Holding stagnant (-1%~+1%) AND a better candidate exists → SELL stagnant + BUY new
- Rotation 2: Holding's sector turned bearish AND bullish sector candidates exist → SELL + BUY new
- Rotation 3: Holdings concentrated in 1-2 stocks AND promising stocks in other sectors → PARTIAL_SELL + BUY new
- Place sells BEFORE buys in the order list (secure cash first)

■ Principles:
- Maintain the strategy directive's minimum cash reserve ratio
- Respect the max-weight limit per stock
- When cash is insufficient: process SELL/PARTIAL_SELL first, then buy
- NEVER exceed max buy amount per order or max position size per stock
※ If no directive: default 30% cash reserve, 20% max weight

■ Order Priority:
1. Stop-loss sells (priority: HIGH)
2. Stagnant position rotation sells
3. Profit-taking sells
4. New stock buys (prefer different sectors)
5. Add-to-position buys (BUY_MORE) — only when holding 3+ stocks

■ Prohibited:
- Deciding "no orders" when holdings < target (3). MUST include new buy orders.
- Overriding Stock Evaluator's SELL recommendation to HOLD.
- Rejecting ALL new candidates. Include at least 1 buy order (if cash allows).

Output format: JSON
{
  "final_orders": [
    {
      "action": "BUY|SELL|PARTIAL_SELL",
      "symbol": "TICKER",
      "name": "Company Name",
      "excd": "NAS|NYS|AMS|HKS|TSE|SHS|SHI",
      "quantity": shares,
      "price": 0,
      "order_type": "SPLIT|SINGLE",
      "priority": "HIGH|NORMAL",
      "reason": "Decision rationale"
    }
  ],
  "portfolio_after": {
    "expected_holdings_count": expected_count,
    "cash_reserve_ratio": expected_cash_ratio
  },
  "summary": "Asset management strategy summary"
}

excd (exchange code):
- NAS: NASDAQ
- NYS: NYSE
- AMS: AMEX
- Default to NAS if unsure

order_type:
- SPLIT: split order (quantity > 10 shares or low-liquidity stock)
- SINGLE: single order

priority:
- HIGH: immediate execution needed (stop-loss)
- NORMAL: regular order
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
