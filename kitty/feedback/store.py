"""에이전트 성과 피드백 영속 저장소

각 에이전트별 JSON 파일에 최근 MAX_ENTRIES일 피드백을 누적 저장하고,
system_prompt에 주입할 텍스트를 생성한다.
"""
import json
import tempfile
from pathlib import Path
from typing import Any

from kitty.utils import logger

FEEDBACK_DIR = Path("feedback")
MAX_ENTRIES = 14   # 보관할 최대 일수 (2주)
PROMPT_ENTRIES = 5  # system_prompt에 노출할 최근 N일


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
    # 같은 날짜 항목 교체
    entries = [e for e in entries if e.get("date") != entry.get("date")]
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]
    target = _path(agent_name)
    # 원자적 쓰기: 임시 파일에 먼저 쓴 후 rename (동시 쓰기 시 JSON 손상 방지)
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


def get_feedback_prompt(agent_name: str) -> str:
    """system_prompt 끝에 주입할 피드백 블록 반환. 없으면 빈 문자열.

    포함 정보:
    - 점수 추이 (최근 7일)
    - 최근 N일 피드백 (요약 + 유지할 패턴 + 개선점)
    - 최우선 개선 과제 (최근 3일 중복 제거)
    """
    entries = load_entries(agent_name)
    if not entries:
        return ""

    # 점수 추이 (최근 7일)
    all_scores = [e.get("score", "?") for e in entries]
    trend_str = " → ".join(str(s) for s in all_scores[-7:])

    # 추이 방향 판단
    recent_scores = [s for s in all_scores[-5:] if isinstance(s, (int, float))]
    if len(recent_scores) >= 3:
        first_half = sum(recent_scores[:len(recent_scores) // 2]) / max(1, len(recent_scores) // 2)
        second_half = sum(recent_scores[len(recent_scores) // 2:]) / max(1, len(recent_scores) - len(recent_scores) // 2)
        if second_half > first_half + 5:
            trend_icon = "📈 개선 중"
        elif second_half < first_half - 5:
            trend_icon = "📉 하락 중 — 개선 시급"
        else:
            trend_icon = "➡️ 유지"
    else:
        trend_icon = ""

    lines = [
        "",
        "[📊 과거 성과 피드백 — 아래를 참고해 판단을 지속적으로 개선하세요]",
        f"점수 추이: {trend_str} {trend_icon}",
    ]

    # 최근 N일 피드백 (최신 순)
    recent = entries[-PROMPT_ENTRIES:]
    for e in reversed(recent):
        score = e.get("score", "?")
        date = e.get("date", "")
        summary = e.get("summary", "")
        improvement = e.get("improvement", "")
        good_pattern = e.get("good_pattern", "")

        lines.append(f"\n• {date} ({score}/100): {summary}")
        if good_pattern:
            lines.append(f"  ✅ 유지: {good_pattern}")
        if improvement:
            lines.append(f"  💡 개선: {improvement}")

    # 최우선 개선 과제 (최근 3일, 중복 제거)
    recent_improvements = [e["improvement"] for e in entries[-3:] if e.get("improvement")]
    seen: set[str] = set()
    unique = [i for i in recent_improvements if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]
    if unique:
        lines.append(f"\n[최우선 개선 과제] {' / '.join(unique)}")

    return "\n".join(lines)
