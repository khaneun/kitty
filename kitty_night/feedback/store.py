"""Night mode 에이전트 성과 피드백 저장소 — night-feedback/ 경로 사용

누적 요약 시스템:
  - 최근 14일 원본 엔트리 보관
  - 반복되는 개선사항 → "Recurring Issues" (3회 이상 유사 키워드)
  - 검증된 좋은 패턴 → "Learned Rules" (2회 이상 반복)
  - 프롬프트 주입 시: Learned Rules > Recurring Issues > 최근 3일 상세 피드백
"""
import json
from collections import Counter
from pathlib import Path
from typing import Any

FEEDBACK_DIR = Path("night-feedback")
MAX_ENTRIES = 14
PROMPT_RECENT = 3          # 상세 피드백으로 보여줄 최근 일수
RECURRING_THRESHOLD = 2    # 반복 이슈로 승격하는 최소 등장 횟수
LEARNED_THRESHOLD = 2      # 검증된 패턴으로 승격하는 최소 등장 횟수


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
    entries = load_entries(agent_name)
    entries = [e for e in entries if e.get("date") != entry.get("date")]
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]
    _path(agent_name).write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 누적 요약 분석 ──────────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> set[str]:
    """피드백 텍스트에서 핵심 키워드/구문을 추출 (간단한 n-gram 기반)"""
    if not text:
        return set()
    # 소문자 변환 후 불용어 제거
    words = text.lower().replace(",", " ").replace(".", " ").split()
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "and", "or", "but", "for", "to", "of", "in", "on", "at", "by", "with",
        "from", "that", "this", "it", "as", "if", "when", "than", "more", "not",
        "no", "do", "should", "would", "could", "can", "will", "may", "need",
        "also", "only", "just", "even", "still", "very", "too", "so", "such",
    }
    filtered = [w for w in words if w not in stop_words and len(w) > 2]
    # 2-gram 추출
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

    # 각 항목의 키워드 추출
    item_keywords = [(item, _extract_keywords(item)) for item in items]

    # 2-gram 빈도 카운팅
    bigram_counter: Counter[str] = Counter()
    for _, kws in item_keywords:
        bigrams = [k for k in kws if " " in k]
        bigram_counter.update(bigrams)

    # 빈도 높은 bigram을 포함하는 원본 항목을 대표로 선택
    recurring: list[str] = []
    used_indices: set[int] = set()

    for bigram, count in bigram_counter.most_common():
        if count < threshold:
            break
        # 이 bigram을 포함하는 가장 최근 항목을 대표로
        for idx in range(len(items) - 1, -1, -1):
            if idx not in used_indices and bigram in _extract_keywords(items[idx]):
                recurring.append(f"({count}x) {items[idx]}")
                used_indices.add(idx)
                break

    # bigram으로 못 잡은 단독 반복 키워드도 체크
    word_counter: Counter[str] = Counter()
    for _, kws in item_keywords:
        singles = [k for k in kws if " " not in k and len(k) > 4]
        word_counter.update(singles)

    for word, count in word_counter.most_common(5):
        if count < threshold + 1:  # 단일 단어는 더 높은 임계치
            break
        for idx in range(len(items) - 1, -1, -1):
            if idx not in used_indices and word in items[idx].lower():
                recurring.append(f"({count}x) {items[idx]}")
                used_indices.add(idx)
                break

    return recurring[:5]  # 최대 5개


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
            trend_label = "📈 improving"
        elif second_half < first_half - 5:
            trend_label = "📉 declining — action needed"
        else:
            trend_label = "➡️ stable"
    else:
        trend_label = ""

    lines = [
        "",
        "=" * 60,
        "[ACCUMULATED PERFORMANCE FEEDBACK]",
        f"Score trend (recent 7): {trend_str} {trend_label}",
        f"Average (recent 5): {avg_score:.0f}/100",
        "",
    ]

    # ── 2. 누적 요약: 검증된 규칙 + 반복 이슈 ─────────────────────────────────
    accumulated = _build_accumulated_summary(entries)

    if accumulated["learned_rules"]:
        lines.append("[PROVEN GOOD PATTERNS — Keep doing these]")
        for rule in accumulated["learned_rules"]:
            lines.append(f"  ✅ {rule}")
        lines.append("")

    if accumulated["recurring_issues"]:
        lines.append("[RECURRING ISSUES — Fix these with highest priority]")
        for issue in accumulated["recurring_issues"]:
            lines.append(f"  ⚠️ {issue}")
        lines.append("")

    # ── 3. 최근 상세 피드백 ──────────────────────────────────────────────────────
    recent = entries[-PROMPT_RECENT:]
    lines.append(f"[RECENT FEEDBACK — last {len(recent)} sessions]")
    for e in reversed(recent):
        score = e.get("score", "?")
        date = e.get("date", "")
        summary = e.get("summary", "")
        improvement = e.get("improvement", "")
        good_pattern = e.get("good_pattern", "")

        score_bar = "█" * (score // 10) + "░" * (10 - score // 10) if isinstance(score, (int, float)) else ""
        lines.append(f"\n  [{date}] {score_bar} {score}/100")
        if summary:
            lines.append(f"    Summary: {summary}")
        if good_pattern:
            lines.append(f"    ✅ KEEP: {good_pattern}")
        if improvement:
            lines.append(f"    🔧 FIX: {improvement}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(
        "Apply the PROVEN PATTERNS and address RECURRING ISSUES in your decisions. "
        "These are derived from accumulated real performance data."
    )

    return "\n".join(lines)
