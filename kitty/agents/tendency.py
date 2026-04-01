"""투자 성향 에이전트 - 다른 에이전트의 경계선 판단에 성향 지침을 제공"""
from typing import Any

from .base import BaseAgent

# ── 성향 프로필 정의 ─────────────────────────────────────────────────────────
# 각 프로필은 'directive' (에이전트 프롬프트 주입용 지침 텍스트) 를 포함합니다.

TENDENCY_PROFILES: dict[str, dict] = {
    "aggressive": {
        "label": "공격적 (Aggressive)",
        "description": "소폭 수익 빠른 실현, 시장 위험 신호 즉시 대응, 모멘텀 확인 시 적극 진입",
        "take_profit_pct": 3.0,    # 익절 검토 시작 수익률 (%)
        "stop_loss_pct": -2.0,     # 손절 검토 시작 손실률 (%)
        "max_weight_pct": 30.0,    # 단일 종목 최대 비중 (%)
        "cash_reserve_min": 0.15,  # 최소 현금 비중
        "entry_threshold_pct": 3.0,  # 당일 등락률 이 이하면 진입 적극 검토 (%)
        "directive": """\
[투자 성향 지침 — 공격적 (AGGRESSIVE)]
아래 성향 지침을 기존 원칙보다 우선 적용하세요.

• 익절: 평가수익 +3% 이상이면 PARTIAL_SELL 적극 검토. 상승 모멘텀 둔화 신호(거래량 감소·등락률 꺾임) 포착 시 +2%에도 즉시 실행.
• 손절: 평가손실 -2% 이상이면서 섹터 전망이 중립 또는 약세이면 지체 없이 SELL. 시장 리스크 신호(금리 급등·외인 대량 매도·섹터 급락)가 보이면 손익에 관계없이 빠른 처분 우선.
• 신규 진입: 섹터 모멘텀이 bullish로 확인되면 관망보다 실행 우선. 당일 등락률 +3% 이하 종목은 적극 진입 검토.
• 집중 배분: 확신도가 높은 종목 1~2개에 자산 집중 허용. 단일 종목 최대 비중 30%까지 허용 (기본 20% 대신).
• 현금 비중: 강력한 매수 기회 시 현금 비중을 15%까지 낮출 수 있음 (기본 30% 대신).
• 판단 원칙: 소폭 수익의 빠른 실현이 큰 수익을 기다리는 것보다 우선. 불확실할 때는 매도 방향으로 판단.\
""",
    },

    "balanced": {
        "label": "균형 (Balanced)",
        "description": "기본 원칙 유지, 섹터 전망과 수익률 기준을 균형있게 적용",
        "take_profit_pct": 15.0,
        "stop_loss_pct": -5.0,
        "max_weight_pct": 20.0,
        "cash_reserve_min": 0.30,
        "entry_threshold_pct": 5.0,
        "directive": """\
[투자 성향 지침 — 균형 (BALANCED)]
기존 원칙(익절 +15%, 손절 -5%, 현금 30% 유보)을 기본으로 적용하세요.
섹터 전망과 수익률을 균형 있게 고려하여 판단하세요.\
""",
    },

    "conservative": {
        "label": "보수적 (Conservative)",
        "description": "손실 최소화 우선, 높은 확신도에서만 진입, 현금 비중 높게 유지",
        "take_profit_pct": 10.0,
        "stop_loss_pct": -3.0,
        "max_weight_pct": 15.0,
        "cash_reserve_min": 0.50,
        "entry_threshold_pct": 2.0,
        "directive": """\
[투자 성향 지침 — 보수적 (CONSERVATIVE)]
아래 성향 지침을 기존 원칙보다 우선 적용하세요.

• 익절: 평가수익 +10% 이상이면 PARTIAL_SELL 검토. 시장 불확실성이 높으면 +7%에도 실행.
• 손절: 평가손실 -3% 이상이면 섹터 전망에 관계없이 SELL 우선 검토.
• 신규 진입: 당일 등락률 +2% 이하이고 거래량 급증 등 확실한 모멘텀 신호가 있을 때만 진입.
• 집중 배분: 단일 종목 최대 비중 15% 이하 유지. 분산 투자 우선.
• 현금 비중: 최소 50% 현금 유보. 강력한 기회에서도 70%를 초과하여 투입하지 않음.
• 판단 원칙: 불확실할 때는 HOLD 또는 현금 보유 방향으로 판단.\
""",
    },
}


SYSTEM_PROMPT = """당신은 투자 성향 전문 컨설턴트입니다.

역할:
- 투자자의 성향(공격적/균형/보수적)을 바탕으로 각 에이전트의 판단에 지침을 제공합니다
- 현재 포트폴리오 상황과 시장 환경을 고려하여 성향에 맞는 구체적인 조언을 합니다
- 성향 변경 요청을 처리하고 그 의미를 설명합니다

현재 지원 성향:
- 공격적 (aggressive): 소폭 수익 빠른 실현, 시장 위험 신호 즉시 대응, 모멘텀 확인 시 적극 진입
- 균형 (balanced): 기본 원칙 유지, 섹터 전망과 수익률 기준 균형 적용
- 보수적 (conservative): 손실 최소화 우선, 높은 확신도에서만 진입, 현금 비중 높게 유지
"""


class TendencyAgent(BaseAgent):
    """투자 성향 에이전트.

    get_directive() 로 현재 성향 지침 문자열을 반환합니다.
    반환된 지침은 종목평가가·종목발굴가·자산운용가 프롬프트에 주입됩니다.
    chat() 메서드를 통해 모니터 대시보드에서 직접 질문할 수 있습니다.
    """

    def __init__(self, profile_name: str = "aggressive") -> None:
        super().__init__(name="투자성향관리자", system_prompt=SYSTEM_PROMPT)
        self._profile_name = profile_name if profile_name in TENDENCY_PROFILES else "aggressive"

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def profile(self) -> dict:
        return TENDENCY_PROFILES[self._profile_name]

    def set_profile(self, profile_name: str) -> bool:
        """성향 변경. 유효하지 않은 이름이면 False 반환."""
        if profile_name not in TENDENCY_PROFILES:
            return False
        self._profile_name = profile_name
        return True

    def get_directive(self) -> str:
        """현재 성향 지침 문자열 반환 (AI 호출 없음, 결정론적)."""
        return self.profile["directive"]

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """현재 성향 정보 반환 (다른 에이전트와의 인터페이스 일관성용)."""
        return {
            "profile_name": self._profile_name,
            "label": self.profile["label"],
            "description": self.profile["description"],
            "directive": self.get_directive(),
        }
