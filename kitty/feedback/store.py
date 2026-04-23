"""에이전트 성과 피드백 영속 저장소

각 에이전트별 JSON 파일에 최근 MAX_ENTRIES일 피드백을 누적 저장하고,
system_prompt에 주입할 텍스트를 생성한다.

누적 요약 시스템:
  - 최근 14일 원본 엔트리 보관
  - 반복되는 개선사항 → "반복 이슈" (2회 이상 유사 키워드)
  - 검증된 좋은 패턴 → "검증된 규칙" (2회 이상 반복)
  - 프롬프트 주입 시: 검증된 규칙 > 반복 이슈 > 최근 3일 상세 피드백
"""
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from kitty.utils import logger

FEEDBACK_DIR = Path("feedback")
MAX_ENTRIES = 14
PROMPT_RECENT = 5
REFLECTION_RECENT = 10  # 반성문 요약에 사용할 최근 항목 수
RECURRING_THRESHOLD = 2
LEARNED_THRESHOLD = 2


def _path(agent_name: str) -> Path:
    FEEDBACK_DIR.mkdir(exist_ok=True)
    safe = agent_name.replace("/", "_").replace(" ", "_")
    return FEEDBACK_DIR / f"{safe}.json"


def load_entries(agent_name: str) -> list[dict[str, Any]]:
    path = _path(agent_name)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_entry(agent_name: str, entry: dict[str, Any]) -> None:
    """새 평가 항목 추가 (날짜가 같으면 덮어씀). 원자적 쓰기로 파일 손상 방지."""
    entries = load_entries(agent_name)
    entries = [e for e in entries if e.get("date") != entry.get("date")]
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]
    target = _path(agent_name)
    try:
        fd = tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", dir=target.parent, delete=False, encoding="utf-8"
        )
        fd.write(json.dumps(entries, ensure_ascii=False, indent=2))
        fd.flush()
        fd.close()
        Path(fd.name).replace(target)
    except Exception as e:
        logger.warning(f"[피드백] {agent_name} 원자적 쓰기 실패, 직접 쓰기로 폴백: {e}")
        target.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ── 누적 요약 분석 ──────────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> set[str]:
    """피드백 텍스트에서 핵심 키워드/구문을 추출"""
    if not text:
        return set()
    words = text.lower().replace(",", " ").replace(".", " ").replace("—", " ").split()
    stop_words = {
        "있습니다", "합니다", "입니다", "됩니다", "하세요", "해야", "필요",
        "것을", "위해", "경우", "때문", "시에", "으로", "에서", "대한",
        "이상", "이하", "이후", "보다", "때의", "점수", "달성",
        "the", "a", "an", "is", "and", "or", "for", "to", "of", "in",
    }
    filtered = [w for w in words if w not in stop_words and len(w) > 1]
    keywords: set[str] = set()
    for i in range(len(filtered)):
        keywords.add(filtered[i])
        if i + 1 < len(filtered):
            keywords.add(f"{filtered[i]} {filtered[i+1]}")
    return keywords


def _find_recurring_patterns(
    items: list[str], threshold: int = RECURRING_THRESHOLD,
) -> list[str]:
    """유사한 피드백을 그룹핑하여 반복 패턴을 찾음"""
    if len(items) < threshold:
        return []

    item_keywords = [(item, _extract_keywords(item)) for item in items]

    bigram_counter: Counter[str] = Counter()
    for _, kws in item_keywords:
        bigrams = [k for k in kws if " " in k]
        bigram_counter.update(bigrams)

    recurring: list[str] = []
    used_indices: set[int] = set()

    for bigram, count in bigram_counter.most_common():
        if count < threshold:
            break
        for idx in range(len(items) - 1, -1, -1):
            if idx not in used_indices and bigram in _extract_keywords(items[idx]):
                recurring.append(f"({count}회 반복) {items[idx]}")
                used_indices.add(idx)
                break

    word_counter: Counter[str] = Counter()
    for _, kws in item_keywords:
        singles = [k for k in kws if " " not in k and len(k) > 2]
        word_counter.update(singles)

    for word, count in word_counter.most_common(5):
        if count < threshold + 1:
            break
        for idx in range(len(items) - 1, -1, -1):
            if idx not in used_indices and word in items[idx].lower():
                recurring.append(f"({count}회 반복) {items[idx]}")
                used_indices.add(idx)
                break

    return recurring[:5]


def _build_accumulated_summary(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """전체 엔트리에서 누적 요약 생성"""
    improvements = [e["improvement"] for e in entries if e.get("improvement")]
    good_patterns = [e["good_pattern"] for e in entries if e.get("good_pattern")]

    return {
        "recurring_issues": _find_recurring_patterns(improvements),
        "learned_rules": _find_recurring_patterns(good_patterns, LEARNED_THRESHOLD),
    }


# ── 프롬프트 생성 ────────────────────────────────────────────────────────────────

def get_feedback_prompt(agent_name: str) -> str:
    """system_prompt 끝에 주입할 누적 피드백 블록 반환. 없으면 빈 문자열."""
    entries = load_entries(agent_name)
    if not entries:
        return ""

    # ── 1. 점수 트렌드 ──────────────────────────────────────────────────────────
    all_scores = [e.get("score", "?") for e in entries]
    trend_str = " → ".join(str(s) for s in all_scores[-7:])

    recent_scores = [s for s in all_scores[-5:] if isinstance(s, (int, float))]
    avg_score = sum(recent_scores) / len(recent_scores) if recent_scores else 50
    if len(recent_scores) >= 3:
        first_half = sum(recent_scores[:len(recent_scores) // 2]) / max(1, len(recent_scores) // 2)
        second_half = sum(recent_scores[len(recent_scores) // 2:]) / max(
            1, len(recent_scores) - len(recent_scores) // 2
        )
        if second_half > first_half + 5:
            trend_label = "📈 개선 중"
        elif second_half < first_half - 5:
            trend_label = "📉 하락 중 — 개선 시급"
        else:
            trend_label = "➡️ 유지"
    else:
        trend_label = ""

    lines = [
        "",
        "=" * 60,
        "[누적 성과 피드백]",
        f"점수 추이 (최근 7일): {trend_str} {trend_label}",
        f"최근 5일 평균: {avg_score:.0f}/100",
        "",
    ]

    # ── 2. 반성문 요약 → 강력 인스트럭션 ─────────────────────────────────────
    reflection_entries = entries[-REFLECTION_RECENT:]
    reflections = [e.get("reflection", "") for e in reflection_entries if e.get("reflection")]
    low_score_reflections = [
        e.get("reflection", "") for e in reflection_entries
        if e.get("reflection") and isinstance(e.get("score"), (int, float)) and e.get("score", 100) <= 60
    ]

    if reflections:
        lines.append("╔══════════════════════════════════════════════════════════╗")
        lines.append("║  ⚠️  반성 인스트럭션 — 이 규칙을 반드시 따르세요  ⚠️  ║")
        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.append("아래는 실제 실패에서 도출된 절대 금지 패턴입니다. 무조건 회피하세요:")
        lines.append("")
        # 저점수(≤60) 반성문 우선 표시
        shown = set()
        for r in low_score_reflections[-5:]:
            if r and r not in shown:
                lines.append(f"  ❌ {r}")
                shown.add(r)
        for r in reflections[-5:]:
            if r and r not in shown:
                lines.append(f"  ⚠️ {r}")
                shown.add(r)
        lines.append("")
        lines.append("위 반성 내용이 반복된다면 즉시 판단을 바꾸세요. 같은 실수의 반복은 용납되지 않습니다.")
        lines.append("")

    # ── 3. 누적 요약: 검증된 규칙 + 반복 이슈 ─────────────────────────────────
    accumulated = _build_accumulated_summary(entries)

    if accumulated["learned_rules"]:
        lines.append("[검증된 좋은 패턴 — 반드시 유지하세요]")
        for rule in accumulated["learned_rules"]:
            lines.append(f"  ✅ {rule}")
        lines.append("")

    if accumulated["recurring_issues"]:
        lines.append("[반복 이슈 — 최우선으로 개선하세요]")
        for issue in accumulated["recurring_issues"]:
            lines.append(f"  ⚠️ {issue}")
        lines.append("")

    # ── 4. 최근 상세 피드백 ──────────────────────────────────────────────────────
    recent = entries[-PROMPT_RECENT:]
    lines.append(f"[최근 {len(recent)}일 상세 피드백]")
    for e in reversed(recent):
        score = e.get("score", "?")
        date = e.get("date", "")
        summary = e.get("summary", "")
        improvement = e.get("improvement", "")
        good_pattern = e.get("good_pattern", "")

        score_bar = "█" * (score // 10) + "░" * (10 - score // 10) if isinstance(score, (int, float)) else ""
        lines.append(f"\n  [{date}] {score_bar} {score}/100")
        if summary:
            lines.append(f"    요약: {summary}")
        if good_pattern:
            lines.append(f"    ✅ 유지: {good_pattern}")
        if improvement:
            lines.append(f"    🔧 개선: {improvement}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(
        "반성 인스트럭션 > 검증된 패턴 > 반복 이슈 순으로 우선 적용하세요. "
        "이 피드백은 실제 성과 데이터에서 누적 도출된 것입니다."
    )

    return "\n".join(lines)
