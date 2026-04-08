"""Night mode 투자 성향 에이전트 — US market, 5 dimensions × 6 levels

  Dimension       L1 (aggressive)          L6 (conservative)
  ─────────────── ──────────────────────── ──────────────────────
  Take Profit     +3% immediate            +30% hold long
  Stop Loss       -2% cut fast             -15% tolerate
  Cash Reserve    10% minimum              60% minimum
  Max Weight      40% single stock         10% diversified
  Entry           +10% chase OK            ±0.5% flat only
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .base import NightBaseAgent

_KST = ZoneInfo("Asia/Seoul")
_STATE_PATH = Path("night-logs/night_tendency_state.json")

# ── Dimensions ────────────────────────────────────────────────────────────────

DIMS = ("take_profit", "stop_loss", "cash", "max_weight", "entry")

DIM_LABELS: dict[str, str] = {
    "take_profit": "Take Profit",
    "stop_loss":   "Stop Loss",
    "cash":        "Cash Reserve",
    "max_weight":  "Max Weight",
    "entry":       "Entry Threshold",
}

# ── 6-level values (L1 = most aggressive, L6 = most conservative) ─────────

LEVEL_VALUES: dict[str, dict[int, float]] = {
    # take_profit: expanded targets to ride momentum longer, improve R:R ratio
    "take_profit": {1: 5.0, 2: 8.0, 3: 12.0, 4: 18.0, 5: 28.0, 6: 40.0},
    # stop_loss: tighter thresholds at every level to minimize downside
    "stop_loss":   {1: -1.5, 2: -2.5, 3: -4.0, 4: -6.0, 5: -8.5, 6: -12.0},
    "cash":        {1: 0.10, 2: 0.18, 3: 0.25, 4: 0.35, 5: 0.48, 6: 0.60},
    "max_weight":  {1: 40.0, 2: 30.0, 3: 22.0, 4: 17.0, 5: 13.0, 6: 10.0},
    "entry":       {1: 10.0, 2: 6.0, 3: 4.0, 4: 2.5, 5: 1.5, 6: 0.5},
}

LEVEL_LABEL: dict[int, str] = {
    1: "Very Aggressive",
    2: "Aggressive",
    3: "Active",
    4: "Balanced",
    5: "Conservative",
    6: "Very Conservative",
}

_INITIAL_LEVELS: dict[str, int] = {dim: 2 for dim in DIMS}

PRESETS: dict[str, dict[str, int]] = {
    # Higher TP target + tight SL — R:R 3.2:1
    "aggressive": {
        "take_profit": 2,  # +8%
        "stop_loss":   2,  # -2.5%
        "cash":        2,  # 18%
        "max_weight":  2,  # 30%
        "entry":       2,  # +6%
    },
    # Balanced with improved R:R — R:R 3.4:1
    "balanced": {
        "take_profit": 3,  # +12%
        "stop_loss":   3,  # -4%
        "cash":        4,  # 35%
        "max_weight":  4,  # 17%
        "entry":       3,  # +4%
    },
    # Wide TP + tight SL, high cash — R:R 7.1:1
    "conservative": {
        "take_profit": 5,  # +28%
        "stop_loss":   3,  # -4%
        "cash":        5,  # 48%
        "max_weight":  5,  # 13%
        "entry":       5,  # +1.5%
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _v(dim: str, level: int) -> float:
    return LEVEL_VALUES[dim][max(1, min(6, level))]


def _overall(levels: dict[str, int]) -> tuple[str, str]:
    avg = sum(levels.values()) / len(levels)
    if avg <= 2.0:
        profile, name = "aggressive", "Aggressive"
    elif avg <= 3.0:
        profile, name = "aggressive", "Active"
    elif avg <= 4.0:
        profile, name = "balanced", "Balanced"
    elif avg <= 5.0:
        profile, name = "conservative", "Conservative"
    else:
        profile, name = "conservative", "Very Conservative"
    return profile, f"{name} (avg L{avg:.1f})"


def _build_directive(levels: dict[str, int], rationale: str = "") -> str:
    tp_lv = levels["take_profit"]
    sl_lv = levels["stop_loss"]
    ca_lv = levels["cash"]
    mw_lv = levels["max_weight"]
    en_lv = levels["entry"]

    tp = _v("take_profit", tp_lv)
    sl = abs(_v("stop_loss", sl_lv))
    ca = int(_v("cash", ca_lv) * 100)
    mw = _v("max_weight", mw_lv)
    en = _v("entry", en_lv)

    _, overall_label = _overall(levels)

    if tp_lv <= 2:
        tp_action = f"execute PARTIAL_SELL immediately. On momentum slowdown, sell at +{max(1.0, round(tp * 0.7, 1))}%"
    elif tp_lv <= 4:
        tp_action = f"actively consider PARTIAL_SELL. On sector weakness, sell at +{round(tp * 0.75, 1)}%"
    else:
        tp_action = "consider PARTIAL_SELL. Avoid premature selling before target"

    if sl_lv <= 2:
        sl_action = "SELL without hesitation. On market uncertainty, exit regardless of P&L"
    elif sl_lv <= 4:
        sl_action = "prioritize SELL. Exit if accompanied by sector weakness or risk signals"
    else:
        sl_action = "consider SELL. Distinguish temporary correction from trend reversal"

    if en_lv <= 2:
        en_cond = "chase buying allowed when sector momentum is bullish"
    elif en_lv <= 4:
        en_cond = "enter after confirming bullish sector momentum + volume"
    else:
        en_cond = "only enter top-tier sector picks in flat range with strong signals"

    if all(v <= 2 for v in levels.values()):
        principle = "Quick P&L realization over waiting. When uncertain, lean toward selling."
    elif all(v >= 5 for v in levels.values()):
        principle = "Loss prevention over profit pursuit. When uncertain, HOLD or stay in cash."
    else:
        principle = "Apply each dimension's criteria strictly. Execute by the rules when criteria aren't met."

    soft_sl = round(sl * 0.5, 2)        # soft stop: 50% of hard stop threshold
    trail_trigger = round(tp * 0.5, 1)  # trailing stop activates after 50% of TP target

    rationale_line = f"\n• Rationale: {rationale}" if rationale else ""

    return f"""[Trading Strategy Directive — {overall_label}]
Apply these per-dimension criteria with higher priority than default rules.

• Take Profit (L{tp_lv} {LEVEL_LABEL[tp_lv]}): At +{tp}% unrealized gain, {tp_action}.
• Stop Loss (L{sl_lv} {LEVEL_LABEL[sl_lv]}): At -{sl}% unrealized loss with neutral/weak sector, {sl_action}.
• Cash Reserve (L{ca_lv} {LEVEL_LABEL[ca_lv]}): Maintain at least {ca}% cash. Never go below {ca}% even on strong buy signals.
• Max Weight (L{mw_lv} {LEVEL_LABEL[mw_lv]}): Single stock max {mw}% of portfolio. High-conviction picks may concentrate up to this limit.
• Entry (L{en_lv} {LEVEL_LABEL[en_lv]}): Only enter stocks with intraday change ≤+{en}%. {en_cond}.
• Principle: {principle}{rationale_line}

[Loss Minimization Technical Rules — MANDATORY]
① Soft Stop (Early Warning): At -{soft_sl}% unrealized loss, begin PARTIAL_SELL evaluation.
   - Sector bullish + clearly temporary dip → HOLD allowed, but MUST re-evaluate next cycle.
   - Sector neutral/bearish → Execute PARTIAL_SELL immediately. Do NOT wait for hard stop.
② Hard Stop (Mandatory Cut): At -{sl}% loss exceeded → PARTIAL_SELL ~50% unconditionally. No exceptions.
③ Emergency Stop (Full Exit): At -{round(sl*2, 1)}% loss (2× hard stop) or circuit breaker proximity → Full SELL.
④ Trailing Stop (Profit Protection): If peak unrealized gain reached +{trail_trigger}% and current gain fell below +{round(trail_trigger*0.4, 1)}% → execute PARTIAL_SELL to lock in profits.
   - Capture momentum gains. Fading momentum = exit signal. Re-entry possible on next setup.
⑤ Volume Momentum Exit: If intraday change_rate ≤-1.5% AND sector is neutral/bearish → consider PARTIAL_SELL even before hard stop.
⑥ R:R Minimum for New Entries: Only approve new buys where expected TP/SL ratio ≥ 2.5:1."""


# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Chief Investment Officer (CIO) for US stock trading.

Role:
After each trading session, analyze agent performance evaluations and determine
the next session's investment levels across 5 independent dimensions.

── 5 Dimensions × 6 Levels ──────────────────────────────

■ Take Profit: profit target — higher level = longer ride for more gains
  L1(+5%) L2(+8%) L3(+12%) L4(+18%) L5(+28%) L6(+40%)

■ Stop Loss: loss-cutting speed — lower level (L1) = faster cut
  L1(-1.5%) L2(-2.5%) L3(-4%) L4(-6%) L5(-8.5%) L6(-12%)
  ※ L1 = fastest cut, L6 = most tolerant
  ※ Use 50% of stop threshold as soft stop (early warning system)

■ Cash Reserve: minimum cash ratio
  L1(10%) L2(18%) L3(25%) L4(35%) L5(48%) L6(60%)

■ Max Weight: single stock max allocation
  L1(40%) L2(30%) L3(22%) L4(17%) L5(13%) L6(10%)

■ Entry Threshold: max intraday change for new entry
  L1(10%) L2(6%) L3(4%) L4(2.5%) L5(1.5%) L6(0.5%)

── Adjustment Rules ──────────────────────────────────────

Agent scores are on 0-100 scale.
- Asset Manager / Stock Picker score low (≤40) → lower take_profit, stop_loss (more aggressive)
- Stock Evaluator / Sector Analyst score low (≤40) → raise cash, entry (more conservative)
- Sell Executor score low (≤40) → lower stop_loss (faster cuts)
- Buy Executor score low (≤40) → raise cash, entry (more selective)
- Overall high scores (≥70) → maintain or slightly lower levels
- Overall low scores (≤40) → raise each level by 1-2
- Max level change per update: ±2 (prevent sudden strategy shifts)
"""


# ── NightTendencyAgent ────────────────────────────────────────────────────────

class NightTendencyAgent(NightBaseAgent):
    """Night mode tendency agent — 5 dims × 6 levels for US market."""

    def __init__(self, profile_name: str = "aggressive") -> None:
        super().__init__(name="NightTendency", system_prompt=SYSTEM_PROMPT)
        loaded = self._load_state()
        if loaded:
            self._levels = loaded["levels"]
            self._rationale = loaded.get("rationale", "")
            self._updated_at = loaded.get("updated_at", "")
        else:
            preset = profile_name if profile_name in PRESETS else "aggressive"
            self._levels: dict[str, int] = dict(PRESETS[preset])
            self._rationale = "Initial setup"
            self._updated_at = ""

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> dict | None:
        try:
            if _STATE_PATH.exists():
                data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
                lvl = data.get("levels", {})
                if all(k in lvl for k in DIMS):
                    return data
        except Exception:
            pass
        return None

    def _save_state(self) -> None:
        from kitty_night.utils import logger
        try:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(
                json.dumps({
                    "levels": self._levels,
                    "rationale": self._rationale,
                    "updated_at": self._updated_at,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Night:NightTendency] state save failed: {e}")

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def profile_name(self) -> str:
        profile, _ = _overall(self._levels)
        return profile

    @property
    def profile(self) -> dict:
        _, label = _overall(self._levels)
        return {
            "profile_name":        self.profile_name,
            "label":               label,
            "levels":              dict(self._levels),
            "take_profit_pct":     _v("take_profit", self._levels["take_profit"]),
            "stop_loss_pct":       _v("stop_loss",   self._levels["stop_loss"]),
            "cash_reserve_min":    _v("cash",        self._levels["cash"]),
            "max_weight_pct":      _v("max_weight",  self._levels["max_weight"]),
            "entry_threshold_pct": _v("entry",       self._levels["entry"]),
            "rationale":           self._rationale,
            "updated_at":          self._updated_at,
        }

    def set_profile(self, profile_name: str) -> bool:
        if profile_name not in PRESETS:
            return False
        self._levels = dict(PRESETS[profile_name])
        self._rationale = f"Manual switch — {profile_name} preset"
        self._updated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()
        return True

    def get_directive(self) -> str:
        return _build_directive(self._levels, self._rationale)

    # ── Post-session strategy update (AI call) ────────────────────────────────

    async def update_strategy(self, eval_results: dict[str, Any]) -> dict[str, Any]:
        from kitty_night.utils import logger
        from kitty_night.config import AIProvider, night_settings

        if not eval_results:
            logger.info("[Night:NightTendency] No eval results — keeping levels")
            return self.profile

        logger.info("[Night:NightTendency] AI determining next session levels...")

        scores = [
            {"agent": name, "score": e.get("score", 50),
             "summary": e.get("summary", ""), "improvement": e.get("improvement", "")}
            for name, e in eval_results.items()
        ]
        avg_score = sum(s["score"] for s in scores) / len(scores) if scores else 50.0

        current_levels_str = "\n".join(
            f"  {DIM_LABELS[d]}: L{self._levels[d]} ({LEVEL_LABEL[self._levels[d]]}) "
            f"→ value {_v(d, self._levels[d])}"
            for d in DIMS
        )

        prompt = f"""Here are today's agent performance evaluation results. Determine the level for each dimension for the next trading session.

── Current Levels ──────────────────────────────
{current_levels_str}

── Agent Performance (avg {avg_score:.1f}/100) ─────────
{json.dumps(scores, ensure_ascii=False, indent=2)}

── Level Decision Rules ────────────────────────
Each level is an integer from 1 (most aggressive) to 6 (most conservative).
Scores are on 0-100 scale.
- Asset Manager / Stock Picker low (≤40) → lower take_profit, stop_loss
- Sector Analyst / Stock Evaluator low (≤40) → raise cash, entry
- Sell Executor low (≤40) → lower stop_loss
- Overall high (≥70) → maintain or slightly lower
- Overall low (≤40) → raise each by 1-2
- Max change per dimension: ±2

Respond ONLY in this JSON format:
{{
  "take_profit": 1-6,
  "stop_loss": 1-6,
  "cash": 1-6,
  "max_weight": 1-6,
  "entry": 1-6,
  "rationale": "2-3 sentence rationale based on today's performance"
}}"""

        try:
            new_data: dict = {}

            if night_settings.ai_provider == AIProvider.ANTHROPIC:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=night_settings.anthropic_api_key)
                resp = await client.messages.create(
                    model=self._model, max_tokens=512,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else "{}"
                if resp.usage:
                    self._record_tokens(resp.usage.input_tokens, resp.usage.output_tokens)
                m = re.search(r"\{.*?\}", text, re.DOTALL)
                new_data = json.loads(m.group()) if m else {}

            elif night_settings.ai_provider == AIProvider.OPENAI:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=night_settings.openai_api_key)
                resp = await client.chat.completions.create(
                    model=self._model, max_tokens=512,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                if resp.usage:
                    self._record_tokens(resp.usage.prompt_tokens, resp.usage.completion_tokens)
                new_data = json.loads(resp.choices[0].message.content or "{}")

            elif night_settings.ai_provider == AIProvider.GEMINI:
                import google.generativeai as genai
                genai.configure(api_key=night_settings.gemini_api_key)
                model = genai.GenerativeModel(
                    model_name=self._model,
                    system_instruction=self.system_prompt,
                )
                resp = await model.generate_content_async(prompt)
                if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                    self._record_tokens(
                        resp.usage_metadata.prompt_token_count,
                        resp.usage_metadata.candidates_token_count,
                    )
                text = resp.text or "{}"
                m = re.search(r"\{.*?\}", text, re.DOTALL)
                new_data = json.loads(m.group()) if m else {}

            if not new_data:
                logger.warning("[Night:NightTendency] AI response parse failed — keeping levels")
                return self.profile

            new_levels: dict[str, int] = {}
            for dim in DIMS:
                raw = int(new_data.get(dim, self._levels[dim]))
                clamped = max(1, min(6, raw))
                prev = self._levels[dim]
                new_levels[dim] = max(prev - 2, min(prev + 2, clamped))

            rationale = str(new_data.get("rationale", ""))[:300]
            updated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

            changes = [
                f"{DIM_LABELS[d]} L{self._levels[d]}→L{new_levels[d]}"
                for d in DIMS if new_levels[d] != self._levels[d]
            ]
            _, new_label = _overall(new_levels)
            logger.info(
                f"[Night:NightTendency] Level update: {new_label} | "
                + (", ".join(changes) if changes else "no changes")
            )

            self._levels = new_levels
            self._rationale = rationale
            self._updated_at = updated_at
            self._save_state()

            return self.profile

        except Exception as e:
            logger.error(f"[Night:NightTendency] update_strategy error: {e}")
            return self.profile

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        return self.profile
