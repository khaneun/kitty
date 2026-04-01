"""투자 성향 에이전트

5개 차원 각각 6단계 레벨로 투자 행동 양식을 결정합니다.

  차원          L1 (공격적)           L6 (보수적)
  ─────────── ──────────────────── ──────────────────────
  익절         +2%에 즉시 실현       +25% 될 때까지 보유
  손절         -1.5% 즉시 차단       -13% 감내
  현금         10% 유보              60% 유보
  종목집중      최대 40% 집중         최대 10% (분산)
  진입기준      +8% 추격매수도 가능    ±0.5% 이하만 진입

장 마감 후 update_strategy()를 호출하면 AI가 성과를 분석해 내일 레벨을 결정합니다.
결정된 레벨은 logs/tendency_state.json에 저장되어 재시작 시에도 유지됩니다.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .base import BaseAgent

_KST = ZoneInfo("Asia/Seoul")
_STATE_PATH = Path("logs/tendency_state.json")

# ── 차원 정의 ─────────────────────────────────────────────────────────────────

DIMS = ("take_profit", "stop_loss", "cash", "max_weight", "entry")

DIM_LABELS: dict[str, str] = {
    "take_profit": "익절",
    "stop_loss":   "손절",
    "cash":        "현금",
    "max_weight":  "종목집중",
    "entry":       "진입기준",
}

# ── 6단계 실제값 (L1 = 가장 공격적, L6 = 가장 보수적) ─────────────────────────

LEVEL_VALUES: dict[str, dict[int, float]] = {
    # 익절 시작 수익률 (%)
    "take_profit": {1: 2.0, 2: 4.0, 3: 7.0, 4: 12.0, 5: 18.0, 6: 25.0},
    # 손절 기준 손실률 (%) — 절댓값이 작을수록 빠른 손절
    "stop_loss":   {1: -1.5, 2: -2.5, 3: -4.0, 4: -6.0, 5: -9.0, 6: -13.0},
    # 최소 현금 비중 (0~1)
    "cash":        {1: 0.10, 2: 0.18, 3: 0.25, 4: 0.35, 5: 0.48, 6: 0.60},
    # 단일 종목 최대 비중 (%)
    "max_weight":  {1: 40.0, 2: 30.0, 3: 22.0, 4: 17.0, 5: 13.0, 6: 10.0},
    # 신규 진입 가능 당일 등락률 상한 (%) — 높을수록 추격매수 허용
    "entry":       {1: 8.0, 2: 5.0, 3: 3.0, 4: 2.0, 5: 1.0, 6: 0.5},
}

LEVEL_LABEL: dict[int, str] = {
    1: "매우 공격적",
    2: "공격적",
    3: "적극적",
    4: "균형",
    5: "보수적",
    6: "매우 보수적",
}

# ── 초기 레벨 (모두 L2 = 공격적 성향) ────────────────────────────────────────

_INITIAL_LEVELS: dict[str, int] = {dim: 2 for dim in DIMS}

# ── 프리셋 (set_profile() / 참고용) ──────────────────────────────────────────
# 각 값은 해당 차원의 레벨 (1~6)

PRESETS: dict[str, dict[str, int]] = {
    # 빠른 익절·손절, 집중 투자, 낮은 현금
    "aggressive": {
        "take_profit": 2,  # +4.0%
        "stop_loss":   2,  # -2.5%
        "cash":        2,  # 18%
        "max_weight":  2,  # 30%
        "entry":       2,  # +5%
    },
    # 균형 잡힌 기준, 적절한 분산
    "balanced": {
        "take_profit": 4,  # +12.0%
        "stop_loss":   4,  # -6.0%
        "cash":        4,  # 35%
        "max_weight":  4,  # 17%
        "entry":       3,  # +3%
    },
    # 충분한 수익 대기, 높은 현금, 분산 투자
    "conservative": {
        "take_profit": 5,  # +18.0%
        "stop_loss":   3,  # -4.0% (자본 보호 위해 중간 강도)
        "cash":        5,  # 48%
        "max_weight":  5,  # 13%
        "entry":       5,  # +1%
    },
}


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────

def _v(dim: str, level: int) -> float:
    """차원과 레벨로 실제값 반환"""
    return LEVEL_VALUES[dim][max(1, min(6, level))]


def _overall(levels: dict[str, int]) -> tuple[str, str]:
    """레벨 dict → (profile_name, label_str) 전체 성향 결정"""
    avg = sum(levels.values()) / len(levels)
    if avg <= 2.0:
        profile, name = "aggressive", "공격적"
    elif avg <= 3.0:
        profile, name = "aggressive", "적극적"
    elif avg <= 4.0:
        profile, name = "balanced", "균형"
    elif avg <= 5.0:
        profile, name = "conservative", "보수적"
    else:
        profile, name = "conservative", "매우 보수적"
    return profile, f"{name} (평균 L{avg:.1f})"


def _build_directive(levels: dict[str, int], rationale: str = "") -> str:
    """현재 레벨에서 에이전트 주입용 지침 문자열 동적 생성"""
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

    # 익절 행동 표현
    if tp_lv <= 2:
        tp_action = f"즉시 PARTIAL_SELL 실행. 모멘텀 둔화 감지 시 +{max(1.0, round(tp * 0.7, 1))}%에도 실행"
    elif tp_lv <= 4:
        tp_action = f"PARTIAL_SELL 적극 검토. 섹터 전망 약화 시 +{round(tp * 0.75, 1)}%에도 실행"
    else:
        tp_action = f"PARTIAL_SELL 검토. 수익 목표 도달 전 섣부른 매도 지양"

    # 손절 행동 표현
    if sl_lv <= 2:
        sl_action = "지체 없이 SELL. 시장 불확실 신호 시 손익 무관하게 즉시 처분"
    elif sl_lv <= 4:
        sl_action = "SELL 우선 검토. 섹터 약세 또는 리스크 신호 동반 시 처분"
    else:
        sl_action = "SELL 검토. 일시적 조정과 추세 반전을 구분한 후 결정"

    # 진입 행동 표현
    if en_lv <= 2:
        en_cond = "섹터 모멘텀 bullish 확인 시 추격 매수도 가능"
    elif en_lv <= 4:
        en_cond = "섹터 모멘텀 bullish + 거래량 확인 후 진입"
    else:
        en_cond = "강한 모멘텀 신호 + 섹터 상위 종목 한정, 보합권만 진입"

    # 종합 원칙
    if all(v <= 2 for v in levels.values()):
        principle = "빠른 손익 실현이 대기보다 우선. 불확실 시 매도 방향으로 판단."
    elif all(v >= 5 for v in levels.values()):
        principle = "손실 방어가 수익 추구보다 중요. 불확실 시 HOLD 또는 현금 보유."
    else:
        principle = "각 차원별 기준을 엄격히 적용. 기준 미달 시 원칙대로 실행."

    rationale_line = f"\n• 조정 근거: {rationale}" if rationale else ""

    return f"""[투자 성향 지침 — {overall_label}]
각 차원별 기준을 기존 원칙보다 우선 적용하세요.

• 익절 (L{tp_lv} {LEVEL_LABEL[tp_lv]}): 평가수익 +{tp}% 이상이면 {tp_action}.
• 손절 (L{sl_lv} {LEVEL_LABEL[sl_lv]}): 평가손실 -{sl}% 이상이면서 섹터 중립·약세이면 {sl_action}.
• 현금 유보 (L{ca_lv} {LEVEL_LABEL[ca_lv]}): 최소 {ca}% 현금 유보. 강한 매수 기회에도 {ca}% 미만으로 낮추지 않음.
• 종목 집중 (L{mw_lv} {LEVEL_LABEL[mw_lv]}): 단일 종목 최대 비중 {mw}%. 확신도 높은 종목에 집중 배분 가능.
• 진입 기준 (L{en_lv} {LEVEL_LABEL[en_lv]}): 당일 등락률 +{en}% 이하 종목만 신규 진입 검토. {en_cond}.
• 판단 원칙: {principle}{rationale_line}"""


# ── 시스템 프롬프트 ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 투자 전략 최고 책임자(CIO)입니다.

역할:
매일 장 마감 후 에이전트 성과 평가를 분석하여 다음 날 5개 차원 각각의 투자 레벨을 결정합니다.
각 차원은 독립적으로 결정할 수 있어 다양한 성향 조합이 가능합니다.

── 5개 차원 × 6단계 레벨 ──────────────────────────────

■ 익절 (take_profit): 수익 실현 속도
  L1(+2%) L2(+4%) L3(+7%) L4(+12%) L5(+18%) L6(+25%)

■ 손절 (stop_loss): 손실 차단 속도
  L1(-1.5%) L2(-2.5%) L3(-4%) L4(-6%) L5(-9%) L6(-13%)
  ※ L1이 가장 빠른 손절, L6이 가장 느린 손절

■ 현금 유보 (cash): 현금 보유 비중
  L1(10%) L2(18%) L3(25%) L4(35%) L5(48%) L6(60%)

■ 종목 집중 (max_weight): 단일 종목 최대 비중
  L1(40%) L2(30%) L3(22%) L4(17%) L5(13%) L6(10%)

■ 진입 기준 (entry): 신규 진입 가능 당일 등락률 상한
  L1(8%) L2(5%) L3(3%) L4(2%) L5(1%) L6(0.5%)
  ※ L1이 가장 공격적(추격매수 가능), L6이 가장 제한적

── 차원별 독립 조합 예시 ──────────────────────────────

• "익절에 인내, 손절에 공격적" (트렌드 추종)
  take_profit=L5, stop_loss=L1 → 수익은 길게, 손실은 빠르게

• "익절에 공격적, 손절에 관대" (스캘핑형)
  take_profit=L1, stop_loss=L5 → 소폭 수익 즉시 실현, 손실은 어느 정도 감내

• "현금 보수적이지만 진입 공격적" (기회 집중형)
  cash=L5, entry=L1 → 평소엔 현금 많이 보유하지만 기회 오면 추격매수도

── 조정 기준 ─────────────────────────────────────────

에이전트 점수 기반 조정 방향:
- 자산운용가·종목발굴가 점수 낮음 → 익절(L-), 손절(L-) 조정 (더 공격적)
- 종목평가가·섹터분석가 점수 낮음 → 현금(L+), 진입기준(L+) 조정 (더 보수적)
- 매도실행가 점수 낮음 → 손절(L-) 조정 (더 빠른 손절)
- 매수실행가 점수 낮음 → 현금(L+), 진입기준(L+) 조정 (더 선별적 진입)
- 전반적 고점수(7↑) → 현재 레벨 유지 또는 소폭 공격적 조정
- 전반적 저점수(4↓) → 각 차원 1~2 레벨 보수적으로 이동
"""


# ── TendencyAgent ─────────────────────────────────────────────────────────────

class TendencyAgent(BaseAgent):
    """투자 성향 에이전트.

    5개 차원(익절/손절/현금/집중/진입) 각각 6단계 레벨로 투자 행동 양식을 결정합니다.

    - get_directive() : 현재 레벨 → 지침 문자열 반환 (AI 호출 없음, 결정론적)
    - update_strategy(eval_results) : 장 마감 성과 분석 → 내일 레벨 AI 결정
    - set_profile(name) : aggressive / balanced / conservative 프리셋으로 즉시 전환
    - logs/tendency_state.json에 상태 저장 → 재시작 시 복원
    """

    def __init__(self, profile_name: str = "aggressive") -> None:
        super().__init__(name="투자성향관리자", system_prompt=SYSTEM_PROMPT)
        loaded = self._load_state()
        if loaded:
            self._levels = loaded["levels"]
            self._rationale = loaded.get("rationale", "")
            self._updated_at = loaded.get("updated_at", "")
        else:
            preset = profile_name if profile_name in PRESETS else "aggressive"
            self._levels: dict[str, int] = dict(PRESETS[preset])
            self._rationale = "초기 설정"
            self._updated_at = ""

    # ── 상태 영속화 ────────────────────────────────────────────────────────────

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
            from kitty.utils import logger
            logger.warning(f"[투자성향관리자] 상태 저장 실패: {e}")

    # ── 공개 인터페이스 ────────────────────────────────────────────────────────

    @property
    def profile_name(self) -> str:
        profile, _ = _overall(self._levels)
        return profile

    @property
    def profile(self) -> dict:
        """현재 상태 반환 — 모니터 카드 및 agent_context 저장용"""
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
        """프리셋 성향으로 즉시 전환. 유효하지 않으면 False 반환."""
        if profile_name not in PRESETS:
            return False
        self._levels = dict(PRESETS[profile_name])
        self._rationale = f"수동 전환 — {profile_name} 프리셋"
        self._updated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()
        return True

    def get_directive(self) -> str:
        """현재 레벨 기반 지침 문자열 반환 (AI 호출 없음, 결정론적)."""
        return _build_directive(self._levels, self._rationale)

    # ── 장 마감 후 전략 업데이트 (AI 호출) ────────────────────────────────────

    async def update_strategy(self, eval_results: dict[str, Any]) -> dict[str, Any]:
        """장 마감 성과 평가를 바탕으로 내일의 각 차원 레벨을 AI가 결정.

        Args:
            eval_results: PerformanceEvaluator.run() 반환값
                          {에이전트명: {score, summary, improvement, metrics}}

        Returns:
            업데이트된 profile dict (변경 없으면 현재 profile 반환)
        """
        from kitty.utils import logger

        if not eval_results:
            logger.info("[투자성향관리자] 평가 결과 없음 — 레벨 유지")
            return self.profile

        logger.info("[투자성향관리자] 내일 투자 레벨 AI 결정 중...")

        # 성과 요약 구성
        scores = [
            {"agent": name, "score": e.get("score", 5),
             "summary": e.get("summary", ""), "improvement": e.get("improvement", "")}
            for name, e in eval_results.items()
        ]
        avg_score = sum(s["score"] for s in scores) / len(scores) if scores else 5.0

        # 현재 레벨 요약
        current_levels_str = "\n".join(
            f"  {DIM_LABELS[d]}: L{self._levels[d]} ({LEVEL_LABEL[self._levels[d]]}) "
            f"→ 실제값 {_v(d, self._levels[d])}"
            for d in DIMS
        )

        prompt = f"""오늘 에이전트 성과 평가 결과입니다. 내일 적용할 각 차원 레벨을 결정하세요.

── 현재 투자 레벨 ──────────────────────────────
{current_levels_str}

── 오늘 에이전트 성과 (평균 {avg_score:.1f}/10) ─────────
{json.dumps(scores, ensure_ascii=False, indent=2)}

── 레벨 결정 기준 ──────────────────────────────
각 레벨 값은 1(가장 공격적)~6(가장 보수적) 정수입니다.
- 자산운용가·종목발굴가 점수 낮음 → take_profit, stop_loss 레벨 낮추기 (더 공격적)
- 섹터분석가·종목평가가 점수 낮음 → cash, entry 레벨 높이기 (더 보수적)
- 매도실행가 점수 낮음 → stop_loss 레벨 낮추기 (더 빠른 손절)
- 전반 고점수(7↑) → 현재 레벨 유지 또는 소폭 낮추기
- 전반 저점수(4↓) → 각 레벨 1~2 올리기 (보수적 이동)
- 레벨 변경은 한 번에 ±2 이내로 제한 (급격한 전략 전환 방지)

아래 JSON 형식으로만 응답하세요:
{{
  "take_profit": 1~6,
  "stop_loss": 1~6,
  "cash": 1~6,
  "max_weight": 1~6,
  "entry": 1~6,
  "rationale": "오늘 성과 기반 조정 근거 (한국어 2~3문장)"
}}"""

        try:
            from kitty.config import AIProvider, settings

            new_data: dict = {}

            if settings.ai_provider == AIProvider.ANTHROPIC:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                resp = await client.messages.create(
                    model=settings.resolved_model, max_tokens=512,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else "{}"
                if resp.usage:
                    self._record_tokens(resp.usage.input_tokens, resp.usage.output_tokens)
                m = re.search(r"\{.*?\}", text, re.DOTALL)
                new_data = json.loads(m.group()) if m else {}

            elif settings.ai_provider == AIProvider.OPENAI:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=settings.openai_api_key)
                resp = await client.chat.completions.create(
                    model=settings.resolved_model, max_tokens=512,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                if resp.usage:
                    self._record_tokens(resp.usage.prompt_tokens, resp.usage.completion_tokens)
                new_data = json.loads(resp.choices[0].message.content or "{}")

            elif settings.ai_provider == AIProvider.GEMINI:
                import google.generativeai as genai
                genai.configure(api_key=settings.gemini_api_key)
                model = genai.GenerativeModel(
                    model_name=settings.resolved_model,
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
                logger.warning("[투자성향관리자] AI 응답 파싱 실패 — 레벨 유지")
                return self.profile

            # 레벨 파싱 및 범위 클램프 (1~6), 급변 방지 (±2)
            new_levels: dict[str, int] = {}
            for dim in DIMS:
                raw = int(new_data.get(dim, self._levels[dim]))
                clamped = max(1, min(6, raw))
                # 한 번에 ±2 초과 변경 방지
                prev = self._levels[dim]
                new_levels[dim] = max(prev - 2, min(prev + 2, clamped))

            rationale = str(new_data.get("rationale", ""))[:300]
            updated_at = datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

            # 로그
            changes = [
                f"{DIM_LABELS[d]} L{self._levels[d]}→L{new_levels[d]}"
                for d in DIMS if new_levels[d] != self._levels[d]
            ]
            _, new_label = _overall(new_levels)
            logger.info(
                f"[투자성향관리자] 레벨 업데이트: {new_label} | "
                + (", ".join(changes) if changes else "변경 없음")
            )

            self._levels = new_levels
            self._rationale = rationale
            self._updated_at = updated_at
            self._save_state()

            return self.profile

        except Exception as e:
            from kitty.utils import logger
            logger.error(f"[투자성향관리자] update_strategy 오류: {e}")
            return self.profile

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """현재 성향 정보 반환 (에이전트 인터페이스 일관성용)."""
        return self.profile
