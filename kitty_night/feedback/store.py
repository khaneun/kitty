"""Night mode 에이전트 성과 피드백 저장소 — night-feedback/ 경로 사용"""
import json
from pathlib import Path
from typing import Any

FEEDBACK_DIR = Path("night-feedback")
MAX_ENTRIES = 14
PROMPT_ENTRIES = 5


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


def get_feedback_prompt(agent_name: str) -> str:
    entries = load_entries(agent_name)
    if not entries:
        return ""

    all_scores = [e.get("score", "?") for e in entries]
    trend_str = " -> ".join(str(s) for s in all_scores[-7:])

    recent_scores = [s for s in all_scores[-5:] if isinstance(s, (int, float))]
    if len(recent_scores) >= 3:
        first_half = sum(recent_scores[: len(recent_scores) // 2]) / max(1, len(recent_scores) // 2)
        second_half = sum(recent_scores[len(recent_scores) // 2 :]) / max(
            1, len(recent_scores) - len(recent_scores) // 2
        )
        if second_half > first_half + 5:
            trend_icon = "improving"
        elif second_half < first_half - 5:
            trend_icon = "declining — needs improvement"
        else:
            trend_icon = "stable"
    else:
        trend_icon = ""

    lines = [
        "",
        "[Past Performance Feedback — use this to improve your decisions]",
        f"Score trend: {trend_str} {trend_icon}",
    ]

    recent = entries[-PROMPT_ENTRIES:]
    for e in reversed(recent):
        score = e.get("score", "?")
        date = e.get("date", "")
        summary = e.get("summary", "")
        improvement = e.get("improvement", "")
        good_pattern = e.get("good_pattern", "")

        lines.append(f"\n- {date} ({score}/100): {summary}")
        if good_pattern:
            lines.append(f"  KEEP: {good_pattern}")
        if improvement:
            lines.append(f"  IMPROVE: {improvement}")

    recent_improvements = [e["improvement"] for e in entries[-3:] if e.get("improvement")]
    seen: set[str] = set()
    unique = [i for i in recent_improvements if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]
    if unique:
        lines.append(f"\n[Top Priority] {' / '.join(unique)}")

    return "\n".join(lines)
