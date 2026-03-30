"""에이전트 성과 피드백 영속 저장소

각 에이전트별 JSON 파일에 최근 MAX_ENTRIES일 피드백을 누적 저장하고,
system_prompt에 주입할 텍스트를 생성한다.
"""
import json
from pathlib import Path
from typing import Any

FEEDBACK_DIR = Path("feedback")
MAX_ENTRIES = 10   # 보관할 최대 일수
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
    """새 평가 항목 추가 (날짜가 같으면 덮어씀)"""
    entries = load_entries(agent_name)
    # 같은 날짜 항목 교체
    entries = [e for e in entries if e.get("date") != entry.get("date")]
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]
    _path(agent_name).write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_feedback_prompt(agent_name: str) -> str:
    """system_prompt 끝에 주입할 피드백 블록 반환. 없으면 빈 문자열."""
    entries = load_entries(agent_name)
    if not entries:
        return ""

    recent = entries[-PROMPT_ENTRIES:]
    lines = ["", "[📊 과거 성과 피드백 - 아래 내용을 참고해 판단을 개선하세요]"]
    for e in recent:
        score = e.get("score", "?")
        summary = e.get("summary", "")
        lines.append(f"• {e['date']} (점수 {score}/10): {summary}")

    improvements = [e["improvement"] for e in recent[-3:] if e.get("improvement")]
    if improvements:
        # 중복 제거 후 마지막 3개
        seen: set[str] = set()
        unique = [i for i in improvements if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]
        lines.append(f"[개선 포인트] {' / '.join(unique)}")

    return "\n".join(lines)
