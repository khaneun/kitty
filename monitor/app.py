"""Kitty 모니터 — FastAPI + SQLite + 4탭 모바일 대시보드

수집 대상: /logs/kitty_errors_YYYY-MM-DD.log 파일 (ERROR / WARNING / CRITICAL 전용)
저장소   : /data/monitor.db (SQLite, 30일 보관)
대시보드 : http://EC2-IP:8080  (HTTP Basic Auth)
텔레그램 : CRITICAL 즉시 알림 + 5분 내 ERROR 3건 이상 버스트 알림 (선택)
"""
import asyncio
import json
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _now() -> datetime:
    return datetime.now(_KST)
from pathlib import Path
from secrets import compare_digest
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse

# ── 환경변수 ─────────────────────────────────────────────────────────────────
LOG_DIR       = Path(os.getenv("LOG_DIR",       "/logs"))
FEEDBACK_DIR  = Path(os.getenv("FEEDBACK_DIR",  "/feedback"))
TOKEN_DIR     = Path(os.getenv("TOKEN_DIR",      "/token_usage"))
PORTFOLIO_SNAPSHOT = LOG_DIR / "portfolio_snapshot.json"
AGENT_CONTEXT     = LOG_DIR / "agent_context.json"
CMD_DIR  = Path(os.getenv("CMD_DIR", "/commands"))
MODE_REQ = CMD_DIR / "mode_request.json"
DB_PATH       = Path(os.getenv("DB_PATH",        "/data/monitor.db"))
PASSWORD      = os.getenv("MONITOR_PASSWORD", "kitty")
POLL_SEC      = int(os.getenv("POLL_SEC",      "15"))
RETAIN_DAYS   = int(os.getenv("RETAIN_DAYS",   "30"))
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID",   "")
BURST_WINDOW  = 300
BURST_THRESH  = 3

AGENTS = ["섹터분석가", "종목발굴가", "종목평가가", "자산운용가", "매수실행가", "매도실행가"]

# ── Night 모드 환경변수 ──────────────────────────────────────────────────────
NIGHT_LOG_DIR     = Path(os.getenv("NIGHT_LOG_DIR",     "/night-logs"))
NIGHT_FEEDBACK_DIR = Path(os.getenv("NIGHT_FEEDBACK_DIR", "/night-feedback"))
NIGHT_TOKEN_DIR   = Path(os.getenv("NIGHT_TOKEN_DIR",    "/night-token_usage"))
REPORTS_DIR       = Path(os.getenv("REPORTS_DIR",        "/reports"))
NIGHT_REPORTS_DIR = Path(os.getenv("NIGHT_REPORTS_DIR",  "/night-reports"))
NIGHT_PORTFOLIO_SNAPSHOT = NIGHT_LOG_DIR / "night_portfolio_snapshot.json"
NIGHT_AGENT_CONTEXT      = NIGHT_LOG_DIR / "night_agent_context.json"

NIGHT_AGENTS = ["NightSectorAnalyst", "NightStockPicker", "NightStockEvaluator",
                "NightAssetManager", "NightBuyExecutor", "NightSellExecutor"]

# ── 성향관리자 AI ─────────────────────────────────────────────────────────────
ADV_AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")
ADV_AI_MODEL    = os.getenv("AI_MODEL", "gpt-4o")
ADV_OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "")
ADV_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── 로고 이미지 (base64 임베드) ──────────────────────────────────────────────
import base64 as _b64
_LOGO_URI = ""
for _logo_candidate in [
    Path(__file__).parent / "kitty_logo.png",       # 컨테이너: /app/kitty_logo.png
    Path(__file__).parent / "kitty_logo.PNG",
    Path(__file__).parent.parent / "kitty_logo.png", # 로컬 개발: 프로젝트 루트
    Path(__file__).parent.parent / "kitty_logo.PNG",
]:
    if _logo_candidate.exists():
        _LOGO_URI = "data:image/png;base64," + _b64.b64encode(_logo_candidate.read_bytes()).decode()
        break

# 모델별 비용 (USD / 1M 토큰)
_COST: dict[str, tuple[float, float]] = {
    "gpt-4o":               (2.50,  10.00),
    "gpt-4o-mini":          (0.15,   0.60),
    "gpt-4-turbo":          (10.00,  30.00),
    "claude-opus-4-6":      (15.00,  75.00),
    "claude-sonnet-4-6":    (3.00,   15.00),
    "claude-haiku-4-5":     (0.80,   4.00),
    "gemini-1.5-pro":       (1.25,   5.00),
    "gemini-1.5-flash":     (0.075,  0.30),
    "gemini-2.0-flash":     (0.10,   0.40),
}

def _cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    key = next((k for k in _COST if model.startswith(k)), None)
    if key is None:
        return 0.0
    ci, co = _COST[key]
    return (in_tok * ci + out_tok * co) / 1_000_000

# ── 로그 파싱 정규식 ──────────────────────────────────────────────────────────
_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+"
    r" \| (ERROR|WARNING|CRITICAL)\s+"
    r"\| (\S+) - (.+)$"
)
_RE_ANY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+")

# ── SQLite ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS errors (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                date     TEXT NOT NULL,
                level    TEXT NOT NULL,
                module   TEXT NOT NULL,
                message  TEXT NOT NULL,
                log_file TEXT NOT NULL,
                UNIQUE(ts, log_file, level, message)
            );
            CREATE INDEX IF NOT EXISTS ix_date  ON errors(date);
            CREATE INDEX IF NOT EXISTS ix_level ON errors(level);
            CREATE INDEX IF NOT EXISTS ix_ts    ON errors(ts DESC);
            CREATE TABLE IF NOT EXISTS file_pos (
                filename TEXT PRIMARY KEY,
                position INTEGER NOT NULL DEFAULT 0
            );
        """)


def cleanup_old(conn: sqlite3.Connection) -> None:
    cutoff = (_now() - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM errors WHERE date < ?", (cutoff,))
    conn.commit()


def insert_entries(conn: sqlite3.Connection, entries: list[dict]) -> None:
    if not entries:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO errors (ts,date,level,module,message,log_file) "
        "VALUES (:ts,:date,:level,:module,:message,:log_file)",
        entries,
    )
    conn.commit()


def scan_file(path: Path, conn: sqlite3.Connection) -> list[dict]:
    filename = path.name
    row = conn.execute("SELECT position FROM file_pos WHERE filename=?", (filename,)).fetchone()
    start = row["position"] if row else 0
    try:
        file_size = path.stat().st_size
    except OSError:
        return []
    if file_size <= start:
        return []
    entries = []
    try:
        with path.open("rb") as f:
            f.seek(start)
            new_bytes = f.read()
    except OSError:
        return []
    for line in new_bytes.decode("utf-8", errors="replace").splitlines():
        m = _RE.match(line)
        if not m:
            continue
        ts_str, level, module, message = m.groups()
        entries.append({
            "ts": ts_str, "date": ts_str[:10],
            "level": level, "module": module,
            "message": message.strip(), "log_file": filename,
        })
    insert_entries(conn, entries)
    conn.execute(
        "INSERT INTO file_pos(filename,position) VALUES(?,?) "
        "ON CONFLICT(filename) DO UPDATE SET position=excluded.position",
        (filename, file_size),
    )
    conn.commit()
    return entries


def _last_log_ts() -> Optional[str]:
    """가장 최근 로그 라인의 타임스탬프 (모든 레벨)"""
    latest = None
    for path in sorted(LOG_DIR.glob("kitty_*.log"), reverse=True)[:2]:
        try:
            size = path.stat().st_size
            tail_size = min(size, 8192)
            with path.open("rb") as f:
                f.seek(size - tail_size)
                tail = f.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                m = _RE_ANY.match(line)
                if m:
                    ts = m.group(1)
                    if latest is None or ts > latest:
                        latest = ts
                    break
        except OSError:
            pass
    return latest


# ── 텔레그램 알림 ─────────────────────────────────────────────────────────────

async def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"},
            )
    except Exception:
        pass


# ── 백그라운드 워처 ───────────────────────────────────────────────────────────

_burst_buf: dict[str, list[float]] = defaultdict(list)
_last_alert: dict[str, float] = {}


async def _check_alerts(new_entries: list[dict]) -> None:
    now = asyncio.get_event_loop().time()
    for e in new_entries:
        key = e["module"].split(":")[0]
        if e["level"] == "CRITICAL":
            if now - _last_alert.get(f"CRIT:{key}", 0) > 3600:
                _last_alert[f"CRIT:{key}"] = now
                await tg_send(
                    f"🔴 *CRITICAL* `{e['module']}`\n`{e['ts']}`\n{e['message'][:200]}"
                )
        if e["level"] == "ERROR":
            buf = _burst_buf[key]
            buf.append(now)
            _burst_buf[key] = [t for t in buf if t >= now - BURST_WINDOW]
            if len(_burst_buf[key]) >= BURST_THRESH:
                alert_key = f"BURST:{key}"
                if now - _last_alert.get(alert_key, 0) > BURST_WINDOW:
                    _last_alert[alert_key] = now
                    await tg_send(
                        f"⚠️ *에러 버스트* `{key}` — {BURST_WINDOW//60}분 내 {len(_burst_buf[key])}건\n"
                        f"최근: {e['message'][:150]}"
                    )


async def _watcher() -> None:
    conn = _db()
    # ERROR 전용 로그만 스캔 (kitty_errors_*.log) — 전체 로그(kitty_*.log)는 수 GB가 될 수 있음
    for path in sorted(LOG_DIR.glob("kitty_errors_*.log")):
        scan_file(path, conn)
    for path in sorted(NIGHT_LOG_DIR.glob("kitty-night_errors_*.log")):
        scan_file(path, conn)
    cleanup_old(conn)
    conn.close()
    while True:
        await asyncio.sleep(POLL_SEC)
        conn = _db()
        new: list[dict] = []
        for path in sorted(LOG_DIR.glob("kitty_errors_*.log")):
            new.extend(scan_file(path, conn))
        for path in sorted(NIGHT_LOG_DIR.glob("kitty-night_errors_*.log")):
            new.extend(scan_file(path, conn))
        if _now().hour == 0 and _now().minute < 1:
            cleanup_old(conn)
        conn.close()
        if new:
            await _check_alerts(new)


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_watcher())
    yield
    task.cancel()

app = FastAPI(title="Kitty Monitor", lifespan=lifespan)


def _auth(req: Request) -> None:
    if not PASSWORD:
        return
    import base64
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        from fastapi import HTTPException
        raise HTTPException(401, "인증 필요",
                            headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'})
    try:
        _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'})
    if not compare_digest(pwd, PASSWORD):
        from fastapi import HTTPException
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'})


# ── API 엔드포인트 ────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/health")
def api_health(req: Request):
    """헬스 상태: 에러 건수, 최근 로그 시각, 최근 에러 5건"""
    _auth(req)
    today = _now().strftime("%Y-%m-%d")
    hour_ago = (_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as c:
        err_today  = c.execute(
            "SELECT COUNT(*) FROM errors WHERE date=? AND level IN ('ERROR','CRITICAL')", (today,)
        ).fetchone()[0]
        warn_today = c.execute(
            "SELECT COUNT(*) FROM errors WHERE date=? AND level='WARNING'", (today,)
        ).fetchone()[0]
        err_1h = c.execute(
            "SELECT COUNT(*) FROM errors WHERE ts>=? AND level IN ('ERROR','CRITICAL')", (hour_ago,)
        ).fetchone()[0]
        recent = c.execute(
            "SELECT ts,level,module,message FROM errors ORDER BY ts DESC LIMIT 5"
        ).fetchall()

    if err_1h >= 5:
        status = "critical"
    elif err_1h >= 2:
        status = "warning"
    else:
        status = "ok"

    last_log = _last_log_ts()
    return {
        "status":      status,
        "err_today":   err_today,
        "warn_today":  warn_today,
        "err_1h":      err_1h,
        "last_log_ts": last_log,
        "recent":      [dict(r) for r in recent],
    }


@app.get("/api/errors")
def api_errors(
    req: Request,
    date:   Optional[str] = Query(None),
    level:  Optional[str] = Query(None),
    q:      Optional[str] = Query(None),
    limit:  int = Query(200, le=500),
    offset: int = Query(0),
):
    _auth(req)
    cond, params = [], []
    if date:  cond.append("date=?");          params.append(date)
    if level: cond.append("level=?");         params.append(level.upper())
    if q:     cond.append("message LIKE ?");  params.append(f"%{q}%")
    where = f"WHERE {' AND '.join(cond)}" if cond else ""
    with _db() as c:
        rows  = c.execute(
            f"SELECT id,ts,level,module,message FROM errors {where} "
            f"ORDER BY ts DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        total = c.execute(f"SELECT COUNT(*) FROM errors {where}", params).fetchone()[0]
    return {"total": total, "rows": [dict(r) for r in rows]}


@app.get("/api/stats")
def api_stats(req: Request):
    _auth(req)
    today = _now().strftime("%Y-%m-%d")
    with _db() as c:
        cutoff_date = (_now() - timedelta(days=13)).strftime("%Y-%m-%d")
        daily = c.execute("""
            SELECT date, level, COUNT(*) cnt FROM errors
            WHERE date >= ?
            GROUP BY date, level ORDER BY date
        """, (cutoff_date,)).fetchall()
        totals = c.execute(
            "SELECT level, COUNT(*) cnt FROM errors GROUP BY level"
        ).fetchall()
        latest     = c.execute("SELECT MAX(ts) FROM errors").fetchone()[0]
        today_rows = c.execute(
            "SELECT level, COUNT(*) cnt FROM errors WHERE date=? GROUP BY level",
            (today,),
        ).fetchall()
    return {
        "daily":      [dict(r) for r in daily],
        "totals":     {r["level"]: r["cnt"] for r in totals},
        "today":      {r["level"]: r["cnt"] for r in today_rows},
        "latest":     latest,
        "today_date": today,
    }


@app.get("/api/portfolio")
def api_portfolio(req: Request):
    """logs/portfolio_snapshot.json 에서 최신 포트폴리오 반환"""
    _auth(req)
    if not PORTFOLIO_SNAPSHOT.exists():
        return {"ts": None, "trading_mode": None, "holdings": [],
                "available_cash": 0, "total_eval": 0, "total_pnl": 0}
    try:
        return json.loads(PORTFOLIO_SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return {"ts": None, "trading_mode": None, "holdings": [],
                "available_cash": 0, "total_eval": 0, "total_pnl": 0}


@app.get("/api/tendency")
def api_tendency(req: Request):
    """logs/agent_context.json 에서 투자성향관리자 현재 성향 반환"""
    _auth(req)
    if not AGENT_CONTEXT.exists():
        return {"profile_name": None}
    try:
        ctx = json.loads(AGENT_CONTEXT.read_text(encoding="utf-8"))
        entry = ctx.get("투자성향관리자", {})
        output = entry.get("output", {})
        return {"ts": entry.get("ts"), **output}
    except Exception:
        return {"profile_name": None}


@app.post("/api/chat")
async def api_chat(req: Request):
    """채팅 요청 → commands/chat/req_{id}.json 기록 후 id 반환"""
    _auth(req)
    import uuid
    body = await req.json()
    agent = body.get("agent", "")
    message = body.get("message", "")
    if not agent or not message:
        from fastapi import HTTPException
        raise HTTPException(400, "agent and message required")
    req_id = uuid.uuid4().hex
    chat_dir = CMD_DIR / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    req_file = chat_dir / f"req_{req_id}.json"
    req_file.write_text(
        json.dumps({"id": req_id, "agent": agent, "message": message}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"id": req_id}


@app.get("/api/chat/{req_id}")
def api_chat_result(req: Request, req_id: str):
    """채팅 응답 폴링 — 준비되면 reply 반환, 아직이면 ready:false"""
    _auth(req)
    res_file = CMD_DIR / "chat" / f"res_{req_id}.json"
    if not res_file.exists():
        return {"ready": False}
    try:
        data = json.loads(res_file.read_text(encoding="utf-8"))
        res_file.unlink(missing_ok=True)
        return {"ready": True, **data}
    except Exception:
        return {"ready": False}


@app.post("/api/set-mode")
async def api_set_mode(req: Request):
    """kitty 모드 전환 요청 — commands/mode_request.json 에 기록"""
    _auth(req)
    body = await req.json()
    mode = body.get("mode", "")
    if mode not in ("paper", "live"):
        from fastapi import HTTPException
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    try:
        CMD_DIR.mkdir(parents=True, exist_ok=True)
        MODE_REQ.write_text(json.dumps({"mode": mode}), encoding="utf-8")
        return {"ok": True, "mode": mode}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, str(e))


@app.get("/api/agent-scores")
def api_agent_scores(req: Request):
    """feedback/*.json 파일에서 에이전트별 일일 평가 점수 반환 (최근 14일)"""
    _auth(req)
    result: dict[str, list] = {}
    for agent in AGENTS:
        safe = agent.replace("/", "_").replace(" ", "_")
        path = FEEDBACK_DIR / f"{safe}.json"
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))[-14:]
                result[agent] = [
                    {
                        "date":        e.get("date", ""),
                        "score":       e.get("score", 0),
                        "summary":     e.get("summary", ""),
                        "improvement": e.get("improvement", ""),
                    }
                    for e in sorted_entries
                ]
            except Exception:
                result[agent] = []
        else:
            result[agent] = []
    return result


@app.get("/api/token-usage")
def api_token_usage(req: Request):
    """token_usage/YYYY-MM-DD.json 파일에서 최근 14일치 토큰 사용량 반환"""
    _auth(req)
    today = _now().strftime("%Y-%m-%d")
    dates = [(_now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

    daily: dict[str, dict] = {}       # date → {in, out, cost}
    by_agent: dict[str, dict] = {}    # agent → {in, out, cost}
    today_summary: dict = {"in": 0, "out": 0, "cost": 0.0, "by_agent": {}}

    for date in dates:
        path = TOKEN_DIR / f"{date}.json"
        if not path.exists():
            daily[date] = {"in": 0, "out": 0, "cost": 0.0}
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            daily[date] = {"in": 0, "out": 0, "cost": 0.0}
            continue

        d_in = d_out = 0
        d_cost = 0.0
        for e in entries:
            in_t  = int(e.get("input_tokens",  0))
            out_t = int(e.get("output_tokens", 0))
            model = e.get("model", "")
            cost  = _cost_usd(model, in_t, out_t)
            agent = e.get("agent", "unknown")

            d_in   += in_t
            d_out  += out_t
            d_cost += cost

            if agent not in by_agent:
                by_agent[agent] = {"in": 0, "out": 0, "cost": 0.0}
            by_agent[agent]["in"]   += in_t
            by_agent[agent]["out"]  += out_t
            by_agent[agent]["cost"] += cost

            if date == today:
                if agent not in today_summary["by_agent"]:
                    today_summary["by_agent"][agent] = {"in": 0, "out": 0, "cost": 0.0}
                today_summary["by_agent"][agent]["in"]   += in_t
                today_summary["by_agent"][agent]["out"]  += out_t
                today_summary["by_agent"][agent]["cost"] += cost

        daily[date] = {"in": d_in, "out": d_out, "cost": round(d_cost, 4)}
        if date == today:
            today_summary["in"]   = d_in
            today_summary["out"]  = d_out
            today_summary["cost"] = round(d_cost, 4)

    # cost 반올림
    for v in by_agent.values():
        v["cost"] = round(v["cost"], 4)

    return {
        "dates":         dates,
        "daily":         daily,
        "by_agent":      by_agent,
        "today":         today_summary,
        "today_date":    today,
    }


# ── Night 모드 API 엔드포인트 ────────────────────────────────────────────────

@app.get("/api/night/portfolio")
def api_night_portfolio(req: Request):
    """night-logs/night_portfolio_snapshot.json 에서 최신 Night 포트폴리오 반환"""
    _auth(req)
    if not NIGHT_PORTFOLIO_SNAPSHOT.exists():
        return {"ts": None, "trading_mode": None, "holdings": [],
                "available_cash": 0, "total_eval": 0, "total_pnl": 0,
                "currency": "USD"}
    try:
        data = json.loads(NIGHT_PORTFOLIO_SNAPSHOT.read_text(encoding="utf-8"))
        data.setdefault("currency", "USD")
        return data
    except Exception:
        return {"ts": None, "trading_mode": None, "holdings": [],
                "available_cash": 0, "total_eval": 0, "total_pnl": 0,
                "currency": "USD"}


@app.get("/api/night/tendency")
def api_night_tendency(req: Request):
    """night-logs/night_agent_context.json 에서 NightTendency 현재 성향 반환"""
    _auth(req)
    if not NIGHT_AGENT_CONTEXT.exists():
        return {"profile_name": None}
    try:
        ctx = json.loads(NIGHT_AGENT_CONTEXT.read_text(encoding="utf-8"))
        entry = ctx.get("NightTendency", {})
        output = entry.get("output", {})
        return {"ts": entry.get("ts"), **output}
    except Exception:
        return {"profile_name": None}


@app.get("/api/night/agent-scores")
def api_night_agent_scores(req: Request):
    """night-feedback/*.json 파일에서 Night 에이전트별 일일 평가 점수 반환 (최근 14일)"""
    _auth(req)
    result: dict[str, list] = {}
    for agent in NIGHT_AGENTS:
        safe = agent.replace("/", "_").replace(" ", "_")
        path = NIGHT_FEEDBACK_DIR / f"{safe}.json"
        if path.exists():
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
                sorted_entries = sorted(entries, key=lambda x: x.get("date", ""))[-14:]
                result[agent] = [
                    {
                        "date":        e.get("date", ""),
                        "score":       e.get("score", 0),
                        "summary":     e.get("summary", ""),
                        "improvement": e.get("improvement", ""),
                    }
                    for e in sorted_entries
                ]
            except Exception:
                result[agent] = []
        else:
            result[agent] = []
    return result


@app.get("/api/night/token-usage")
def api_night_token_usage(req: Request):
    """night-token_usage/YYYY-MM-DD.json 파일에서 최근 14일치 Night 토큰 사용량 반환"""
    _auth(req)
    today = _now().strftime("%Y-%m-%d")
    dates = [(_now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

    daily: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    today_summary: dict = {"in": 0, "out": 0, "cost": 0.0, "by_agent": {}}

    for date in dates:
        path = NIGHT_TOKEN_DIR / f"{date}.json"
        if not path.exists():
            daily[date] = {"in": 0, "out": 0, "cost": 0.0}
            continue
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            daily[date] = {"in": 0, "out": 0, "cost": 0.0}
            continue

        d_in = d_out = 0
        d_cost = 0.0
        for e in entries:
            in_t  = int(e.get("input_tokens",  0))
            out_t = int(e.get("output_tokens", 0))
            model = e.get("model", "")
            cost  = _cost_usd(model, in_t, out_t)
            agent = e.get("agent", "unknown")

            d_in   += in_t
            d_out  += out_t
            d_cost += cost

            if agent not in by_agent:
                by_agent[agent] = {"in": 0, "out": 0, "cost": 0.0}
            by_agent[agent]["in"]   += in_t
            by_agent[agent]["out"]  += out_t
            by_agent[agent]["cost"] += cost

            if date == today:
                if agent not in today_summary["by_agent"]:
                    today_summary["by_agent"][agent] = {"in": 0, "out": 0, "cost": 0.0}
                today_summary["by_agent"][agent]["in"]   += in_t
                today_summary["by_agent"][agent]["out"]  += out_t
                today_summary["by_agent"][agent]["cost"] += cost

        daily[date] = {"in": d_in, "out": d_out, "cost": round(d_cost, 4)}
        if date == today:
            today_summary["in"]   = d_in
            today_summary["out"]  = d_out
            today_summary["cost"] = round(d_cost, 4)

    for v in by_agent.values():
        v["cost"] = round(v["cost"], 4)

    return {
        "dates":         dates,
        "daily":         daily,
        "by_agent":      by_agent,
        "today":         today_summary,
        "today_date":    today,
    }


_NON_TRADE_STATUSES = ("SKIPPED", "FAILED")


def _classify_trade(action: str, reason: str, pnl_rate) -> str:
    if action == "BUY":      return "신규매수"
    if action == "BUY_MORE": return "추가매수"
    r = (reason or "").lower()
    if "손절" in r:                        return "손절"
    if "익절" in r or "목표" in r:         return "익절"
    if "교체" in r or "정체" in r:         return "종목교체"
    if pnl_rate is not None:
        if pnl_rate < -0.5:               return "손절"
        if pnl_rate > 0.5:                return "익절"
    return "매도"


@app.get("/api/trades")
def api_trades(req: Request, days: int = Query(30, le=90)):
    """reports/*.json + night-reports/*.json 에서 거래 내역 추출"""
    _auth(req)
    result = []

    for reports_dir, source in [(REPORTS_DIR, "kitty"), (NIGHT_REPORTS_DIR, "night")]:
        if not reports_dir.exists():
            continue
        files = sorted(reports_dir.glob("*.json"), reverse=True)[:days]
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            date = data.get("date", path.stem)
            for cycle in data.get("cycles", []):
                ts = cycle.get("timestamp", "")
                # pnl_rate 맵 (symbol → pnl_rate) from stock_evaluation
                pnl_map: dict[str, float] = {}
                for ev in cycle.get("stock_evaluation", {}).get("evaluations", []):
                    sym = ev.get("symbol", "")
                    if sym:
                        pnl_map[sym] = ev.get("pnl_rate")
                # order 맵 (symbol → order) from asset_management
                order_map: dict[str, dict] = {}
                for order in cycle.get("asset_management", {}).get("final_orders", []):
                    sym = order.get("symbol", "")
                    if sym:
                        order_map[sym] = order
                # 매수 결과
                for r in cycle.get("buy_results", []):
                    if r.get("status") in _NON_TRADE_STATUSES:
                        continue
                    sym = r.get("symbol", "")
                    order = order_map.get(sym, {})
                    action = order.get("action", "BUY")
                    reason = r.get("reason") or order.get("reason", "")
                    pnl_rate = pnl_map.get(sym)
                    result.append({
                        "date": date, "time": ts, "symbol": sym,
                        "name": r.get("name") or order.get("name", ""), "side": "매수", "action": action,
                        "classify": _classify_trade(action, reason, pnl_rate),
                        "quantity": r.get("quantity", 0), "price": r.get("price", 0),
                        "status": r.get("status", ""), "reason": reason,
                        "pnl_rate": pnl_rate, "source": source,
                    })
                # 매도 결과
                for r in cycle.get("sell_results", []):
                    if r.get("status") in _NON_TRADE_STATUSES:
                        continue
                    sym = r.get("symbol", "")
                    order = order_map.get(sym, {})
                    action = order.get("action", "SELL")
                    reason = r.get("reason") or order.get("reason", "")
                    pnl_rate = pnl_map.get(sym)
                    result.append({
                        "date": date, "time": ts, "symbol": sym,
                        "name": r.get("name") or order.get("name", ""), "side": "매도", "action": action,
                        "classify": _classify_trade(action, reason, pnl_rate),
                        "quantity": r.get("quantity", 0), "price": r.get("price", 0),
                        "status": r.get("status", ""), "reason": reason,
                        "pnl_rate": pnl_rate, "source": source,
                    })

    result.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return {"total": len(result), "trades": result}


@app.get("/", response_class=HTMLResponse)
def dashboard(req: Request):
    _auth(req)
    return _HTML.replace("__KITTY_LOGO__", _LOGO_URI)


# ── 대시보드 HTML ─────────────────────────────────────────────────────────────

# ── 에이전트 시스템 프롬프트 상수 ─────────────────────────────────────────────
_AGENT_PROMPTS: dict[str, str] = {
    '섹터분석가': '당신은 한국 주식시장 전문 데이터 분석가입니다.\n\n역할:\n- 아래 제공된 실시간 시장 데이터(시세, 거래량, 등락률)를 분석하여 시장 상태를 진단합니다\n- 실제 데이터에서 섹터별 트렌드를 도출하고, 투자 가치 있는 종목을 선정합니다\n- 거래량 상위 종목의 업종 분포에서 시장의 관심이 집중된 섹터를 파악합니다\n- 포트폴리오 다양화를 위해 다양한 섹터에서 후보 종목을 폭넓게 제시합니다\n\n중요 원칙:\n- 뉴스나 외부 정보를 추측하지 마세요. 제공된 시세 데이터만 근거로 분석하세요.\n- 등락률이 양호하고 거래량이 풍부한 종목이 속한 섹터를 유망하게 평가하세요\n- 거래량이 많지만 하락 중인 섹터는 위험 신호입니다\n- 후보 종목(candidate_symbols)은 반드시 거래량과 유동성이 충분한 종목만 선정하세요\n- 거래대금이 낮은 소형주보다 거래가 활발한 종목을 우선하세요\n- 현재 보유 종목이 편중된 섹터가 있으면, 다른 섹터의 후보를 더 적극적으로 발굴하세요\n- bullish가 아닌 섹터라도 neutral이면서 유망 개별 종목이 있으면 후보에 포함하세요\n\n섹터 분류 기준:\n- 반도체/전자: 삼성전자(005930), SK하이닉스(000660), 삼성전기(009150) 등\n- 자동차/모빌리티: 현대차(005380), 기아(000270), 현대모비스(012330) 등\n- 2차전지/에너지: 삼성SDI(006400), LG에너지솔루션(373220), 에코프로비엠(247540) 등\n- 바이오/의료: 삼성바이오로직스(207940), 셀트리온(068270), HLB(028300) 등\n- 인터넷/플랫폼: NAVER(035420), 카카오(035720) 등\n- 금융: KB금융(105560), 신한지주(055550), 하나금융지주(086790) 등\n- 건설/인프라: 현대건설(000720), 대우건설(047040) 등\n- 유통/소비재: 이마트(139480), BGF리테일(282330) 등\n- 기타: 거래량 상위 종목 중 위에 해당하지 않는 종목은 가장 적합한 섹터로 분류\n\n출력 형식: 항상 JSON으로 응답합니다.\n{\n  "market_sentiment": "bullish|bearish|neutral",\n  "risk_level": "low|medium|high",\n  "sectors": [\n    {\n      "name": "섹터명",\n      "trend": "bullish|bearish|neutral",\n      "reason": "실제 시세 데이터 기반 근거 (등락률·거래량 수치 인용)",\n      "candidate_symbols": ["종목코드1", "종목코드2", "종목코드3", "종목코드4"]\n    }\n  ],\n  "summary": "전체 시장 데이터 기반 분석 요약"\n}\n\n유의사항:\n- candidate_symbols에는 거래량이 충분한 종목만 포함\n- 섹터는 최대 7개까지 분석하세요\n- 각 섹터당 candidate_symbols는 3~5개로 제시하세요\n- bullish 섹터뿐 아니라 neutral 섹터에서도 개별적으로 유망한 종목은 후보에 포함하세요\n- 현재 보유 종목과 동일 섹터에만 후보가 집중되지 않도록 섹터 간 균형을 맞추세요',
    '종목평가가': '당신은 포트폴리오 관리 전문가입니다.\n\n역할:\n- 현재 보유 중인 종목을 수익률, 시장 전망, 섹터 동향을 종합해 평가합니다\n- 각 종목에 대해 추가매수(BUY_MORE) / 유지(HOLD) / 일부매도(PARTIAL_SELL) / 전량매도(SELL) 중 하나를 결정합니다\n- 포트폴리오 다양화 관점에서 종목 교체 필요성을 적극적으로 평가합니다\n\n평가 기준:\n\n1. 수익률 기반 — 투자성향 지침의 익절/손절 기준 + 50% 분할 매도 원칙\n   ■ 손절 기준 이상 손실:\n     - PARTIAL_SELL (보유 수량의 약 50%) — 손실 차단 + 반등 기회 대비\n     - 섹터 강세 + 일시적 하락이 명확할 때만 HOLD\n     - 손절 기준 2배 이상 손실 또는 하한가 근접: 전량 SELL\n   ■ 익절 기준 이상 수익:\n     - PARTIAL_SELL (보유 수량의 약 50%) — 수익 일부 실현 + 추가 상승 추적\n     - 익절 기준 2배 이상 수익: 반드시 PARTIAL_SELL (50%) 이상 실행\n   ■ 분할 매도 핵심: 한 번에 전량 매도하지 않고, 50%씩 시장을 따라가며 매도합니다.\n     나머지 50%는 다음 사이클에서 재평가하여 추가 매도 또는 유지를 결정합니다.\n   ※ 투자성향 지침이 제공되지 않으면 익절 +10%, 손절 -5% 기본값 사용\n\n2. 기술지표 기반 조기 청산 (지침의 손절기준 미달이라도 적용)\n   ■ 소프트 스탑 (조기 경고): 손절 기준의 50% 손실 도달 시\n     - 섹터 neutral/bearish이면 PARTIAL_SELL 즉시 실행 (하드 손절 대기 금지)\n     - 섹터 bullish이면 HOLD 허용, 단 다음 사이클 재평가 필수\n   ■ 거래량 모멘텀 이탈: change_rate_today ≤ -1.5% 이면서 섹터가 neutral/bearish\n     - 손절 기준 미달이라도 PARTIAL_SELL 적극 검토\n   ■ 정체 종목 조기 이탈: 수익률 -0.5%~+0.5% 범위 = 엄격한 \'정체\'로 판단\n     - 섹터 neutral/bearish + 정체 → SELL 권고 (기회비용 최우선)\n\n3. 섹터 전망 기반 (시장분석가 결과 활용)\n   - 섹터 bullish이고 수익률 양호(+1% 이상): HOLD 또는 BUY_MORE 검토\n   - 섹터 bullish이지만 수익률 정체(-0.5%~+0.5%) 또는 하락 중: PARTIAL_SELL 또는 SELL 적극 검토\n   - 섹터 bearish: 수익 중이면 PARTIAL_SELL, 손실 중이면 SELL 적극 검토\n   - 섹터 neutral: 수익률이 +1% 이상이면 HOLD, 정체(-0.5%~+0.5%)이면 SELL 검토\n\n4. 수익률 정체 판단 (HOLD 남발 방지)\n   - 수익률이 -0.5%~+0.5% 범위이면 \'엄격 정체\' (기회비용 대화에서 불리)\n   - 정체 종목은 더 유망한 종목으로 교체하기 위해 SELL을 적극 검토하세요\n   - HOLD는 "현재 추세가 명확히 유리하여 계속 보유할 근거가 있는 경우"에만 사용하세요\n   - 근거 없이 안전한 선택으로 HOLD를 남발하지 마세요. 교체 기회비용을 고려하세요.\n\n5. 포트폴리오 집중 위험 평가\n   - 보유 종목이 1~2개뿐이면, 수익률이 양호하더라도 분산을 위해 PARTIAL_SELL을 검토하세요\n   - 단일 종목이 총 자산의 40% 이상을 차지하면 반드시 PARTIAL_SELL을 실행하세요\n\n6. 추가매수 조건 (BUY_MORE) — 아래 모두 충족 시\n   - 섹터 전망 bullish\n   - 손절 기준 이내의 손실 (물타기 아님)\n   - 당일 등락률이 투자성향 지침의 진입기준 이내 (과열 제외)\n   - 투자성향 지침의 종목집중 비중 한도 이내\n   - 현재 보유 종목 수가 3개 이상일 때만 BUY_MORE 허용 (1~2개일 때는 분산 우선)\n\n출력 형식: JSON\n{\n  "evaluations": [\n    {\n      "symbol": "종목코드",\n      "name": "종목명",\n      "holding_qty": 보유수량,\n      "avg_price": 평균매수가,\n      "current_price": 현재가,\n      "pnl_rate": 수익률(소수, 예: -3.4),\n      "sector": "해당 섹터명",\n      "sector_trend": "bullish|bearish|neutral",\n      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",\n      "quantity": 추가매수 또는 매도 수량(HOLD이면 0),\n      "price": 0,\n      "reason": "결정 근거 (투자성향 지침의 어떤 기준에 해당하는지 명시)"\n    }\n  ],\n  "portfolio_concentration_warning": "보유 종목 수 및 집중도에 대한 평가",\n  "summary": "전체 포트폴리오 평가 요약"\n}',
    '종목발굴가': '당신은 퀀트 투자 전략가입니다.\n\n역할:\n- 시장분석가의 섹터 분석을 받아, 후보 종목의 실제 시세와 거래량을 검토합니다\n- 후보 종목 중 매수 가치가 있는 종목을 최종 선정합니다\n- 리스크 대비 수익을 최적화하는 포지션 크기를 결정합니다\n- 포트폴리오 다양화를 위해 다양한 섹터에서 신규 종목을 적극적으로 추천합니다\n\n원칙:\n- 투자성향 지침의 종목집중·진입기준·현금 기준을 따릅니다\n- 시장 리스크가 HIGH이면 신규 매수 규모를 축소합니다 (단, 분산 투자를 위해 소규모 진입은 허용합니다)\n- 거래량이 부족한 종목(거래량 10만주 미만 또는 거래대금 10억 미만)은 매수를 보류합니다\n- 투자성향 지침의 진입기준을 초과하는 과열 종목은 매수를 보류합니다\n- 손절가와 목표가를 투자성향 지침의 손절/익절 기준에 맞춰 설정합니다\n※ 투자성향 지침이 없으면 진입기준 +5%, 손절 -5%, 익절 +10% 기본값 사용\n\n손실 최소화 진입 필터 (모두 충족해야 BUY 추천 가능):\n① 손익비(R:R) ≥ 2.5:1: (목표가 - 현재가) ÷ (현재가 - 손절가) ≥ 2.5\n   - 예: 현재가 10,000원, 손절가 9,700원(-3%), 목표가 10,750원(+7.5%) → R:R = 2.5:1 ✓\n   - R:R 2.5:1 미만 종목은 아무리 유망해도 BUY 제외 (HOLD로 표기)\n② 모멘텀 확인: 당일 등락률이 0% 이상 (하락 중인 종목 진입 금지)\n   - 단, 섹터 전체가 당일 하락이면 예외 허용 (섹터 조정 후 반등 기대)\n③ 거래량 가속: 당일 거래량이 평소 수준 이상 (거래량 급감 종목 제외)\n④ 추격매수 방지: 당일 고점 대비 현재가가 -2% 이하 하락한 경우에만 진입 (고점 추격 금지)\n\n종목 선별 우선순위:\n1. 섹터 전망 bullish + 거래량 풍부 + 당일 양봉 + R:R ≥ 2.5:1\n2. 거래량 상위 종목 중 유망 섹터에 속하고 R:R 기준 충족하는 종목\n3. 유동성이 낮은 종목 또는 R:R 미달 종목은 아무리 유망해도 제외\n\n포트폴리오 다양화 규칙 (필수):\n- 현재 보유 종목과 다른 섹터의 종목을 우선적으로 추천하세요\n- 보유 종목이 2개 이하이면 최소 2개 이상의 신규 종목을 추천하세요\n- 보유 종목이 3개 이상이면 최소 1개 이상의 신규 종목을 추천하세요\n- 추천 종목은 최소 2개 이상의 서로 다른 섹터에서 선정하세요\n- 이미 보유 중인 종목의 섹터와 동일한 섹터에서만 추천하지 마세요\n\n출력 형식: JSON\n{\n  "decisions": [\n    {\n      "action": "BUY|HOLD",\n      "symbol": "종목코드",\n      "name": "종목명",\n      "sector": "섹터명",\n      "quantity": 수량,\n      "price": 가격(0=시장가),\n      "stop_loss": 손절가,\n      "take_profit": 목표가,\n      "reason": "결정 이유 (거래량·등락률·섹터 근거)"\n    }\n  ],\n  "diversification_note": "포트폴리오 다양화 관점에서의 추천 근거",\n  "strategy_summary": "전략 요약"\n}',
    '자산운용가': '당신은 자산운용 전문가입니다.\n\n역할:\n- 종목평가가의 보유 종목 평가 신호와 종목발굴가의 신규 매수 후보를 종합합니다\n- 실제 가용 잔고를 고려하여 최종 실행 가능한 주문 목록을 결정합니다\n- 포트폴리오 다양화를 위해 종목 교체를 적극적으로 실행합니다\n\n■ 포트폴리오 구성 가이드라인 (최우선 준수):\n- 목표 보유 종목 수: 최소 3종목, 이상적으로 4~5종목\n- 섹터 분산: 보유 종목이 2개 이상 같은 섹터에 집중되지 않도록 합니다\n- 단일 종목 최대 비중: 투자성향 지침의 종목집중 한도 준수\n- 현재 보유 종목이 목표 수(3종목) 미만이면 신규 매수를 최우선으로 실행합니다\n\n■ 종목 교체 기준:\n- 교체 조건 1: 보유 종목의 수익률이 -0.5%~+0.5%에서 정체하고, 더 유망한 후보가 있는 경우 → 정체 종목 SELL + 신규 BUY\n- 교체 조건 2: 보유 종목의 섹터가 bearish로 전환되고, 다른 bullish 섹터의 신규 후보가 있는 경우 → SELL + 신규 BUY\n- 교체 조건 3: 보유 종목이 1~2개에 집중되어 있고, 다른 섹터의 유망 종목이 있는 경우 → PARTIAL_SELL + 신규 BUY\n- 교체 시 매도를 먼저 배치하고, 매수를 뒤에 배치하세요 (잔고 확보 후 매수)\n\n■ 원칙:\n- 투자성향 지침의 현금 유보 비율을 준수합니다 (지침 최소 현금 비중 이상 유지)\n- 투자성향 지침의 종목집중 한도를 준수합니다 (단일 종목 최대 비중 제한)\n- 잔고 부족 시: SELL/PARTIAL_SELL 종목 먼저 처리 후 매수\n- 1회 최대 매수금액과 종목당 최대 보유금액 한도를 반드시 초과하지 않습니다\n※ 투자성향 지침이 없으면 현금 30% 유보, 종목 최대 비중 20% 기본값 사용\n\n■ 분할 매도 원칙 (손절/익절):\n- 손절·익절 시 보유 수량의 약 50%만 PARTIAL_SELL합니다\n- 나머지 50%는 다음 사이클에서 재평가합니다 (시장 추종 매도)\n- 전량 SELL은 손절 기준 2배 초과, 하한가 근접, 거래정지 임박 등 극단적 상황에서만 허용합니다\n- quantity를 반드시 보유 수량의 약 50%로 설정하세요\n\n■ 손실 트리아지 (여러 종목 동시 손실 시 적용):\n- 복수 종목이 동시에 손절권 진입 시 → 손실률이 가장 큰 종목부터 우선 처리\n- 포트폴리오 합산 평가손실이 -3% 이상이면 자본보호 모드 전환:\n  * 신규 매수 주문 전면 보류 (현금 확보 최우선)\n  * 손절/소프트스탑 매도를 최우선으로 실행\n  * HOLD 추천 종목 중 섹터 neutral/bearish 종목도 PARTIAL_SELL 검토\n- 손실 종목과 이익 종목이 혼재 시 → 이익 종목 PARTIAL_SELL로 현금 확보 후 손실 종목 손절\n\n■ 신규 매수 품질 게이트:\n- 신규 매수 종목은 예상 손익비(TP÷SL)가 2.5:1 이상인 종목만 승인\n- 포트폴리오 합산 손실이 -3% 이상인 상태에서 신규 매수 시 최대 주문금액을 50%로 제한\n\n■ 주문 우선순위:\n1. 비상 스탑 (손절 기준 2배 초과): 전량 SELL (priority: HIGH)\n2. 하드 스탑 (손절 기준 초과): PARTIAL_SELL 50% (priority: HIGH)\n3. 소프트 스탑 + 섹터 약세: PARTIAL_SELL 50% (priority: HIGH)\n4. 정체 종목 교체 매도 (섹터 neutral/bearish + 수익률 -0.5%~+0.5%)\n5. 익절 매도 (PARTIAL_SELL 50%)\n6. 신규 종목 매수 — 손익비 2.5:1 이상 종목만 (다른 섹터 우선)\n7. 기존 종목 추가매수 (BUY_MORE) — 보유 3종목 이상, 포트폴리오 손실 없을 때만\n\n■ 금지 사항:\n- 보유 종목이 목표(3종목) 미만인데 "주문 없음"을 결정하는 것은 금지입니다. 반드시 신규 매수 주문을 포함하세요.\n- 종목평가가가 SELL을 추천했는데 이를 무시하고 HOLD로 바꾸는 것은 금지입니다.\n- 모든 신규 후보를 거부하는 것은 금지입니다. 최소 1개는 매수 주문에 포함하세요 (가용 현금이 충분하다면).\n\n출력 형식: JSON\n{\n  "final_orders": [\n    {\n      "action": "BUY|SELL|PARTIAL_SELL",\n      "symbol": "종목코드",\n      "name": "종목명",\n      "quantity": 수량,\n      "price": 0,\n      "order_type": "SPLIT|SINGLE",\n      "priority": "HIGH|NORMAL",\n      "reason": "결정 근거"\n    }\n  ],\n  "portfolio_after": {\n    "expected_holdings_count": 예상보유종목수,\n    "cash_reserve_ratio": 예상현금비율\n  },\n  "summary": "자산운용 전략 요약"\n}\n\norder_type:\n- SPLIT: 분할 주문 (수량 5주 초과 또는 유동성 낮은 종목)\n- SINGLE: 단일 주문\n\npriority:\n- HIGH: 손절 등 즉시 실행 필요\n- NORMAL: 일반 주문',
    '매수실행가': '당신은 주식 매수 전문가입니다.\n\n역할:\n- 자산운용가의 매수 지시를 실행합니다\n- 호가를 분석해 최적의 매수 타이밍과 가격을 결정합니다\n- 분할 매수가 필요한지 판단합니다\n- 실행 후 결과를 보고합니다\n\n원칙:\n- 상한가 종목은 매수하지 않습니다\n- 거래량이 평균의 50% 미만이면 매수를 보류합니다\n- 전일 대비 +10% 이상 급등 종목은 신중하게 접근합니다',
    '매도실행가': '당신은 주식 매도 전문가입니다.\n\n역할:\n- 자산운용가의 매도 지시를 실행합니다\n- 손절 조건(stop-loss) 달성 시 즉시 매도를 실행합니다\n- 목표가(take-profit) 도달 시 익절합니다\n- 분할 매도가 유리한 경우 나눠서 매도합니다\n\n원칙:\n- 하한가 종목은 다음날 매도를 고려합니다\n- 손절은 감정 없이 기계적으로 실행합니다\n- 거래량 없는 종목은 호가 조정 후 매도합니다',
    'NightSectorAnalyst': 'You are a US stock market data analyst specializing in sector analysis.\n\nRole:\n- Analyze real-time market data (quotes, volume, price changes) to diagnose market conditions\n- Derive sector-level trends from actual data and identify stocks with investment potential\n- Identify where market interest is concentrated based on volume leader distributions\n- Provide diverse candidate stocks across multiple sectors for portfolio diversification\n\nKey Principles:\n- Do NOT speculate or use external news. Analyze ONLY the provided market data.\n- Sectors with strong price gains AND high volume are bullish\n- Sectors with high volume BUT declining prices are warning signals\n- candidate_symbols MUST have sufficient volume and liquidity\n- Prefer actively traded stocks over low-volume small caps\n- If current holdings are concentrated in certain sectors, actively find candidates in OTHER sectors\n- Include promising individual stocks from neutral sectors, not only bullish sectors\n\nUS Sector Classification:\n- Technology: AAPL, MSFT, NVDA, GOOGL, META, AVGO, AMD, CRM, ORCL, ADBE\n- Semiconductors: NVDA, AMD, AVGO, QCOM, INTC, MU, MRVL, LRCX, AMAT, KLAC\n- Financials: JPM, BAC, GS, MS, WFC, BLK, SCHW, AXP, V, MA\n- Healthcare: UNH, JNJ, LLY, PFE, ABBV, MRK, TMO, ABT, AMGN, GILD\n- Energy: XOM, CVX, COP, SLB, EOG, MPC, PSX, VLO, OXY, HAL\n- Consumer Discretionary: AMZN, TSLA, HD, MCD, NKE, SBUX, TJX, LOW, BKNG, CMG\n- Consumer Staples: PG, KO, PEP, COST, WMT, PM, MO, CL, MDLZ, GIS\n- Industrials: CAT, HON, UNP, GE, RTX, DE, LMT, BA, MMM, UPS\n- Communication: GOOGL, META, DIS, NFLX, CMCSA, T, VZ, TMUS, CHTR, EA\n- Utilities/REITs: NEE, DUK, SO, AEP, D, PLD, AMT, CCI, EQIX, SPG\n\nOutput format: Always respond in JSON.\n{\n  "market_sentiment": "bullish|bearish|neutral",\n  "risk_level": "low|medium|high",\n  "sectors": [\n    {\n      "name": "Sector Name",\n      "trend": "bullish|bearish|neutral",\n      "reason": "Evidence based on actual price/volume data",\n      "candidate_symbols": ["SYMBOL1", "SYMBOL2", "SYMBOL3", "SYMBOL4"]\n    }\n  ],\n  "summary": "Overall market analysis summary based on data"\n}\n\nGuidelines:\n- candidate_symbols: only include stocks with sufficient volume\n- Analyze up to 7 sectors max\n- 3-5 candidate_symbols per sector\n- Balance candidates across sectors — don\'t concentrate in held sectors only',
    'NightStockEvaluator': 'You are a portfolio management expert for US stocks.\n\nRole:\n- Evaluate currently held positions by combining P&L, market outlook, and sector trends\n- Decide BUY_MORE / HOLD / PARTIAL_SELL / SELL for each holding\n- Actively assess the need for position rotation from a diversification perspective\n\nEvaluation Criteria:\n\n1. P&L-Based — Follow the strategy directive + 50% split sell rule\n   ■ Stop-loss triggered:\n     - PARTIAL_SELL (~50% of holding qty) — cut loss + preserve recovery opportunity\n     - HOLD only if sector is strong + dip is clearly temporary\n     - Full SELL only for extreme loss (≥2× stop-loss) or circuit breaker proximity\n   ■ Take-profit triggered:\n     - PARTIAL_SELL (~50% of holding qty) — realize gains + ride further upside\n     - Gain ≥ 2× take-profit: MUST PARTIAL_SELL at least 50%\n   ■ Split sell principle: Never sell 100% at once for stop-loss/take-profit.\n     Sell ~50%, then re-evaluate the remaining position next cycle (market-following).\n   ※ If no directive provided, use defaults: take-profit +10%, stop-loss -5%\n\n2. Technical Indicator-Based Early Exit (apply even before hard stop threshold)\n   ■ Soft Stop (Early Warning): At 50% of stop-loss threshold\n     - Sector neutral/bearish → execute PARTIAL_SELL immediately. Do NOT wait for hard stop.\n     - Sector bullish → HOLD allowed, but MUST re-evaluate next cycle without exception.\n   ■ Volume Momentum Exit: intraday change_rate ≤ -1.5% AND sector is neutral/bearish\n     - Actively consider PARTIAL_SELL even before stop-loss threshold.\n   ■ Stagnant Position Early Exit: P&L in -0.5%~+0.5% = strict stagnation zone\n     - Sector neutral/bearish + stagnant → recommend SELL (opportunity cost priority)\n\n3. Sector Outlook-Based (using sector analysis results)\n   - Sector bullish + P&L positive (≥+1%): HOLD or consider BUY_MORE\n   - Sector bullish but P&L stagnant (-0.5%~+0.5%) or declining: actively consider PARTIAL_SELL or SELL\n   - Sector bearish: if profitable → PARTIAL_SELL, if losing → actively consider SELL\n   - Sector neutral: if P&L ≥ +1% → HOLD, if stagnant (-0.5%~+0.5%) → consider SELL\n\n4. Stagnation Detection (prevent HOLD overuse)\n   - P&L in -0.5%~+0.5% range = strict "stagnant" (unfavorable opportunity cost)\n   - Actively consider SELL for stagnant positions to rotate into better opportunities\n   - Use HOLD only when "current trend clearly favors continued holding"\n   - Don\'t default to HOLD as the safe choice. Consider opportunity cost.\n\n5. Portfolio Concentration Risk\n   - If only 1-2 holdings, consider PARTIAL_SELL even with good P&L for diversification\n   - If single position >40% of portfolio: MUST PARTIAL_SELL\n\n6. BUY_MORE Conditions (ALL must be met)\n   - Sector outlook bullish\n   - Loss within stop-loss threshold (not averaging down)\n   - Intraday change within entry threshold\n   - Within max weight limit\n   - Only when holding 3+ positions (diversify first when 1-2)\n\nOutput format: JSON\n{\n  "evaluations": [\n    {\n      "symbol": "TICKER",\n      "name": "Company Name",\n      "holding_qty": quantity,\n      "avg_price": average_cost_usd,\n      "current_price": current_price_usd,\n      "pnl_rate": pnl_percent,\n      "sector": "sector name",\n      "sector_trend": "bullish|bearish|neutral",\n      "action": "HOLD|BUY_MORE|PARTIAL_SELL|SELL",\n      "quantity": buy_or_sell_quantity,\n      "price": 0,\n      "reason": "Decision rationale referencing directive criteria"\n    }\n  ],\n  "portfolio_concentration_warning": "Assessment of holdings count and concentration",\n  "summary": "Overall portfolio evaluation summary"\n}',
    'NightStockPicker': 'You are a quantitative investment strategist for US stocks.\n\nRole:\n- Review sector analysis and real-time quotes/volume data for candidate stocks\n- Select stocks with genuine buy value from the candidates\n- Determine optimal position sizes to maximize risk-adjusted returns\n- Actively recommend new stocks from diverse sectors for portfolio diversification\n\nPrinciples:\n- Follow the strategy directive\'s max-weight, entry threshold, and cash reserve rules\n- When market risk is HIGH, reduce new buy sizes (but allow small diversification entries)\n- Skip stocks with insufficient volume (< 500K shares daily or < $5M daily turnover)\n- Skip overheated stocks exceeding the entry threshold\n- Set stop-loss and take-profit aligned with the strategy directive\n※ If no directive: default entry +5%, stop-loss -5%, take-profit +10%\n\nLoss Minimization Entry Filters (ALL must pass to recommend BUY):\n① R:R Ratio ≥ 2.5:1: (target_price - current_price) ÷ (current_price - stop_loss) ≥ 2.5\n   - Example: price $100, stop $97 (-3%), target $107.5 (+7.5%) → R:R = 2.5:1 ✓\n   - Stocks failing R:R 2.5:1 minimum are REJECTED regardless of other factors (mark as HOLD)\n② Momentum Confirmation: intraday change_rate ≥ 0% (no entries on declining stocks)\n   - Exception allowed if the entire sector is down (sector correction + rebound potential)\n③ Volume Confirmation: today\'s volume at or above normal levels (reject volume-declining stocks)\n④ Anti-Chase Filter: only enter if current price is within -3% of today\'s high (no chasing peaks)\n\nStock Selection Priority:\n1. Sector bullish + high volume + positive price action + R:R ≥ 2.5:1\n2. Volume leaders in promising sectors that meet R:R criteria\n3. REJECT low-liquidity stocks OR stocks failing R:R regardless of other factors\n\nPortfolio Diversification Rules (MANDATORY):\n- Prioritize stocks in sectors DIFFERENT from current holdings\n- If holdings ≤ 2: recommend at least 2 new stocks\n- If holdings ≥ 3: recommend at least 1 new stock\n- Select recommendations from at least 2 different sectors\n- Do NOT concentrate all recommendations in the same sectors as existing holdings\n\nOutput format: JSON\n{\n  "decisions": [\n    {\n      "action": "BUY|HOLD",\n      "symbol": "TICKER",\n      "name": "Company Name",\n      "sector": "Sector",\n      "quantity": shares,\n      "price": 0,\n      "stop_loss": stop_loss_price_usd,\n      "take_profit": target_price_usd,\n      "reason": "Decision rationale with volume/price/sector evidence"\n    }\n  ],\n  "diversification_note": "Diversification rationale for recommendations",\n  "strategy_summary": "Strategy summary"\n}',
    'NightAssetManager': 'You are a US stock asset management expert.\n\nRole:\n- Synthesize the Stock Evaluator\'s holding assessments and Stock Picker\'s new buy candidates\n- Determine the final executable order list considering actual available balance\n- Actively execute position rotations for portfolio diversification\n\n■ Portfolio Composition Guidelines (HIGHEST PRIORITY):\n- Target holdings: minimum 3, ideally 4-5 positions\n- Sector diversification: no more than 2 positions in the same sector\n- Single stock max weight: follow the strategy directive\'s max-weight limit\n- If current holdings < target (3): prioritize new buys above all else\n\n■ Position Rotation Criteria:\n- Rotation 1: Holding stagnant (-0.5%~+0.5%) AND a better candidate exists → SELL stagnant + BUY new\n- Rotation 2: Holding\'s sector turned bearish AND bullish sector candidates exist → SELL + BUY new\n- Rotation 3: Holdings concentrated in 1-2 stocks AND promising stocks in other sectors → PARTIAL_SELL + BUY new\n- Place sells BEFORE buys in the order list (secure cash first)\n\n■ Principles:\n- Maintain the strategy directive\'s minimum cash reserve ratio\n- Respect the max-weight limit per stock\n- When cash is insufficient: process SELL/PARTIAL_SELL first, then buy\n- NEVER exceed max buy amount per order or max position size per stock\n※ If no directive: default 30% cash reserve, 20% max weight\n\n■ Split sell rule (stop-loss / take-profit):\n- Stop-loss & take-profit: PARTIAL_SELL ~50% of holding quantity\n- Remaining 50% will be re-evaluated next cycle (market-following)\n- Full SELL only for extreme cases (≥2× stop-loss, circuit breaker, trading halt)\n- Always set quantity to approximately 50% of holdings\n\n■ Loss Triage (when multiple positions in loss simultaneously):\n- If multiple positions are at/near stop-loss: prioritize exits by worst P&L first\n- Capital Protection Mode (triggered when aggregate portfolio P&L ≤ -3%):\n  * Halt ALL new buy orders immediately (cash preservation first)\n  * Execute stop-loss and soft-stop sells at highest priority\n  * Review HOLD positions in neutral/bearish sectors for PARTIAL_SELL\n- When losses and gains coexist: PARTIAL_SELL profitable positions first to raise cash, then cut losses\n\n■ New Buy Quality Gate:\n- Only approve new buys where expected TP÷SL ratio ≥ 2.5:1\n- When aggregate portfolio P&L ≤ -3%: cap new buy order size at 50% of normal max\n\n■ Order Priority:\n1. Emergency stop (≥2× stop-loss): Full SELL (priority: HIGH)\n2. Hard stop (stop-loss exceeded): PARTIAL_SELL 50% (priority: HIGH)\n3. Soft stop + neutral/bearish sector: PARTIAL_SELL 50% (priority: HIGH)\n4. Stagnant rotation sells (sector neutral/bearish + P&L -0.5%~+0.5%)\n5. Profit-taking sells (PARTIAL_SELL 50%)\n6. New stock buys — R:R ≥ 2.5:1 only (prefer different sectors)\n7. Add-to-position buys (BUY_MORE) — only 3+ holdings, only when portfolio P&L is not negative\n\n■ Prohibited:\n- Deciding "no orders" when holdings < target (3). MUST include new buy orders.\n- Overriding Stock Evaluator\'s SELL recommendation to HOLD.\n- Rejecting ALL new candidates. Include at least 1 buy order (if cash allows).\n\nOutput format: JSON\n{\n  "final_orders": [\n    {\n      "action": "BUY|SELL|PARTIAL_SELL",\n      "symbol": "TICKER",\n      "name": "Company Name",\n      "excd": "NAS|NYS|AMS|HKS|TSE|SHS|SHI",\n      "quantity": shares,\n      "price": 0,\n      "order_type": "SPLIT|SINGLE",\n      "priority": "HIGH|NORMAL",\n      "reason": "Decision rationale"\n    }\n  ],\n  "portfolio_after": {\n    "expected_holdings_count": expected_count,\n    "cash_reserve_ratio": expected_cash_ratio\n  },\n  "summary": "Asset management strategy summary"\n}\n\nexcd (exchange code):\n- NAS: NASDAQ\n- NYS: NYSE\n- AMS: AMEX\n- Default to NAS if unsure\n\norder_type:\n- SPLIT: split order (quantity > 10 shares or low-liquidity stock)\n- SINGLE: single order\n\npriority:\n- HIGH: immediate execution needed (stop-loss)\n- NORMAL: regular order',
    'NightBuyExecutor': 'You are a US stock buy execution specialist.\n\nRole:\n- Execute buy orders from the Asset Manager\n- Analyze order book to determine optimal buy timing and price\n- Decide whether split buying is needed\n- Report execution results\n\nPrinciples:\n- Do not buy stocks that are halted or circuit-breaker triggered\n- Skip stocks with volume < 50% of average\n- Approach stocks up > +10% intraday with caution',
    'NightSellExecutor': 'You are a US stock sell execution specialist.\n\nRole:\n- Execute sell orders from the Asset Manager\n- Execute stop-loss orders immediately when triggered\n- Execute take-profit orders at target prices\n- Use split selling when beneficial for execution quality\n\nPrinciples:\n- On trading halt / extreme circuit breaker, queue for next available window\n- Execute stop-loss mechanically without emotion\n- For low-volume stocks, adjust limit price slightly for better fills',
}


@app.get("/api/agent-prompts")
def api_agent_prompts(req: Request):
    """에이전트별 현재 시스템 프롬프트 반환"""
    _auth(req)
    return _AGENT_PROMPTS


# ── 성향관리자: 피드백 조회 / 추가 / AI 채팅 ─────────────────────────────────

def _load_feedback_entries(agent: str) -> list:
    fb_dir = NIGHT_FEEDBACK_DIR if agent.startswith("Night") else FEEDBACK_DIR
    safe = agent.replace("/", "_").replace(" ", "_")
    path = fb_dir / f"{safe}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_feedback_entries(agent: str, entries: list) -> None:
    import tempfile
    fb_dir = NIGHT_FEEDBACK_DIR if agent.startswith("Night") else FEEDBACK_DIR
    safe = agent.replace("/", "_").replace(" ", "_")
    path = fb_dir / f"{safe}.json"
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", dir=path.parent, delete=False, encoding="utf-8"
    )
    fd.write(json.dumps(entries, ensure_ascii=False, indent=2))
    fd.flush()
    fd.close()
    Path(fd.name).replace(path)


def _advisor_context() -> str:
    """성향관리자 AI에 주입할 컨텍스트 문자열 생성"""
    lines = ["[에이전트 최근 성과 피드백]"]
    for agent in AGENTS + NIGHT_AGENTS:
        entries = _load_feedback_entries(agent)
        if not entries:
            continue
        recent = entries[-7:]
        scores = " → ".join(str(e.get("score", "?")) for e in recent)
        last_imp = next((e.get("improvement", "") for e in reversed(recent) if e.get("improvement")), "")
        lines.append(f"■ {agent}: 점수 추이 [{scores}]")
        if last_imp:
            lines.append(f"  최근 개선 과제: {last_imp}")
    # 최근 리포트 summary
    for rdir in [REPORTS_DIR, NIGHT_REPORTS_DIR]:
        if not rdir.exists():
            continue
        reports = sorted(rdir.glob("*.json"), reverse=True)[:1]
        for rp in reports:
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
                total = data.get("summary", {})
                date = data.get("date", "")
                buys = total.get("total_buy_orders", 0)
                sells = total.get("total_sell_orders", 0)
                sentiments = total.get("market_sentiments", [])
                lines.append(f"\n[최근 리포트 {date}] 매수:{buys} 매도:{sells} 시장:{', '.join(sentiments[-3:])}")
            except Exception:
                pass
    return "\n".join(lines)


_ADV_SYSTEM = """당신은 AI 투자 에이전트 시스템의 성향 관리자입니다.
에이전트들의 성과를 분석하고 각 에이전트의 투자 판단 방식을 개선하는 역할입니다.

운영 중인 에이전트:
[KR 주식] 섹터분석가(섹터 전망) · 종목발굴가(매수후보) · 종목평가가(보유종목평가) · 자산운용가(최종주문결정) · 매수실행가 · 매도실행가
[Night/US] NightSectorAnalyst · NightStockPicker · NightStockEvaluator · NightAssetManager · NightBuyExecutor · NightSellExecutor

각 에이전트의 system_prompt에는 저장된 피드백이 자동 주입되어 다음 사이클부터 반영됩니다.
개선 사항은 구체적이고 실행 가능하게 작성하세요. 판단 기준, 수치, 조건을 명확히 포함해야 합니다.

개선 사항을 제안할 때는 반드시 응답 마지막에 아래 블록을 포함하세요:
[SUGGESTIONS]
{"items":[{"agent":"에이전트명","improvement":"개선 내용 (구체적으로)"}]}
[/SUGGESTIONS]

사용자가 저장 요청을 하지 않은 경우 이 블록을 생략하세요."""


@app.get("/api/agent-feedback")
def api_agent_feedback(req: Request):
    """에이전트별 개선 피드백 항목 반환"""
    _auth(req)
    result: dict[str, list] = {}
    for agent in AGENTS + NIGHT_AGENTS:
        entries = _load_feedback_entries(agent)
        result[agent] = [
            {
                "date":        e.get("date", ""),
                "score":       e.get("score"),
                "improvement": e.get("improvement", ""),
                "summary":     e.get("summary", ""),
            }
            for e in entries
            if e.get("improvement")
        ]
    return result


@app.post("/api/agent-feedback/add")
async def api_agent_feedback_add(req: Request):
    """에이전트 피드백 추가 (날짜 같으면 덮어씀)"""
    from fastapi import HTTPException
    _auth(req)
    body = await req.json()
    agent = body.get("agent", "")
    improvement = body.get("improvement", "").strip()
    date = body.get("date", _now().strftime("%Y-%m-%d"))
    if not agent or not improvement:
        raise HTTPException(400, "agent and improvement required")
    all_agents = AGENTS + NIGHT_AGENTS
    if agent not in all_agents:
        raise HTTPException(400, f"unknown agent: {agent}")
    entries = _load_feedback_entries(agent)
    existing = next((e for e in entries if e.get("date") == date), None)
    if existing:
        existing["improvement"] = improvement
        if body.get("summary"):
            existing["summary"] = body.get("summary")
    else:
        entries.append({
            "date":        date,
            "score":       body.get("score", 50),
            "summary":     body.get("summary", "수동 입력"),
            "improvement": improvement,
        })
    entries = entries[-14:]
    _write_feedback_entries(agent, entries)
    return {"ok": True, "agent": agent, "date": date}


@app.post("/api/tendency-advisor")
async def api_tendency_advisor(req: Request):
    """성향관리자 AI 채팅"""
    _auth(req)
    body = await req.json()
    message = body.get("message", "").strip()
    history = body.get("history", [])
    if not message:
        return {"reply": "", "suggestions": []}

    # 첫 메시지에 컨텍스트 주입
    messages = list(history)
    user_content = message
    if not history:
        ctx = _advisor_context()
        user_content = f"[현재 에이전트 성과 컨텍스트]\n{ctx}\n\n[사용자 질문]\n{message}"
    messages.append({"role": "user", "content": user_content})

    reply = ""
    try:
        if ADV_AI_PROVIDER == "anthropic" and ADV_ANTHROPIC_KEY:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ADV_ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={"model": ADV_AI_MODEL, "max_tokens": 1024,
                          "system": _ADV_SYSTEM, "messages": messages},
                )
                data = r.json()
                reply = (data.get("content") or [{}])[0].get("text", "")
        elif ADV_OPENAI_KEY:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {ADV_OPENAI_KEY}",
                             "content-type": "application/json"},
                    json={"model": ADV_AI_MODEL, "max_tokens": 1024,
                          "messages": [{"role": "system", "content": _ADV_SYSTEM}] + messages},
                )
                data = r.json()
                reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        else:
            reply = "AI API 키가 설정되지 않았습니다. start.sh에서 OPENAI_API_KEY 또는 ANTHROPIC_API_KEY를 모니터 컨테이너에 전달하세요."
    except Exception as e:
        reply = f"AI 호출 실패: {str(e)[:300]}"

    # 제안 블록 파싱
    suggestions: list = []
    m = re.search(r'\[SUGGESTIONS\](.*?)\[/SUGGESTIONS\]', reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            suggestions = data.get("items", [])
            reply = reply[:reply.index("[SUGGESTIONS]")].strip()
        except Exception:
            pass

    return {"reply": reply, "suggestions": suggestions}


_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>🐱 Kitty Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
header{background:#161b22;border-bottom:1px solid #30363d;padding:8px 16px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;gap:10px}
.logo{font-size:15px;font-weight:700;color:#f0f6fc;flex-shrink:0;display:flex;align-items:center;gap:8px}
.logo-img{width:22px;height:22px;border-radius:50%;object-fit:cover;background:#ffffff;flex-shrink:0;transition:background 0.4s}
.gnb{display:flex;align-items:center;gap:8px;flex:1;justify-content:flex-end}
.upd{font-size:11px;color:#8b949e;display:flex;align-items:center;gap:5px;flex-shrink:0}
.dot{width:7px;height:7px;border-radius:50%;background:#3fb950;animation:blink 2s infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
/* GNB 셀렉터 */
.gnb-select{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;outline:none}
.gnb-select:focus{border-color:#58a6ff}
.mode-paper{border-color:#3fb950!important;color:#3fb950!important}
.mode-live{border-color:#f85149!important;color:#f85149!important}
/* 탭 */
.tabs{display:flex;border-bottom:1px solid #30363d;background:#161b22;position:sticky;top:41px;z-index:99;overflow-x:auto}
.tab{padding:10px 14px;font-size:12px;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;flex-shrink:0}
.tab.active{color:#f0f6fc;border-bottom-color:#58a6ff}
.tab-content{display:none}.tab-content.active{display:block}
/* 공통 */
.wrap{padding:12px 14px;max-width:860px;margin:0 auto}
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:14px}
.sec-title{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;font-weight:600}
/* 카드 그리드 */
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.cards-2{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 8px;text-align:center}
.card .num{font-size:26px;font-weight:700;line-height:1}
.card .lbl{font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.card .sub{font-size:10px;color:#484f58;margin-top:2px}
.red{color:#f85149}.yellow{color:#d29922}.blue{color:#58a6ff}.green{color:#3fb950}.gray{color:#8b949e}
/* 상태 배지 */
.status-badge{display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:8px;font-weight:700;font-size:15px;margin-bottom:14px;width:100%}
.status-ok{background:#0d3321;color:#3fb950;border:1px solid #1a5c36}
.status-warning{background:#2d2500;color:#d29922;border:1px solid #5c4a00}
.status-critical{background:#2d1010;color:#f85149;border:1px solid #5c1010}
/* 바 차트 */
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:3px}
.bar-dt{width:42px;color:#8b949e;flex-shrink:0;text-align:right;font-size:10px}
.bar-track{flex:1;height:11px;background:#21262d;border-radius:3px;display:flex;overflow:hidden}
.bar-e{background:#f85149;height:100%}.bar-w{background:#d29922;height:100%}
.bar-in{background:#58a6ff;height:100%}.bar-out{background:#3fb950;height:100%}
.bar-n{width:34px;text-align:right;color:#8b949e;flex-shrink:0;font-size:10px}
/* 에이전트 성과 그리드 */
.agent-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:14px}
@media(min-width:600px){.agent-grid{grid-template-columns:repeat(3,1fr)}}
.agent-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px}
.agent-name{font-size:11px;color:#8b949e;margin-bottom:6px;font-weight:600}
.agent-score{font-size:28px;font-weight:700;line-height:1;margin-bottom:2px}
.agent-date{font-size:10px;color:#484f58}
.score-bars{margin-top:8px}
.s-bar-row{display:flex;align-items:center;gap:5px;margin-bottom:2px}
.s-bar-dt{font-size:9px;color:#484f58;width:36px;flex-shrink:0;text-align:right}
.s-bar-track{flex:1;height:8px;background:#21262d;border-radius:2px;overflow:hidden}
.s-bar-fill{height:100%;border-radius:2px;transition:width .3s}
.s-bar-n{font-size:9px;color:#8b949e;width:14px;flex-shrink:0;text-align:right}
/* 포트폴리오 테이블 */
.pf-wrap{overflow-x:auto;border:1px solid #30363d;border-radius:8px;margin-bottom:14px}
table.pf{width:100%;border-collapse:collapse;font-size:12px;min-width:320px}
table.pf th{background:#161b22;padding:7px 10px;text-align:right;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;border-bottom:1px solid #30363d}
table.pf th:first-child{text-align:left}
table.pf td{padding:7px 10px;border-bottom:1px solid #161b22;text-align:right;vertical-align:middle}
table.pf td:first-child{text-align:left}
table.pf tr:last-child td{border-bottom:none}
.pf-rate-cell{cursor:pointer;user-select:none}
.pf-rate-cell:hover{text-decoration:underline dotted;opacity:.85}
.pf-popup{position:fixed;z-index:400;background:#1c2128;border:1px solid #30363d;border-radius:8px;padding:12px 14px;min-width:210px;box-shadow:0 8px 24px rgba(0,0,0,.65);font-size:12px;display:none}
.pf-popup-title{font-size:13px;font-weight:700;color:#f0f6fc;margin-bottom:9px;padding-bottom:6px;border-bottom:1px solid #30363d}
.pf-popup-sym{color:#8b949e;font-size:11px;font-weight:400;margin-left:5px}
.pf-popup-row{display:flex;justify-content:space-between;gap:20px;padding:4px 0;border-bottom:1px solid #21262d}
.pf-popup-row:last-child{border-bottom:none}
.pf-popup-lbl{color:#8b949e}
.pf-popup-val{color:#c9d1d9;font-weight:600}
table.pf tr:hover td{background:#161b22}
.pf-name{font-weight:600;color:#f0f6fc;font-size:12px}
.pf-sym{font-size:10px;color:#8b949e}
/* 히트맵 */
.heatmap-wrap{overflow-x:auto;border-radius:8px;border:1px solid #30363d}
.heatmap{width:100%;border-collapse:collapse;font-size:12px}
.heatmap th{background:#161b22;padding:7px 10px;text-align:center;color:#8b949e;font-size:10px;font-weight:600;white-space:nowrap;border-bottom:1px solid #30363d}
.heatmap th:first-child{text-align:left}
.heatmap td{padding:6px 8px;text-align:center;border-bottom:1px solid #0d1117;font-size:12px;font-weight:700}
.heatmap td:first-child{text-align:left;font-size:11px;color:#8b949e;white-space:nowrap;font-weight:400;padding-left:10px}
.heatmap tr:last-child td{border-bottom:none}
.s-hi{background:#0d3321;color:#3fb950}.s-mid{background:#2d2500;color:#d29922}.s-lo{background:#2d1010;color:#f85149}.s-none{color:#484f58}
/* 에러 테이블 */
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.filters input,.filters select{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 10px;font-size:13px;flex:1;min-width:70px;outline:none}
.filters input:focus,.filters select:focus{border-color:#58a6ff}
.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer}
.btn:hover{background:#30363d}
.btn-pri{background:#238636;border-color:#2ea043;color:#fff}.btn-pri:hover{background:#2ea043}
.tbl-wrap{overflow-x:auto;border:1px solid #30363d;border-radius:8px}
table.log{width:100%;border-collapse:collapse;font-size:12px;min-width:460px}
table.log th{background:#161b22;padding:8px 10px;text-align:left;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;border-bottom:1px solid #30363d}
table.log td{padding:7px 10px;border-bottom:1px solid #161b22;vertical-align:top}
table.log tr:hover td{background:#161b22}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700}
.ERR-b{background:#3d1a1a;color:#f85149}.WARN-b{background:#3d2c00;color:#d29922}.CRIT-b{background:#3d1a1a;color:#ff7b72}
.ts-col{color:#8b949e;font-size:11px;white-space:nowrap}
.mod-col{color:#79c0ff;font-size:10px;white-space:nowrap;max-width:110px;overflow:hidden;text-overflow:ellipsis}
.msg-col{color:#c9d1d9;word-break:break-word;cursor:pointer}
.msg-col:hover{color:#f0f6fc}
.meta{font-size:11px;color:#8b949e;margin-bottom:6px}
.empty{text-align:center;color:#484f58;padding:32px;font-size:13px}
/* 토큰 에이전트 바 */
.tok-bar-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.tok-name{width:70px;font-size:11px;color:#8b949e;flex-shrink:0;text-overflow:ellipsis;overflow:hidden;white-space:nowrap}
.tok-track{flex:1;height:14px;background:#21262d;border-radius:3px;display:flex;overflow:hidden}
.tok-in{background:#58a6ff;height:100%}.tok-out{background:#3fb950;height:100%}
.tok-val{width:70px;text-align:right;font-size:10px;color:#8b949e;flex-shrink:0}
/* 모달 */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;align-items:center;justify-content:center;padding:16px}
.modal-bg.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;max-width:600px;width:100%;max-height:80vh;overflow-y:auto}
.modal h3{font-size:13px;margin-bottom:10px;color:#f0f6fc}
.modal pre{font-size:12px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;line-height:1.6}
.close-btn{float:right;background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;line-height:1}
/* 최근 에러 목록 */
.recent-err{font-size:11px;padding:6px 10px;border-bottom:1px solid #21262d;display:flex;gap:8px;align-items:flex-start}
.recent-err:last-child{border-bottom:none}
.recent-err .ts{color:#484f58;flex-shrink:0;white-space:nowrap}
.recent-err .msg{color:#c9d1d9;word-break:break-word}
/* 투자 성향 카드 */
.tendency-card{padding:12px 14px;background:#0d1117;border:1px solid #30363d;border-radius:8px;margin-bottom:14px}
.tendency-header{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.tendency-badge{flex-shrink:0;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.3px}
.t-aggressive{background:#2d1b00;color:#f0883e;border:1px solid #5c3a00}
.t-balanced{background:#0d2d3d;color:#58a6ff;border:1px solid #1a4a6e}
.t-conservative{background:#1a2a1a;color:#3fb950;border:1px solid #2a4a2a}
.tendency-rationale{font-size:11px;color:#6e7681;flex:1;min-width:0}
/* 종합 평가 리포트 (접기/펼치기) */
.td-report{margin-top:10px;padding-top:8px;border-top:1px solid #21262d}
.td-report-title{font-size:10px;color:#8b949e;font-weight:600;margin-bottom:5px;letter-spacing:.3px}
.td-report-text{font-size:11px;color:#8b949e;line-height:1.6;word-break:break-word}
.td-report-more{background:none;border:none;color:#58a6ff;font-size:10px;cursor:pointer;padding:0 0 0 4px;vertical-align:baseline;text-decoration:underline}
.tendency-dims{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
.td-dim{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:6px 8px;min-width:0}
.td-dim-name{display:block;font-size:9px;color:#484f58;letter-spacing:.4px;text-transform:uppercase;margin-bottom:3px}
.td-dim-lv{display:inline-block;font-size:9px;font-weight:700;padding:1px 5px;border-radius:10px;margin-bottom:3px}
.lv-1{background:#3d1a00;color:#ff8c00}.lv-2{background:#2d1b00;color:#f0883e}
.lv-3{background:#1a2a3d;color:#79c0ff}.lv-4{background:#0d2d3d;color:#58a6ff}
.lv-5{background:#1a2a1a;color:#3fb950}.lv-6{background:#0d1f0d;color:#2ea043}
.td-dim-val{display:block;font-size:10px;font-weight:600;color:#c9d1d9}
.td-dim-sub{display:block;font-size:9px;color:#484f58;margin-top:1px}
/* 포트폴리오 요약 카드 — 라벨 상단 좌측, 금액 폰트 축소 */
#pf-summary-cards .card{text-align:left;padding:10px 12px}
#pf-summary-cards .card .lbl{margin-top:0;margin-bottom:5px;letter-spacing:0;text-transform:none}
#pf-summary-cards .card .num{font-size:17px}
/* 서브탭 (관리 영역) */
.subtabs{display:flex;background:#0d1117;border-bottom:1px solid #21262d;position:sticky;top:83px;z-index:98;overflow-x:auto}
.subtab{padding:7px 16px;font-size:11px;color:#484f58;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;flex-shrink:0;letter-spacing:.3px}
.subtab.active{color:#c9d1d9;border-bottom-color:#58a6ff}
/* GNB view switcher */
.view-switch{display:flex;background:#21262d;border-radius:6px;overflow:hidden;border:1px solid #30363d}
.view-btn{padding:4px 10px;font-size:11px;cursor:pointer;border:none;background:transparent;color:#8b949e;font-weight:600;transition:all .15s}
.view-btn.active{background:#58a6ff;color:#fff}
.view-btn:hover:not(.active){background:#30363d;color:#c9d1d9}
/* FAB 채팅 버튼 */
.fab{position:fixed;bottom:22px;right:18px;z-index:150;width:52px;height:52px;border-radius:50%;background:#238636;border:none;color:#fff;font-size:22px;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;transition:transform .15s,background .15s}
.fab:hover{transform:scale(1.08);background:#2ea043}
body{padding-bottom:80px}
/* 채팅 팝업 */
.chat-popup{position:fixed;inset:0;z-index:190;display:flex;flex-direction:column;justify-content:flex-end;pointer-events:none}
.chat-popup.open{pointer-events:auto}
.chat-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.6);opacity:0;transition:opacity .25s;cursor:pointer}
.chat-popup.open .chat-backdrop{opacity:1}
.chat-panel{position:relative;background:#161b22;border-top:1px solid #30363d;border-radius:16px 16px 0 0;display:flex;flex-direction:column;max-height:78vh;transform:translateY(100%);transition:transform .28s cubic-bezier(.4,0,.2,1)}
.chat-popup.open .chat-panel{transform:translateY(0)}
.chat-drag{width:40px;height:4px;background:#30363d;border-radius:2px;margin:10px auto 0;flex-shrink:0}
.chat-panel-head{display:flex;align-items:center;gap:8px;padding:10px 14px 10px;border-bottom:1px solid #21262d;flex-shrink:0}
.chat-panel-title{font-size:13px;font-weight:600;color:#f0f6fc;flex:1}
.chat-close{background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;padding:2px 4px;line-height:1}
.chat-agent-sel{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:5px 8px;font-size:12px;outline:none;cursor:pointer}
.chat-agent-sel:focus{border-color:#58a6ff}
.chat-history{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px;min-height:160px}
.chat-msg{display:flex;flex-direction:column;gap:3px;max-width:92%}
.chat-msg.user{align-self:flex-end;align-items:flex-end}
.chat-msg.assistant{align-self:flex-start;align-items:flex-start}
.chat-bubble{padding:8px 12px;border-radius:10px;font-size:13px;line-height:1.55;word-break:break-word;white-space:pre-wrap}
.chat-msg.user .chat-bubble{background:#1c4a7a;color:#cae0f9;border-bottom-right-radius:3px}
.chat-msg.assistant .chat-bubble{background:#21262d;color:#c9d1d9;border-bottom-left-radius:3px}
.chat-meta{font-size:10px;color:#484f58;padding:0 2px}
.chat-thinking{color:#484f58;font-size:12px;font-style:italic;padding:4px 2px;animation:blink 1.4s infinite}
.chat-input-row{display:flex;gap:8px;align-items:flex-end;padding:10px 14px 18px;border-top:1px solid #21262d;flex-shrink:0}
.chat-input{flex:1;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:8px 10px;font-size:13px;resize:none;min-height:40px;max-height:100px;outline:none;font-family:inherit;line-height:1.5}
.chat-input:focus{border-color:#58a6ff}
/* 성향관리 탭 */
.adv-agent-block{margin-bottom:10px;background:#161b22;border-radius:8px;padding:10px 12px}
.adv-agent-name{font-size:11px;font-weight:700;color:#58a6ff;margin-bottom:6px;letter-spacing:.3px}
.adv-item{padding:5px 0;border-top:1px solid #21262d;margin-top:4px;display:flex;gap:8px;align-items:flex-start}
.adv-item-date{font-size:10px;color:#484f58;flex-shrink:0;padding-top:1px}
.adv-item-text{font-size:12px;color:#c9d1d9;flex:1;line-height:1.5}
.adv-sugg-item{background:#1a1200;border:1px solid #d29922;border-radius:8px;padding:10px 12px;margin-bottom:8px}
.adv-sugg-agent{font-size:10px;color:#d29922;font-weight:700;margin-bottom:4px;letter-spacing:.3px}
.adv-sugg-text{font-size:12px;color:#c9d1d9;line-height:1.5}
#adv-chat-box{height:220px;overflow-y:auto;background:#0d1117;border-radius:8px;padding:10px;border:1px solid #21262d;margin-bottom:8px;display:flex;flex-direction:column;gap:8px}
.adv-msg-user{align-self:flex-end;background:#1c4a7a;color:#cae0f9;padding:7px 11px;border-radius:10px 10px 3px 10px;font-size:12px;max-width:88%;word-break:break-word;white-space:pre-wrap;line-height:1.5}
.adv-msg-ai{align-self:flex-start;background:#21262d;color:#c9d1d9;padding:7px 11px;border-radius:10px 10px 10px 3px;font-size:12px;max-width:92%;word-break:break-word;white-space:pre-wrap;line-height:1.5}
.adv-thinking{color:#484f58;font-size:11px;font-style:italic;animation:blink 1.4s infinite;padding:4px 2px;align-self:flex-start}
#adv-prompt-box{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;margin-top:8px;max-height:280px;overflow-y:auto}
#adv-prompt-text{font-size:10px;color:#8b949e;white-space:pre-wrap;word-break:break-word;line-height:1.5}
#adv-prompt-feedback{font-size:10px;color:#3fb950;white-space:pre-wrap;word-break:break-word;line-height:1.5;border-top:1px solid #21262d;margin-top:8px;padding-top:8px}
/* 페이지네이션 */
.pg-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;min-width:32px}
.pg-btn:hover:not(:disabled){background:#30363d}.pg-btn:disabled{opacity:.35;cursor:default}
.pg-cur{background:#1c4a7a!important;border-color:#58a6ff!important;color:#cae0f9!important;font-weight:700}
/* 매매일지 분류 배지 */
.trade-cls{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap}
/* 프롬프트 버튼 */
.btn-prompt{background:transparent;border:1px solid #30363d;color:#58a6ff;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin-top:6px;width:100%}
.btn-prompt:hover{background:#1c4a7a;border-color:#58a6ff}
.btn-detail{background:transparent;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:2px 7px;font-size:11px;cursor:pointer;white-space:nowrap}
.btn-detail:hover{border-color:#58a6ff;color:#58a6ff}
.cls-익절{background:#3d1010;color:#f85149}.cls-손절{background:#0d1a3d;color:#4493f8}
.cls-신규매수{background:#0d2d3d;color:#58a6ff}.cls-추가매수{background:#142814;color:#57ab5a}
.cls-종목교체{background:#2d2500;color:#d29922}.cls-매도{background:#21262d;color:#8b949e}
/* 매매일지 가로바 */
.tr-bar-wrap{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin-bottom:12px}
.tr-bar-total{font-size:12px;color:#8b949e;margin-bottom:8px}
.tr-bar-total strong{color:#c9d1d9;font-size:13px}
.tr-bar-row{margin-bottom:6px}
.tr-bar-row-lbl{font-size:10px;color:#484f58;margin-bottom:3px}
.tr-bar-track{display:flex;height:14px;border-radius:4px;overflow:hidden;background:#21262d;gap:1px}
.tr-bar-seg{height:100%;transition:width .3s;min-width:0}
.tr-bar-labels{display:flex;gap:14px;margin-top:6px;flex-wrap:wrap}
.tr-bar-lbl{display:flex;align-items:center;gap:5px;font-size:11px;color:#8b949e}
.tr-bar-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.tr-bar-cnt{font-weight:700;color:#c9d1d9;margin-left:1px}
/* 매매일지 컬럼 너비 */
table.log th:nth-child(3),table.log td:nth-child(3){width:72px}
table.log th:nth-child(4),table.log td:nth-child(4){width:52px;text-align:center}
</style>
</head>
<body>
<header>
  <div class="logo">
    <img class="logo-img" id="logo-img" src="__KITTY_LOGO__" alt="kitty">
    <span id="logo-text">🐱 Kitty Monitor</span>
  </div>
  <div class="gnb">
    <div class="view-switch">
      <button class="view-btn active" id="view-kitty" onclick="switchView('kitty')">🐱 Kitty</button>
      <button class="view-btn" id="view-night" onclick="switchView('night')">🌙 Night</button>
    </div>
    <select id="gnb-mode" class="gnb-select" onchange="onModeChange(this.value)" title="매매 모드">
      <option value="paper">📄 paper</option>
      <option value="live">🔴 live</option>
    </select>
  </div>
  <div class="upd"><span class="dot"></span><span id="upd-txt">연결 중...</span></div>
</header>

<div class="tabs">
  <div class="tab active" id="main-tab-agents" onclick="switchMain('agents')">🤖 성적표</div>
  <div class="tab" id="main-tab-trades" onclick="switchMain('trades')">📒 매매일지</div>
  <div class="tab" id="main-tab-admin" onclick="switchMain('admin')">⚙️ 관리</div>
</div>
<div class="subtabs" id="subtabs" style="display:none">
  <div class="subtab active" id="sub-tab-errors" onclick="switchAdmin('errors')">📋 에러</div>
  <div class="subtab" id="sub-tab-tokens" onclick="switchAdmin('tokens')">🔢 토큰</div>
  <div class="subtab" id="sub-tab-advisor" onclick="switchAdmin('advisor')">🧠 성향관리</div>
</div>


<!-- ══ 에러 로그 탭 ══ -->
<div id="tab-errors" class="tab-content">
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="num red"    id="c-err">-</div><div class="lbl">오늘 에러</div></div>
    <div class="card"><div class="num yellow" id="c-warn">-</div><div class="lbl">오늘 경고</div></div>
    <div class="card"><div class="num blue"   id="c-total">-</div><div class="lbl">전체</div></div>
  </div>
  <div class="section">
    <div class="sec-title">14일 에러 추이</div>
    <div id="err-chart"></div>
  </div>
  <div class="filters">
    <input type="date" id="f-date">
    <select id="f-level">
      <option value="">전체</option>
      <option value="ERROR">ERROR</option>
      <option value="WARNING">WARNING</option>
      <option value="CRITICAL">CRITICAL</option>
    </select>
    <input type="text" id="f-q" placeholder="메시지 검색">
    <button class="btn btn-pri" onclick="loadErrors()">조회</button>
    <button class="btn" onclick="clearFilter()">초기화</button>
  </div>
  <div class="meta" id="err-meta"></div>
  <div class="tbl-wrap">
    <table class="log">
      <thead><tr><th>시각</th><th>레벨</th><th>모듈</th><th>메시지 (클릭=전체)</th></tr></thead>
      <tbody id="err-tbody"></tbody>
    </table>
  </div>
</div>
</div>

<!-- ══ 에이전트 성적표 (메인) ══ -->
<div id="tab-agents" class="tab-content active">
<div id="agents-kitty" class="wrap">
  <!-- 투자 성향 -->
  <div id="tendency-card" class="tendency-card" style="display:none">
    <div class="tendency-header">
      <span id="td-badge" class="tendency-badge">-</span>
    </div>
    <div class="tendency-dims" id="td-dims">
      <div class="td-dim"><span class="td-dim-name">익절</span><span id="td-tp-lv" class="td-dim-lv lv-2">L2</span><span id="td-tp" class="td-dim-val">-</span><span id="td-tp-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">손절</span><span id="td-sl-lv" class="td-dim-lv lv-2">L2</span><span id="td-sl" class="td-dim-val">-</span><span id="td-sl-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">현금</span><span id="td-cash-lv" class="td-dim-lv lv-2">L2</span><span id="td-cash" class="td-dim-val">-</span><span id="td-cash-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">집중도</span><span id="td-wt-lv" class="td-dim-lv lv-2">L2</span><span id="td-wt" class="td-dim-val">-</span><span id="td-wt-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">진입기준</span><span id="td-en-lv" class="td-dim-lv lv-2">L2</span><span id="td-en" class="td-dim-val">-</span><span id="td-en-sub" class="td-dim-sub">-</span></div>
    </div>
    <!-- 종합 평가 리포트 -->
    <div class="td-report" id="td-report" style="display:none">
      <div class="td-report-title" id="td-report-title"></div>
      <div class="td-report-text">
        <span id="td-report-preview"></span><span id="td-report-full" style="display:none"></span><button class="td-report-more" id="td-report-more" onclick="toggleReport()">more</button>
      </div>
    </div>
  </div>
  <!-- 포트폴리오 현황 -->
  <div class="section">
    <div class="sec-title">현재 포트폴리오</div>

    <div class="cards" id="pf-summary-cards" style="margin-bottom:10px">
      <div class="card"><div class="lbl">총평가금액(원)</div><div class="num blue"  id="pf-total-eval">-</div></div>
      <div class="card"><div class="lbl">평가손익(원)</div><div class="num"       id="pf-total-pnl">-</div></div>
      <div class="card"><div class="lbl">주문가능현금(원)</div><div class="num gray"  id="pf-cash">-</div></div>
    </div>
    <div class="pf-wrap">
      <table class="pf">
        <thead><tr>
          <th>종목</th><th>수량</th><th>평균단가</th><th>현재가</th><th>수익률 ⓘ</th>
        </tr></thead>
        <tbody id="pf-tbody"><tr><td colspan="5" class="empty">로딩 중...</td></tr></tbody>
      </table>
    </div>
    <div style="font-size:10px;color:#484f58;margin-top:6px;text-align:right" id="pf-ts"></div>
  </div>
  <div class="agent-grid" id="agent-cards"></div>
  <div class="section">
    <div class="sec-title">일별 점수 히트맵</div>
    <div class="heatmap-wrap">
      <table class="heatmap" id="heatmap"></table>
    </div>
  </div>
</div>
<div id="agents-night" class="wrap" style="display:none">
  <!-- Night 투자 성향 -->
  <div id="night-tendency-card" class="tendency-card" style="display:none">
    <div class="tendency-header">
      <span id="nt-badge" class="tendency-badge">-</span>
    </div>
    <div class="tendency-dims" id="nt-dims">
      <div class="td-dim"><span class="td-dim-name">Take Profit</span><span id="nt-tp-lv" class="td-dim-lv lv-2">L2</span><span id="nt-tp" class="td-dim-val">-</span><span id="nt-tp-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">Stop Loss</span><span id="nt-sl-lv" class="td-dim-lv lv-2">L2</span><span id="nt-sl" class="td-dim-val">-</span><span id="nt-sl-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">Cash</span><span id="nt-cash-lv" class="td-dim-lv lv-2">L2</span><span id="nt-cash" class="td-dim-val">-</span><span id="nt-cash-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">Weight</span><span id="nt-wt-lv" class="td-dim-lv lv-2">L2</span><span id="nt-wt" class="td-dim-val">-</span><span id="nt-wt-sub" class="td-dim-sub">-</span></div>
      <div class="td-dim"><span class="td-dim-name">Entry</span><span id="nt-en-lv" class="td-dim-lv lv-2">L2</span><span id="nt-en" class="td-dim-val">-</span><span id="nt-en-sub" class="td-dim-sub">-</span></div>
    </div>
    <div class="td-report" id="nt-report" style="display:none">
      <div class="td-report-title" id="nt-report-title"></div>
      <div class="td-report-text">
        <span id="nt-report-preview"></span><span id="nt-report-full" style="display:none"></span><button class="td-report-more" id="nt-report-more" onclick="toggleNightReport()">more</button>
      </div>
    </div>
  </div>
  <!-- Night 포트폴리오 -->
  <div class="section">
    <div class="sec-title">Night Portfolio (USD)</div>
    <div class="cards" id="nt-summary-cards" style="margin-bottom:10px">
      <div class="card" style="text-align:left;padding:10px 12px"><div class="lbl" style="margin-top:0;margin-bottom:5px">Total Value</div><div class="num blue" style="font-size:17px" id="nt-total-eval">-</div></div>
      <div class="card" style="text-align:left;padding:10px 12px"><div class="lbl" style="margin-top:0;margin-bottom:5px">P&L</div><div class="num" style="font-size:17px" id="nt-total-pnl">-</div></div>
      <div class="card" style="text-align:left;padding:10px 12px"><div class="lbl" style="margin-top:0;margin-bottom:5px">Cash</div><div class="num gray" style="font-size:17px" id="nt-cash-val">-</div></div>
    </div>
    <div class="pf-wrap">
      <table class="pf">
        <thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>Price</th><th>P&L% ⓘ</th></tr></thead>
        <tbody id="nt-pf-tbody"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
    <div style="font-size:10px;color:#484f58;margin-top:6px;text-align:right" id="nt-pf-ts"></div>
  </div>
  <div class="agent-grid" id="nt-agent-cards"></div>
  <div class="section">
    <div class="sec-title">Night Agent Score Heatmap</div>
    <div class="heatmap-wrap"><table class="heatmap" id="nt-heatmap"></table></div>
  </div>
</div>
</div>

<!-- ══ 토큰 사용량 탭 ══ -->
<div id="tab-tokens" class="tab-content">
<div class="wrap">
  <div class="cards-2">
    <div class="card"><div class="num blue"  id="tk-in-today">-</div><div class="lbl">오늘 입력 토큰</div></div>
    <div class="card"><div class="num green" id="tk-out-today">-</div><div class="lbl">오늘 출력 토큰</div></div>
    <div class="card"><div class="num yellow" id="tk-cost-today">-</div><div class="lbl">오늘 비용 (USD)</div></div>
    <div class="card"><div class="num gray"  id="tk-cost-14d">-</div><div class="lbl">14일 비용 (USD)</div></div>
  </div>
  <div class="section">
    <div class="sec-title">에이전트별 총 토큰 사용량</div>
    <div id="tok-agent-bars"></div>
  </div>
  <div class="section">
    <div class="sec-title">14일 일별 토큰 추이</div>
    <div id="tok-daily-chart"></div>
    <div style="display:flex;gap:14px;margin-top:8px;font-size:10px;color:#8b949e">
      <span><span style="display:inline-block;width:10px;height:10px;background:#58a6ff;border-radius:2px;margin-right:4px"></span>입력</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:#3fb950;border-radius:2px;margin-right:4px"></span>출력</span>
    </div>
  </div>
</div>
</div>

<!-- ══ 성향관리 탭 ══ -->
<div id="tab-advisor" class="tab-content">
<div class="wrap">

  <!-- 에이전트 프롬프트 확인 -->
  <div class="section">
    <div class="sec-title">에이전트 프롬프트</div>
    <select id="adv-prompt-sel" class="gnb-select" style="width:100%;margin-bottom:4px" onchange="showAdvPrompt()">
      <option value="">에이전트 선택...</option>
      <optgroup label="KR 주식">
        <option>섹터분석가</option><option>종목발굴가</option><option>종목평가가</option>
        <option>자산운용가</option><option>매수실행가</option><option>매도실행가</option>
      </optgroup>
      <optgroup label="Night (US)">
        <option>NightSectorAnalyst</option><option>NightStockPicker</option>
        <option>NightStockEvaluator</option><option>NightAssetManager</option>
        <option>NightBuyExecutor</option><option>NightSellExecutor</option>
      </optgroup>
    </select>
    <div id="adv-prompt-box" style="display:none">
      <pre id="adv-prompt-text"></pre>
      <pre id="adv-prompt-feedback" style="display:none"></pre>
    </div>
  </div>

  <!-- 개선 피드백 리스트 -->
  <div class="section">
    <div class="sec-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>개선 피드백 리스트</span>
      <select id="adv-agent-filter" class="gnb-select" style="font-size:10px" onchange="renderAdvImprovements()">
        <option value="">전체</option>
        <optgroup label="KR">
          <option>섹터분석가</option><option>종목발굴가</option><option>종목평가가</option>
          <option>자산운용가</option><option>매수실행가</option><option>매도실행가</option>
        </optgroup>
        <optgroup label="Night">
          <option>NightSectorAnalyst</option><option>NightStockPicker</option>
          <option>NightStockEvaluator</option><option>NightAssetManager</option>
          <option>NightBuyExecutor</option><option>NightSellExecutor</option>
        </optgroup>
      </select>
    </div>
    <div id="adv-improvements" style="margin-top:8px"><div class="empty">로딩 중...</div></div>
  </div>

  <!-- AI 성향관리자 대화 -->
  <div class="section">
    <div class="sec-title">성향관리자 AI 대화</div>
    <div id="adv-chat-box">
      <div id="adv-chat-placeholder" class="adv-thinking" style="animation:none;color:#484f58;font-style:normal;font-size:12px;padding:8px">
        에이전트 성과와 개선 방향에 대해 대화하세요.<br>
        <span style="font-size:11px;color:#30363d">예: "어떤 에이전트 개선이 시급한가요?" · "종목평가가 판단 기준을 어떻게 강화할까요?"</span>
      </div>
    </div>
    <!-- 제안된 개선사항 -->
    <div id="adv-sugg-section" style="display:none;margin-bottom:8px">
      <div class="sec-title" style="color:#d29922;font-size:11px;margin-bottom:6px">💡 제안된 개선사항 (저장하면 다음 사이클부터 반영)</div>
      <div id="adv-sugg-list"></div>
    </div>
    <div style="display:flex;gap:8px;align-items:flex-end">
      <textarea id="adv-chat-input" class="chat-input" rows="2"
        placeholder="개선 방향 질문 (Enter: 전송 / Shift+Enter: 줄바꿈)"
        onkeydown="onAdvKey(event)" oninput="autoResize(this)"></textarea>
      <button class="btn btn-pri" id="adv-send-btn" onclick="sendAdvChat()" style="flex-shrink:0">전송</button>
    </div>
    <button class="btn" onclick="clearAdvChat()" style="margin-top:6px;font-size:11px;width:100%">대화 초기화</button>
  </div>

</div>
</div>

<!-- ══ 매매일지 탭 ══ -->
<div id="tab-trades" class="tab-content">
<div class="wrap">
  <div class="tr-bar-wrap">
    <div class="tr-bar-total">전체 거래 <strong id="tr-total-cnt">-</strong>건</div>
    <div class="tr-bar-row">
      <div class="tr-bar-row-lbl">매수 / 매도</div>
      <div class="tr-bar-track">
        <div class="tr-bar-seg" id="tr-bar-buy"  style="background:#1c4a7a;width:0%"></div>
        <div class="tr-bar-seg" id="tr-bar-sell" style="background:#2d2500;width:0%"></div>
      </div>
      <div class="tr-bar-labels">
        <div class="tr-bar-lbl"><div class="tr-bar-dot" style="background:#58a6ff"></div>매수 <span class="tr-bar-cnt" id="tr-buy-cnt">-</span></div>
        <div class="tr-bar-lbl"><div class="tr-bar-dot" style="background:#d29922"></div>매도 <span class="tr-bar-cnt" id="tr-sell-cnt">-</span></div>
      </div>
    </div>
    <div class="tr-bar-row" style="margin-top:10px">
      <div class="tr-bar-row-lbl">익절 / 손절 / 기타</div>
      <div class="tr-bar-track">
        <div class="tr-bar-seg" id="tr-bar-profit" style="background:#1a4a1a;width:0%"></div>
        <div class="tr-bar-seg" id="tr-bar-loss"   style="background:#4a1010;width:0%"></div>
        <div class="tr-bar-seg" id="tr-bar-other"  style="background:#2d2f33;width:0%"></div>
      </div>
      <div class="tr-bar-labels">
        <div class="tr-bar-lbl"><div class="tr-bar-dot" style="background:#3fb950"></div>익절 <span class="tr-bar-cnt" id="tr-profit-cnt">-</span></div>
        <div class="tr-bar-lbl"><div class="tr-bar-dot" style="background:#f85149"></div>손절 <span class="tr-bar-cnt" id="tr-loss-cnt">-</span></div>
        <div class="tr-bar-lbl"><div class="tr-bar-dot" style="background:#484f58"></div>기타 <span class="tr-bar-cnt" id="tr-other-cnt">-</span></div>
      </div>
    </div>
  </div>
  <div class="filters">
    <input type="date" id="tr-date">
    <select id="tr-cls">
      <option value="">전체 분류</option>
      <option value="신규매수">신규매수</option>
      <option value="추가매수">추가매수</option>
      <option value="익절">익절</option>
      <option value="손절">손절</option>
      <option value="종목교체">종목교체</option>
      <option value="매도">매도</option>
    </select>
    <select id="tr-src">
      <option value="">전체 (국내+해외)</option>
      <option value="kitty">🐱 Kitty (국내)</option>
      <option value="night">🌙 Night (해외)</option>
    </select>
    <button class="btn btn-pri" onclick="loadTrades()">조회</button>
  </div>
  <div class="meta" id="tr-meta"></div>
  <div class="tbl-wrap">
    <table class="log" style="min-width:400px">
      <thead><tr><th>날짜/시각</th><th>종목</th><th>분류</th><th>상세</th></tr></thead>
      <tbody id="tr-tbody"></tbody>
    </table>
  </div>
  <div id="tr-pagination" style="display:flex;justify-content:center;gap:4px;margin-top:10px;flex-wrap:wrap"></div>
</div>
</div>

<!-- FAB 채팅 버튼 -->
<button class="fab" id="fab-chat" onclick="openChat()" title="에이전트 채팅">💬</button>

<!-- 채팅 팝업 -->
<div class="chat-popup" id="chat-popup">
  <div class="chat-backdrop" onclick="closeChat()"></div>
  <div class="chat-panel">
    <div class="chat-drag"></div>
    <div class="chat-panel-head">
      <span class="chat-panel-title">에이전트 채팅</span>
      <select id="chat-agent" class="chat-agent-sel">
        <option value="섹터분석가">섹터분석가</option>
        <option value="종목발굴가">종목발굴가</option>
        <option value="종목평가가">종목평가가</option>
        <option value="자산운용가">자산운용가</option>
        <option value="매수실행가">매수실행가</option>
        <option value="매도실행가">매도실행가</option>
        <option value="투자성향관리자">투자성향관리자</option>
      </select>
      <button class="chat-close" onclick="closeChat()">✕</button>
    </div>
    <div class="chat-history" id="chat-history">
      <div id="chat-placeholder" style="text-align:center;color:#484f58;font-size:12px;padding:24px 16px">
        에이전트를 선택하고 질문해보세요.<br>
        <span style="color:#30363d;font-size:11px;margin-top:6px;display:block">예: "왜 그런 판단을 했나요?" · "분석 근거 설명해줘"</span>
      </div>
    </div>
    <div class="chat-input-row">
      <textarea id="chat-input" class="chat-input" rows="1"
        placeholder="질문 입력  (Enter: 전송 / Shift+Enter: 줄바꿈)"
        onkeydown="onChatKey(event)" oninput="autoResize(this)"></textarea>
      <button class="btn btn-pri" id="chat-send-btn" onclick="sendChat()">전송</button>
    </div>
  </div>
</div>

<!-- 모달 -->
<div id="pf-popup" class="pf-popup" onclick="event.stopPropagation()"></div>
<div class="modal-bg" id="modal" onclick="closeModal(event)">
  <div class="modal">
    <button class="close-btn" onclick="document.getElementById('modal').classList.remove('show')">✕</button>
    <h3 id="modal-title"></h3>
    <div id="modal-body" style="font-size:12px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;line-height:1.6"></div>
  </div>
</div>

<script>
// ── View 전환 (kitty ↔ night) ────────────────────────────
let _currentView = 'kitty';

function switchView(view) {
  _currentView = view;
  document.getElementById('view-kitty').classList.toggle('active', view==='kitty');
  document.getElementById('view-night').classList.toggle('active', view==='night');
  document.getElementById('logo-text').textContent = view==='kitty' ? '🐱 Kitty Monitor' : '🌙 Night Monitor';
  document.getElementById('logo-img').style.background = view==='night' ? '#000000' : '#ffffff';
  // 현재 활성 탭을 그대로 유지하되 내용을 새 view에 맞게 리로드
  const activeMain = ['agents','trades','admin'].find(t =>
    document.getElementById('main-tab-'+t)?.classList.contains('active')
  ) || 'agents';
  switchMain(activeMain);
}

// ── 탭 전환 ─────────────────────────────────────────────
let _adminTab = 'errors';
let _advFeedback = {};
let _advPrompts = {};
let _advHistory = [];
let _advSuggestions = [];

function switchMain(name) {
  ['agents','trades','admin'].forEach(t => {
    const el = document.getElementById('main-tab-'+t);
    if(el) el.classList.toggle('active', t===name);
  });
  document.getElementById('subtabs').style.display = name==='admin' ? 'flex' : 'none';
  ['errors','tokens','advisor','agents','trades'].forEach(n => {
    document.getElementById('tab-'+n).classList.remove('active');
  });
  if(name === 'agents') {
    document.getElementById('tab-agents').classList.add('active');
    if(_currentView === 'night') {
      document.getElementById('agents-kitty').style.display = 'none';
      document.getElementById('agents-night').style.display = '';
      loadNightTendency(); loadNightPortfolio(); loadNightAgentScores();
    } else {
      document.getElementById('agents-kitty').style.display = '';
      document.getElementById('agents-night').style.display = 'none';
      loadTendency(); loadPortfolio(); loadAgentScores();
    }
  } else if(name === 'trades') {
    document.getElementById('tab-trades').classList.add('active');
    // view에 따라 소스 자동 필터 (수동으로 바꿀 수도 있음)
    document.getElementById('tr-src').value = _currentView === 'night' ? 'night' : 'kitty';
    loadTrades();
  } else {
    switchAdmin(_adminTab);
  }
}

function switchAdmin(name) {
  _adminTab = name;
  ['errors','tokens','advisor'].forEach(n => {
    document.getElementById('sub-tab-'+n).classList.toggle('active', n===name);
    document.getElementById('tab-'+n).classList.toggle('active', n===name);
  });
  document.getElementById('tab-agents').classList.remove('active');
  if(name==='errors'){ loadStats(); loadErrors(); }
  if(name==='tokens'){
    if(_currentView === 'night') loadNightTokens();
    else loadTokens();
  }
  if(name==='advisor'){ loadAdvisor(); }
}

// ── 채팅 팝업 ────────────────────────────────────────────
function openChat() {
  document.getElementById('chat-popup').classList.add('open');
  document.getElementById('chat-input').focus();
}
function closeChat() {
  document.getElementById('chat-popup').classList.remove('open');
}

// KST 유틸리티 (Asia/Seoul, UTC+9, DST 없음)
const _toKST = d => new Date(d).toLocaleString('sv-SE', {timeZone:'Asia/Seoul'});   // "YYYY-MM-DD HH:MM:SS"
const _kstDate = () => _toKST(new Date()).slice(0,10);
const _kstTime = () => new Date().toLocaleTimeString('ko-KR', {hour:'2-digit', minute:'2-digit', timeZone:'Asia/Seoul'});
const today = _kstDate();
document.getElementById('f-date').value = today;

const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const badge = lvl => {
  const cls = lvl==='ERROR'?'ERR-b':lvl==='WARNING'?'WARN-b':'CRIT-b';
  return `<span class="badge ${cls}">${lvl}</span>`;
};
const scoreColor = s => s>=70?'#3fb950':s>=40?'#d29922':'#f85149';
const scoreBg    = s => s>=70?'s-hi':s>=40?'s-mid':'s-lo';
const fmtNum = n => n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n);

// ── 상태 탭 ─────────────────────────────────────────────
async function loadHealth() {
  try {
    const d = await fetch('/api/health').then(r=>r.json());
    document.getElementById('h-err-today').textContent  = d.err_today;
    document.getElementById('h-warn-today').textContent = d.warn_today;
    document.getElementById('h-err-1h').textContent     = d.err_1h;
    document.getElementById('h-last-log').textContent   = d.last_log_ts || '로그 없음';
    if(d.last_log_ts) document.getElementById('upd-txt').textContent = '갱신 '+d.last_log_ts.slice(5,16)+' KST';

    const badge = document.getElementById('status-badge');
    const labels = {ok:'✅ 정상 운영 중', warning:'⚠️ 경고 — 에러 증가 중', critical:'🔴 위험 — 에러 다수 발생'};
    badge.className = 'status-badge status-'+d.status;
    badge.textContent = labels[d.status] || d.status;

    const el = document.getElementById('h-recent-errors');
    if(!d.recent || !d.recent.length){
      el.innerHTML = '<div class="empty">없음 ✅</div>';
    } else {
      el.innerHTML = d.recent.map(e=>`
        <div class="recent-err">
          <span class="ts">${e.ts.slice(5,16)}</span>
          ${badge_(e.level)}
          <span class="msg">${esc(e.message.slice(0,100))}</span>
        </div>`).join('');
    }
  } catch(ex){ console.error('health',ex); }
}
function badge_(lvl){
  const cls=lvl==='ERROR'?'ERR-b':lvl==='WARNING'?'WARN-b':'CRIT-b';
  return `<span class="badge ${cls}" style="flex-shrink:0">${lvl}</span>`;
}

// ── 에러 탭 ─────────────────────────────────────────────
async function loadStats() {
  try {
    const d = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('c-err').textContent   = (d.today['ERROR']||0)+(d.today['CRITICAL']||0);
    document.getElementById('c-warn').textContent  = d.today['WARNING']||0;
    const tot = Object.values(d.totals).reduce((a,v)=>a+v,0);
    document.getElementById('c-total').textContent = tot;
    if(d.latest) document.getElementById('upd-txt').textContent = '갱신 '+d.latest.slice(5,16)+' KST';

    const dates  = [...new Set(d.daily.map(x=>x.date))].sort();
    const maxN   = Math.max(1,...dates.map(dt=>d.daily.filter(x=>x.date===dt).reduce((a,x)=>a+x.cnt,0)));
    document.getElementById('err-chart').innerHTML = dates.map(dt=>{
      const rows = d.daily.filter(x=>x.date===dt);
      const err  = rows.filter(x=>x.level!=='WARNING').reduce((a,x)=>a+x.cnt,0);
      const warn = rows.filter(x=>x.level==='WARNING').reduce((a,x)=>a+x.cnt,0);
      return `<div class="bar-row">
        <div class="bar-dt">${dt.slice(5)}</div>
        <div class="bar-track">
          <div class="bar-e" style="width:${err/maxN*100}%"></div>
          <div class="bar-w" style="width:${warn/maxN*100}%"></div>
        </div>
        <div class="bar-n">${err+warn}</div>
      </div>`;
    }).join('');
  } catch(e){ console.error(e); }
}

async function loadErrors() {
  const date=document.getElementById('f-date').value,
        level=document.getElementById('f-level').value,
        q=document.getElementById('f-q').value.trim();
  const p=new URLSearchParams({limit:200});
  if(date) p.set('date',date); if(level) p.set('level',level); if(q) p.set('q',q);
  try {
    const d = await fetch('/api/errors?'+p).then(r=>r.json());
    document.getElementById('err-meta').textContent=`총 ${d.total}건 중 ${Math.min(d.total,200)}건`;
    const tbody=document.getElementById('err-tbody');
    if(!d.rows.length){ tbody.innerHTML='<tr><td colspan="4" class="empty">에러 없음 ✅</td></tr>'; return; }
    tbody.innerHTML=d.rows.map(r=>{
      const mod=r.module.split(':')[0].split('.').slice(-2).join('.');
      const msg=esc(r.message.length>90?r.message.slice(0,90)+'…':r.message);
      const full=JSON.stringify(r.message);
      return `<tr>
        <td class="ts-col">${r.ts.slice(5,16)}</td>
        <td>${badge(r.level)}</td>
        <td class="mod-col" title="${esc(r.module)}">${esc(mod)}</td>
        <td class="msg-col" onclick="showModal('${esc(r.ts)}','${esc(r.level)}','${esc(r.module)}',${full})">${msg}</td>
      </tr>`;
    }).join('');
  } catch(e){ console.error(e); }
}

function clearFilter(){
  document.getElementById('f-date').value=_kstDate();
  document.getElementById('f-level').value='';
  document.getElementById('f-q').value='';
  loadErrors();
}

// ── GNB 모드 셀렉터 ─────────────────────────────────────
async function onModeChange(newMode) {
  const sel = document.getElementById('gnb-mode');
  if(newMode === 'live') {
    if(!confirm('⚠️ 실전 매매 모드로 전환합니다.\n실제 자금으로 거래됩니다. 계속하시겠습니까?')) {
      // 취소 시 원래 값 복원
      sel.value = sel.dataset.current || 'paper';
      return;
    }
  }
  try {
    const r = await fetch('/api/set-mode', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: newMode})
    });
    if(r.ok) {
      sel.dataset.current = newMode;
      updateModeStyle(sel, newMode);
    } else {
      alert('모드 전환 요청 실패');
      sel.value = sel.dataset.current || 'paper';
    }
  } catch(e) {
    alert('오류: '+e);
    sel.value = sel.dataset.current || 'paper';
  }
}

function updateModeStyle(sel, mode) {
  sel.className = 'gnb-select ' + (mode==='live'?'mode-live':'mode-paper');
}

// ── 투자 성향 카드 ───────────────────────────────────────
const LV_LABELS = {1:'매우 공격적',2:'공격적',3:'적극적',4:'균형',5:'보수적',6:'매우 보수적'};
function setDimCell(idPfx, lv, val, sub) {
  const lvEl = document.getElementById(idPfx+'-lv');
  if(lvEl){ lvEl.textContent='L'+lv; lvEl.className='td-dim-lv lv-'+lv; }
  const vEl = document.getElementById(idPfx);
  if(vEl) vEl.textContent = val;
  const sEl = document.getElementById(idPfx+'-sub');
  if(sEl) sEl.textContent = LV_LABELS[lv]||'-';
}
async function loadTendency() {
  try {
    const d = await fetch('/api/tendency').then(r=>r.json());
    if(!d.profile_name) return;
    document.getElementById('tendency-card').style.display = '';
    const badge = document.getElementById('td-badge');
    badge.textContent = d.label || d.profile_name;
    badge.className = 'tendency-badge t-' + d.profile_name;
    const lv = d.levels || {};
    setDimCell('td-tp',   lv.take_profit||2, d.take_profit_pct!=null?'+'+d.take_profit_pct+'%':'-');
    setDimCell('td-sl',   lv.stop_loss  ||2, d.stop_loss_pct  !=null?d.stop_loss_pct+'%'      :'-');
    setDimCell('td-cash', lv.cash       ||2, d.cash_reserve_min!=null?Math.round(d.cash_reserve_min*100)+'%이상':'-');
    setDimCell('td-wt',   lv.max_weight ||2, d.max_weight_pct !=null?'최대 '+d.max_weight_pct+'%':'-');
    setDimCell('td-en',   lv.entry      ||2, d.entry_threshold_pct!=null?'±'+d.entry_threshold_pct+'%':'-');
    // 종합 평가 리포트
    const rationale = d.rationale || '';
    if(rationale) {
      const m = (d.ts||'').match(/(\d{4})-(\d{2})-(\d{2})/);
      const title = m ? `${parseInt(m[2])}월 ${parseInt(m[3])}일 종합 평가 Report` : '종합 평가 Report';
      const LIMIT = 40;
      document.getElementById('td-report-title').textContent = title;
      document.getElementById('td-report-preview').textContent = rationale.length > LIMIT ? rationale.slice(0, LIMIT)+'...' : rationale;
      document.getElementById('td-report-full').textContent   = rationale;
      document.getElementById('td-report-full').style.display = 'none';
      document.getElementById('td-report-more').textContent   = 'more';
      document.getElementById('td-report-more').style.display = rationale.length > LIMIT ? '' : 'none';
      document.getElementById('td-report').style.display = '';
    }
  } catch(e){ console.error('tendency',e); }
}

function toggleReport() {
  const preview = document.getElementById('td-report-preview');
  const full    = document.getElementById('td-report-full');
  const btn     = document.getElementById('td-report-more');
  const expanded = full.style.display !== 'none';
  preview.style.display = expanded ? '' : 'none';
  full.style.display    = expanded ? 'none' : '';
  btn.textContent       = expanded ? 'more' : 'less';
}

// ── 포트폴리오 탭 ────────────────────────────────────────
async function loadPortfolio() {
  try {
    const d = await fetch('/api/portfolio').then(r=>r.json());
    const fmtW = n => n.toLocaleString('ko-KR');
    const pnlColor = n => n>=0?'#f85149':'#4493f8';  // 한국 기준: 상승=빨강, 하락=파랑

    // GNB 셀렉터 동기화
    if(d.trading_mode) {
      const sel = document.getElementById('gnb-mode');
      sel.value = d.trading_mode;
      sel.dataset.current = d.trading_mode;
      updateModeStyle(sel, d.trading_mode);
    }

    document.getElementById('pf-total-eval').textContent = d.total_eval ? fmtW(d.total_eval) : '-';
    const pnlEl = document.getElementById('pf-total-pnl');
    pnlEl.textContent = d.total_pnl !== undefined ? (d.total_pnl>=0?'+':'')+fmtW(d.total_pnl) : '-';
    pnlEl.style.color = pnlColor(d.total_pnl||0);
    document.getElementById('pf-cash').textContent = d.available_cash ? fmtW(d.available_cash) : '-';
    document.getElementById('pf-ts').textContent = d.ts ? '기준: '+d.ts+' KST' : '';
    if(d.ts) document.getElementById('upd-txt').textContent = '갱신 '+d.ts.slice(5,16)+' KST';

    const tbody = document.getElementById('pf-tbody');
    if(!d.holdings || !d.holdings.length){
      tbody.innerHTML='<tr><td colspan="5" class="empty">보유 종목 없음</td></tr>';
      return;
    }
    _pfDataMap = {};
    d.holdings.forEach(h=>{ _pfDataMap[h.symbol] = h; });
    tbody.innerHTML = d.holdings.map(h=>{
      const color = pnlColor(h.pnl_rt);
      const arrow = h.pnl_rt>=0?'▲':'▼';
      return `<tr>
        <td><div class="pf-name">${esc(h.name)}</div><div class="pf-sym">${esc(h.symbol)}</div></td>
        <td>${h.qty.toLocaleString()}</td>
        <td>${h.avg.toLocaleString()}</td>
        <td>${h.current.toLocaleString()}</td>
        <td class="pf-rate-cell" style="color:${color};font-weight:700" onclick="showPfPopup(event,'${h.symbol}',false)">${arrow}${Math.abs(h.pnl_rt).toFixed(2)}%</td>
      </tr>`;
    }).join('');
  } catch(e){ console.error('portfolio',e); }
}

// ── 에이전트 성적표 탭 ─────────────────────────────────
async function loadAgentScores() {
  try {
    const data = await fetch('/api/agent-scores').then(r=>r.json());
    const agents = Object.keys(data);
    if(!agents.length) return;
    const allDates = [...new Set(agents.flatMap(a=>data[a].map(e=>e.date)))].sort().slice(-5);

    document.getElementById('agent-cards').innerHTML = agents.map(agent=>{
      const entries = data[agent];
      if(!entries.length) return `
        <div class="agent-card">
          <div class="agent-name">${agent}</div>
          <div class="agent-score" style="color:#484f58">-</div>
          <div class="agent-date">데이터 없음</div>
          <button class="btn-prompt" onclick="onPromptClick(event,'${agent}',false)">프롬프트</button>
        </div>`;
      const latest = entries[entries.length-1];
      const color  = scoreColor(latest.score);
      return `
        <div class="agent-card">
          <div class="agent-name">${agent}</div>
          <div class="agent-score" style="color:${color}">${latest.score}<span style="font-size:14px;color:#8b949e">/100</span></div>
          <div class="agent-date">${latest.date.slice(5)}</div>
          <button class="btn-prompt" onclick="onPromptClick(event,'${agent}',false)">프롬프트</button>
        </div>`;
    }).join('');

    const thead = `<thead><tr><th>에이전트</th>${allDates.map(d=>`<th>${d.slice(5)}</th>`).join('')}</tr></thead>`;
    const tbody = `<tbody>${agents.map(agent=>{
      const scoreMap = Object.fromEntries(data[agent].map(e=>[e.date, e]));
      const cells = allDates.map(d=>{
        const e = scoreMap[d];
        if(!e) return `<td class="s-none">-</td>`;
        const tip = esc(e.summary||'');
        return `<td class="${scoreBg(e.score)}" title="${tip}" style="cursor:pointer"
          onclick="showAgentModal('${esc(agent)}','${e.date}',${e.score},${JSON.stringify(e.summary||'')},${JSON.stringify(e.improvement||'')})">${e.score}</td>`;
      }).join('');
      return `<tr><td>${agent}</td>${cells}</tr>`;
    }).join('')}</tbody>`;
    document.getElementById('heatmap').innerHTML = thead + tbody;
  } catch(e){ console.error('agent-scores',e); }
}

// ── 토큰 탭 ─────────────────────────────────────────────
async function loadNightTokens() {
  try {
    const d = await fetch('/api/night/token-usage').then(r=>r.json());
    document.getElementById('tk-in-today').textContent   = fmtNum(d.today?.in||0);
    document.getElementById('tk-out-today').textContent  = fmtNum(d.today?.out||0);
    document.getElementById('tk-cost-today').textContent = '$'+((d.today?.cost)||0).toFixed(4);
    const total14 = Object.values(d.daily||{}).reduce((a,v)=>a+(v.cost||0),0);
    document.getElementById('tk-cost-14d').textContent   = '$'+total14.toFixed(4);
    const agents = Object.entries(d.by_agent||{}).sort((a,b)=>(b[1].in+b[1].out)-(a[1].in+a[1].out));
    const maxTok = Math.max(1,...agents.map(([,v])=>v.in+v.out));
    document.getElementById('tok-agent-bars').innerHTML = agents.length ? agents.map(([agent,v])=>{
      const total = v.in + v.out;
      return `<div class="tok-bar-row">
        <div class="tok-name" title="${agent}">${agent.replace('Night','')}</div>
        <div class="tok-track">
          <div class="tok-in"  style="width:${v.in/maxTok*100}%"></div>
          <div class="tok-out" style="width:${v.out/maxTok*100}%"></div>
        </div>
        <div class="tok-val">${fmtNum(total)}<span style="color:#484f58;font-size:9px"> $${v.cost.toFixed(3)}</span></div>
      </div>`;
    }).join('') : '<div class="empty">Night 토큰 데이터 없음</div>';
    const dates = d.dates || [];
    const maxDayTok = Math.max(1,...dates.map(dt=>(d.daily[dt]?.in||0)+(d.daily[dt]?.out||0)));
    document.getElementById('tok-daily-chart').innerHTML = dates.map(dt=>{
      const v = d.daily[dt] || {in:0,out:0,cost:0};
      const total = v.in+v.out;
      return `<div class="bar-row">
        <div class="bar-dt">${dt.slice(5)}</div>
        <div class="bar-track">
          <div class="tok-in"  style="width:${v.in/maxDayTok*100}%"></div>
          <div class="tok-out" style="width:${v.out/maxDayTok*100}%"></div>
        </div>
        <div class="bar-n">${fmtNum(total)}</div>
      </div>`;
    }).join('');
  } catch(e){ console.error('night-tokens',e); }
}

async function loadTokens() {
  try {
    const d = await fetch('/api/token-usage').then(r=>r.json());

    document.getElementById('tk-in-today').textContent   = fmtNum(d.today.in||0);
    document.getElementById('tk-out-today').textContent  = fmtNum(d.today.out||0);
    document.getElementById('tk-cost-today').textContent = '$'+(d.today.cost||0).toFixed(4);
    const total14 = Object.values(d.daily).reduce((a,v)=>a+(v.cost||0),0);
    document.getElementById('tk-cost-14d').textContent   = '$'+total14.toFixed(4);

    // 에이전트별 바 차트
    const agents = Object.entries(d.by_agent).sort((a,b)=>(b[1].in+b[1].out)-(a[1].in+a[1].out));
    const maxTok = Math.max(1,...agents.map(([,v])=>v.in+v.out));
    document.getElementById('tok-agent-bars').innerHTML = agents.length ? agents.map(([agent,v])=>{
      const total = v.in + v.out;
      return `<div class="tok-bar-row">
        <div class="tok-name" title="${agent}">${agent}</div>
        <div class="tok-track">
          <div class="tok-in"  style="width:${v.in/maxTok*100}%"></div>
          <div class="tok-out" style="width:${v.out/maxTok*100}%"></div>
        </div>
        <div class="tok-val">${fmtNum(total)}<span style="color:#484f58;font-size:9px"> $${v.cost.toFixed(3)}</span></div>
      </div>`;
    }).join('') : '<div class="empty">토큰 데이터 없음</div>';

    // 14일 일별 추이
    const maxDayTok = Math.max(1,...d.dates.map(dt=>(d.daily[dt]?.in||0)+(d.daily[dt]?.out||0)));
    document.getElementById('tok-daily-chart').innerHTML = d.dates.map(dt=>{
      const v = d.daily[dt] || {in:0,out:0,cost:0};
      const total = v.in+v.out;
      return `<div class="bar-row">
        <div class="bar-dt">${dt.slice(5)}</div>
        <div class="bar-track">
          <div class="tok-in"  style="width:${v.in/maxDayTok*100}%"></div>
          <div class="tok-out" style="width:${v.out/maxDayTok*100}%"></div>
        </div>
        <div class="bar-n">${fmtNum(total)}</div>
      </div>`;
    }).join('');
  } catch(e){ console.error('tokens',e); }
}

// ── 모달 ────────────────────────────────────────────────
function showModal(ts, level, module, msg) {
  document.getElementById('modal-title').textContent = `[${level}] ${ts}`;
  document.getElementById('modal-body').textContent  = `모듈: ${module}\n\n${msg}`;
  document.getElementById('modal').classList.add('show');
}
function showAgentModal(agent, date, score, summary, improvement) {
  document.getElementById('modal-title').textContent = `${agent} — ${date.slice(5)} (${score}/100)`;
  document.getElementById('modal-body').textContent  =
    `📊 요약\n${summary||'없음'}\n\n💡 개선 포인트\n${improvement||'없음'}`;
  document.getElementById('modal').classList.add('show');
}

// ── 매매일지 상세 팝업 ────────────────────────────────────
let _trSliceData = [];

function showTradeDetail(i) {
  const t = _trSliceData[i];
  if (!t) return;
  const isNight = t.source === 'night';
  const pnlTxt = t.pnl_rate != null ? (t.pnl_rate>=0?'+':'')+t.pnl_rate.toFixed(2)+'%' : '-';
  const pnlColor = t.pnl_rate==null?'#8b949e':t.pnl_rate>=0?'#f85149':'#4493f8';
  const priceStr = t.price
    ? (isNight ? '$'+Number(t.price).toFixed(2) : Number(t.price).toLocaleString()+'원')
    : '시장가';
  const srcLabel = isNight ? '🌙 Night (해외)' : '🐱 Kitty (국내)';

  document.getElementById('modal-title').textContent =
    `${t.name||t.symbol} (${t.symbol}) — ${t.date.slice(5)} ${t.time}`;
  document.getElementById('modal-body').innerHTML =
    `<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px">` +
    `<tr><td style="color:#8b949e;padding:5px 0;width:80px">구분</td><td style="color:#c9d1d9">${srcLabel} / ${t.classify}</td></tr>` +
    `<tr><td style="color:#8b949e;padding:5px 0">수량</td><td style="color:#c9d1d9">${(t.quantity||0).toLocaleString()}${isNight?' shares':'주'}</td></tr>` +
    `<tr><td style="color:#8b949e;padding:5px 0">가격</td><td style="color:#c9d1d9">${priceStr}</td></tr>` +
    `<tr><td style="color:#8b949e;padding:5px 0">수익률</td><td style="color:${pnlColor};font-weight:700">${pnlTxt}</td></tr>` +
    `</table>` +
    `<div style="color:#8b949e;font-size:11px;margin-bottom:6px">사유</div>` +
    `<div style="color:#c9d1d9;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-all">${esc(t.reason||'-')}</div>`;
  document.getElementById('modal').classList.add('show');
}

let _agentPromptsCache = null;
async function onPromptClick(event, agentName, isNight) {
  event.stopPropagation();
  if(!_agentPromptsCache) {
    try { _agentPromptsCache = await fetch('/api/agent-prompts').then(r=>r.json()); }
    catch(e) { _agentPromptsCache = {}; }
  }
  const text = _agentPromptsCache[agentName] || '(프롬프트 없음)';
  const label = isNight ? agentName.replace('Night','') : agentName;
  document.getElementById('modal-title').textContent = label + ' — 시스템 프롬프트';
  document.getElementById('modal-body').textContent  = text;
  document.getElementById('modal').classList.add('show');
}
function closeModal(e){ if(e.target.id==='modal') document.getElementById('modal').classList.remove('show'); }

// ── 포트폴리오 수익률 팝업 ─────────────────────────────────
let _pfDataMap = {};
let _ntPfDataMap = {};

function showPfPopup(event, symbol, isNight) {
  event.stopPropagation();
  const h = isNight ? _ntPfDataMap[symbol] : _pfDataMap[symbol];
  if (!h) return;
  const popup = document.getElementById('pf-popup');

  if (!isNight) {
    const color = pnlColor(h.pnl_rt);
    const arrow = h.pnl_rt >= 0 ? '▲' : '▼';
    const pnlAmt = h.pnl_amt != null ? h.pnl_amt : (h.eval_amt - h.avg * h.qty);
    const pnlSign = pnlAmt >= 0 ? '+' : '';
    popup.innerHTML = `
      <div class="pf-popup-title">${esc(h.name)}<span class="pf-popup-sym">${esc(h.symbol)}</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">수량</span><span class="pf-popup-val">${h.qty.toLocaleString()}주</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">평균단가</span><span class="pf-popup-val">${h.avg.toLocaleString()}원</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">현재가</span><span class="pf-popup-val">${h.current.toLocaleString()}원</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">수익률</span><span class="pf-popup-val" style="color:${color}">${arrow}${Math.abs(h.pnl_rt).toFixed(2)}%</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">손익금액</span><span class="pf-popup-val" style="color:${color}">${pnlSign}${pnlAmt.toLocaleString()}원</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">평가금액</span><span class="pf-popup-val">${h.eval_amt.toLocaleString()}원</span></div>`;
  } else {
    const rate = h.pnl_rate || h.pnl_rt || 0;
    const color = pnlColor(rate);
    const arrow = rate >= 0 ? '▲' : '▼';
    const pnlAmt = h.pnl_amount || 0;
    const pnlSign = pnlAmt >= 0 ? '+' : '';
    popup.innerHTML = `
      <div class="pf-popup-title">${esc(h.name||h.symbol)}<span class="pf-popup-sym">${esc(h.symbol)}</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">Qty</span><span class="pf-popup-val">${(h.quantity||h.qty||0).toLocaleString()} shares</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">Avg Price</span><span class="pf-popup-val">${fmtUSD(h.avg_price||h.avg||0)}</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">Current</span><span class="pf-popup-val">${fmtUSD(h.current_price||h.current||0)}</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">P&L%</span><span class="pf-popup-val" style="color:${color}">${arrow}${Math.abs(rate).toFixed(2)}%</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">P&L $</span><span class="pf-popup-val" style="color:${color}">${pnlSign}${fmtUSD(Math.abs(pnlAmt))}</span></div>
      <div class="pf-popup-row"><span class="pf-popup-lbl">Value</span><span class="pf-popup-val">${fmtUSD(h.eval_amount||h.eval_amt||0)}</span></div>`;
  }

  popup.style.display = 'block';
  const rect = event.target.getBoundingClientRect();
  let top = rect.bottom + 6;
  let left = rect.left;
  const pw = popup.offsetWidth || 220;
  const ph = popup.offsetHeight || 210;
  if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
  if (top + ph > window.innerHeight - 8) top = rect.top - ph - 6;
  popup.style.top = top + 'px';
  popup.style.left = left + 'px';
}

document.addEventListener('click', () => {
  document.getElementById('pf-popup').style.display = 'none';
});

// ── 채팅 탭 ─────────────────────────────────────────────
let _chatPollTimer = null;

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function onChatKey(e) {
  if(e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

function appendChatMsg(role, text, agent) {
  const hist = document.getElementById('chat-history');
  // 초기 안내 문구 제거
  const placeholder = hist.querySelector('div[style]');
  if(placeholder) placeholder.remove();

  const now = _kstTime();  // KST 강제
  const metaText = role==='user' ? now+' KST' : `${agent||''} · ${now} KST`;
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  div.innerHTML = `<div class="chat-bubble">${esc(text)}</div><div class="chat-meta">${metaText}</div>`;
  hist.appendChild(div);
  hist.scrollTop = hist.scrollHeight;
  return div;
}

function appendThinking() {
  const hist = document.getElementById('chat-history');
  const div = document.createElement('div');
  div.id = 'chat-thinking';
  div.className = 'chat-thinking';
  div.textContent = '답변 생성 중...';
  hist.appendChild(div);
  hist.scrollTop = hist.scrollHeight;
}

function removeThinking() {
  const el = document.getElementById('chat-thinking');
  if(el) el.remove();
}

async function sendChat() {
  const agent = document.getElementById('chat-agent').value;
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if(!message) return;

  const btn = document.getElementById('chat-send-btn');
  btn.disabled = true;
  input.value = '';
  input.style.height = 'auto';

  appendChatMsg('user', message);
  appendThinking();

  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({agent, message})
    });
    if(!r.ok) throw new Error('요청 실패');
    const {id} = await r.json();
    pollChatResponse(id, agent);
  } catch(e) {
    removeThinking();
    appendChatMsg('assistant', '오류: ' + e.message, agent);
    btn.disabled = false;
  }
}

async function pollChatResponse(id, agent) {
  const MAX_WAIT = 60; // 최대 60초
  let elapsed = 0;
  const btn = document.getElementById('chat-send-btn');

  const poll = async () => {
    try {
      const r = await fetch(`/api/chat/${id}`).then(x=>x.json());
      if(r.ready) {
        removeThinking();
        appendChatMsg('assistant', r.reply || '(빈 응답)', agent);
        btn.disabled = false;
      } else if(elapsed >= MAX_WAIT) {
        removeThinking();
        appendChatMsg('assistant', '응답 시간 초과. kitty가 실행 중인지 확인해주세요.', agent);
        btn.disabled = false;
      } else {
        elapsed += 2;
        setTimeout(poll, 2000);
      }
    } catch(e) {
      removeThinking();
      appendChatMsg('assistant', '폴링 오류: ' + e.message, agent);
      btn.disabled = false;
    }
  };
  setTimeout(poll, 2000);
}

// ── 매매일지 탭 ─────────────────────────────────────────
const TR_PAGE_SIZE = 10;
let _trAllTrades = [];
let _trPage = 1;

async function loadTrades(resetPage) {
  if(resetPage !== false) _trPage = 1;
  try {
    const dateVal = document.getElementById('tr-date').value;
    const clsVal  = document.getElementById('tr-cls').value;
    const srcVal  = document.getElementById('tr-src').value;
    const d = await fetch('/api/trades?days=30').then(r=>r.json());
    let trades = d.trades || [];

    // 필터
    if(dateVal) trades = trades.filter(t => t.date === dateVal);
    if(clsVal)  trades = trades.filter(t => t.classify === clsVal);
    if(srcVal)  trades = trades.filter(t => t.source === srcVal);

    _trAllTrades = trades;

    // 요약 카운트 (필터된 전체 기준)
    const buys    = trades.filter(t => t.side === '매수').length;
    const sells   = trades.filter(t => t.side === '매도').length;
    const profits = trades.filter(t => t.classify === '익절').length;
    const losses  = trades.filter(t => t.classify === '손절').length;
    const others  = Math.max(0, sells - profits - losses);
    const total   = trades.length;
    document.getElementById('tr-total-cnt').textContent  = total;
    document.getElementById('tr-buy-cnt').textContent    = buys;
    document.getElementById('tr-sell-cnt').textContent   = sells;
    document.getElementById('tr-profit-cnt').textContent = profits;
    document.getElementById('tr-loss-cnt').textContent   = losses;
    document.getElementById('tr-other-cnt').textContent  = others;
    // 가로바 1: 매수/매도
    const pct1 = n => total > 0 ? (n / total * 100).toFixed(1)+'%' : '0%';
    document.getElementById('tr-bar-buy').style.width    = pct1(buys);
    document.getElementById('tr-bar-sell').style.width   = pct1(sells);
    // 가로바 2: 익절/손절/기타 (매도 건 기준)
    const pct2 = n => sells > 0 ? (n / sells * 100).toFixed(1)+'%' : '0%';
    document.getElementById('tr-bar-profit').style.width = pct2(profits);
    document.getElementById('tr-bar-loss').style.width   = pct2(losses);
    document.getElementById('tr-bar-other').style.width  = pct2(others);

    renderTradesPage();
  } catch(e){ console.error('trades',e); }
}

function renderTradesPage() {
  const trades = _trAllTrades;
  const total  = trades.length;
  const pages  = Math.max(1, Math.ceil(total / TR_PAGE_SIZE));
  if(_trPage > pages) _trPage = pages;

  const start  = (_trPage - 1) * TR_PAGE_SIZE;
  const slice  = trades.slice(start, start + TR_PAGE_SIZE);

  document.getElementById('tr-meta').textContent =
    `총 ${total}건 · ${_trPage}/${pages} 페이지`;

  const tbody = document.getElementById('tr-tbody');
  if(!total) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">거래 내역 없음</td></tr>';
    document.getElementById('tr-pagination').innerHTML = '';
    return;
  }

  _trSliceData = slice;
  tbody.innerHTML = slice.map((t, i) => {
    const clsCss = 'trade-cls cls-'+t.classify;
    const srcIcon = t.source==='night' ? '🌙' : '🐱';
    return `<tr>
      <td class="ts-col">${srcIcon} ${t.date.slice(5)}<br><span style="color:#484f58">${t.time}</span></td>
      <td><div class="pf-name">${esc(t.name||t.symbol)}</div><div class="pf-sym">${esc(t.symbol)}</div></td>
      <td><span class="${clsCss}">${t.classify}</span></td>
      <td style="text-align:center"><button class="btn-detail" onclick="showTradeDetail(${i})">자세히</button></td>
    </tr>`;
  }).join('');

  // 페이지네이션
  const pg = document.getElementById('tr-pagination');
  if(pages <= 1) { pg.innerHTML = ''; return; }
  const WINDOW = 5;
  const half = Math.floor(WINDOW / 2);
  let pStart = Math.max(1, _trPage - half);
  let pEnd   = Math.min(pages, pStart + WINDOW - 1);
  if(pEnd - pStart < WINDOW - 1) pStart = Math.max(1, pEnd - WINDOW + 1);

  let html = `<button class="pg-btn" onclick="goTradePage(1)" ${_trPage===1?'disabled':''}>«</button>`;
  html    += `<button class="pg-btn" onclick="goTradePage(${_trPage-1})" ${_trPage===1?'disabled':''}>‹</button>`;
  for(let i = pStart; i <= pEnd; i++) {
    html += `<button class="pg-btn ${i===_trPage?'pg-cur':''}" onclick="goTradePage(${i})">${i}</button>`;
  }
  html += `<button class="pg-btn" onclick="goTradePage(${_trPage+1})" ${_trPage===pages?'disabled':''}>›</button>`;
  html += `<button class="pg-btn" onclick="goTradePage(${pages})" ${_trPage===pages?'disabled':''}>»</button>`;
  pg.innerHTML = html;
}

function goTradePage(p) {
  _trPage = p;
  renderTradesPage();
}

function clearTradeFilter() {
  document.getElementById('tr-date').value = '';
  document.getElementById('tr-cls').value  = '';
  document.getElementById('tr-src').value  = '';
  loadTrades();
}

// ── Night Mode 데이터 로드 ───────────────────────────────
const NLV_LABELS = {1:'Very Aggressive',2:'Aggressive',3:'Active',4:'Balanced',5:'Conservative',6:'Very Conservative'};
function setNightDimCell(idPfx, lv, val) {
  const lvEl = document.getElementById(idPfx+'-lv');
  if(lvEl){ lvEl.textContent='L'+lv; lvEl.className='td-dim-lv lv-'+lv; }
  const vEl = document.getElementById(idPfx);
  if(vEl) vEl.textContent = val;
  const sEl = document.getElementById(idPfx+'-sub');
  if(sEl) sEl.textContent = NLV_LABELS[lv]||'-';
}

async function loadNightTendency() {
  try {
    const d = await fetch('/api/night/tendency').then(r=>r.json());
    if(!d.profile_name) return;
    document.getElementById('night-tendency-card').style.display = '';
    const badge = document.getElementById('nt-badge');
    badge.textContent = d.label || d.profile_name;
    badge.className = 'tendency-badge t-' + d.profile_name;
    const lv = d.levels || {};
    setNightDimCell('nt-tp',   lv.take_profit||2, d.take_profit_pct!=null?'+'+d.take_profit_pct+'%':'-');
    setNightDimCell('nt-sl',   lv.stop_loss  ||2, d.stop_loss_pct  !=null?d.stop_loss_pct+'%'      :'-');
    setNightDimCell('nt-cash', lv.cash       ||2, d.cash_reserve_min!=null?Math.round(d.cash_reserve_min*100)+'%+':'-');
    setNightDimCell('nt-wt',   lv.max_weight ||2, d.max_weight_pct !=null?'max '+d.max_weight_pct+'%':'-');
    setNightDimCell('nt-en',   lv.entry      ||2, d.entry_threshold_pct!=null?'±'+d.entry_threshold_pct+'%':'-');
    const rationale = d.rationale || '';
    if(rationale) {
      const m = (d.ts||'').match(/(\d{4})-(\d{2})-(\d{2})/);
      const title = m ? `${m[2]}/${m[3]} Night Report` : 'Night Report';
      const LIMIT = 40;
      document.getElementById('nt-report-title').textContent = title;
      document.getElementById('nt-report-preview').textContent = rationale.length > LIMIT ? rationale.slice(0,LIMIT)+'...' : rationale;
      document.getElementById('nt-report-full').textContent = rationale;
      document.getElementById('nt-report-full').style.display = 'none';
      document.getElementById('nt-report-more').textContent = 'more';
      document.getElementById('nt-report-more').style.display = rationale.length > LIMIT ? '' : 'none';
      document.getElementById('nt-report').style.display = '';
    }
  } catch(e){ console.error('night-tendency',e); }
}

function toggleNightReport() {
  const preview = document.getElementById('nt-report-preview');
  const full = document.getElementById('nt-report-full');
  const btn = document.getElementById('nt-report-more');
  const expanded = full.style.display !== 'none';
  preview.style.display = expanded ? '' : 'none';
  full.style.display = expanded ? 'none' : '';
  btn.textContent = expanded ? 'more' : 'less';
}

async function loadNightPortfolio() {
  try {
    const d = await fetch('/api/night/portfolio').then(r=>r.json());
    const fmtUSD = n => '$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    const pnlColor = n => n>=0?'#3fb950':'#f85149';

    document.getElementById('nt-total-eval').textContent = d.total_eval ? fmtUSD(d.total_eval) : '-';
    const pnlEl = document.getElementById('nt-total-pnl');
    pnlEl.textContent = d.total_pnl !== undefined ? (d.total_pnl>=0?'+':'')+fmtUSD(d.total_pnl) : '-';
    pnlEl.style.color = pnlColor(d.total_pnl||0);
    document.getElementById('nt-cash-val').textContent = d.available_cash ? fmtUSD(d.available_cash) : '-';
    document.getElementById('nt-pf-ts').textContent = d.ts ? 'as of '+d.ts+' KST' : '';

    const tbody = document.getElementById('nt-pf-tbody');
    if(!d.holdings || !d.holdings.length){
      tbody.innerHTML='<tr><td colspan="5" class="empty">No holdings</td></tr>';
      return;
    }
    _ntPfDataMap = {};
    d.holdings.forEach(h=>{ _ntPfDataMap[h.symbol] = h; });
    tbody.innerHTML = d.holdings.map(h=>{
      const color = pnlColor(h.pnl_rate||h.pnl_rt||0);
      const rate = h.pnl_rate||h.pnl_rt||0;
      const arrow = rate>=0?'▲':'▼';
      return `<tr>
        <td><div class="pf-name">${esc(h.name||h.symbol)}</div><div class="pf-sym">${esc(h.symbol)}</div></td>
        <td>${(h.quantity||h.qty||0).toLocaleString()}</td>
        <td>${fmtUSD(h.avg_price||h.avg||0)}</td>
        <td>${fmtUSD(h.current_price||h.current||0)}</td>
        <td class="pf-rate-cell" style="color:${color};font-weight:700" onclick="showPfPopup(event,'${h.symbol}',true)">${arrow}${Math.abs(rate).toFixed(2)}%</td>
      </tr>`;
    }).join('');
  } catch(e){ console.error('night-portfolio',e); }
}

async function loadNightAgentScores() {
  try {
    const data = await fetch('/api/night/agent-scores').then(r=>r.json());
    const agents = Object.keys(data);
    if(!agents.length){ document.getElementById('nt-agent-cards').innerHTML='<div class="empty">No night agent data yet</div>'; return; }
    const allDates = [...new Set(agents.flatMap(a=>data[a].map(e=>e.date)))].sort().slice(-5);

    document.getElementById('nt-agent-cards').innerHTML = agents.map(agent=>{
      const entries = data[agent];
      const shortName = agent.replace('Night','');
      if(!entries.length) return `<div class="agent-card"><div class="agent-name">${shortName}</div><div class="agent-score" style="color:#484f58">-</div><div class="agent-date">No data</div><button class="btn-prompt" onclick="onPromptClick(event,'${agent}',true)">Prompt</button></div>`;
      const latest = entries[entries.length-1];
      const color = scoreColor(latest.score);
      return `<div class="agent-card"><div class="agent-name">${shortName}</div><div class="agent-score" style="color:${color}">${latest.score}<span style="font-size:14px;color:#8b949e">/100</span></div><div class="agent-date">${latest.date.slice(5)}</div><button class="btn-prompt" onclick="onPromptClick(event,'${agent}',true)">Prompt</button></div>`;
    }).join('');

    const thead = `<thead><tr><th>Agent</th>${allDates.map(d=>`<th>${d.slice(5)}</th>`).join('')}</tr></thead>`;
    const tbody = `<tbody>${agents.map(agent=>{
      const scoreMap = Object.fromEntries(data[agent].map(e=>[e.date,e]));
      const cells = allDates.map(d=>{
        const e=scoreMap[d]; if(!e) return '<td class="s-none">-</td>';
        return `<td class="${scoreBg(e.score)}" title="${esc(e.summary||'')}" style="cursor:pointer">${e.score}</td>`;
      }).join('');
      return `<tr><td>${agent.replace('Night','')}</td>${cells}</tr>`;
    }).join('')}</tbody>`;
    document.getElementById('nt-heatmap').innerHTML = thead + tbody;
  } catch(e){ console.error('night-agent-scores',e); }
}

// ── 성향관리 탭 ─────────────────────────────────────
async function loadAdvisor() {
  try {
    const [fbRes, prRes] = await Promise.all([
      fetch('/api/agent-feedback').then(r=>r.json()),
      fetch('/api/agent-prompts').then(r=>r.json()),
    ]);
    _advFeedback = fbRes;
    _advPrompts = prRes;
    renderAdvImprovements();
    showAdvPrompt();
  } catch(e){ console.error('loadAdvisor', e); }
}

function renderAdvImprovements() {
  const filter = document.getElementById('adv-agent-filter').value;
  const container = document.getElementById('adv-improvements');
  const agents = Object.keys(_advFeedback).filter(a => !filter || a===filter);
  const hasAny = agents.some(a => _advFeedback[a]?.length > 0);
  if (!hasAny) {
    container.innerHTML = '<div class="empty">개선 피드백 없음</div>';
    return;
  }
  container.innerHTML = agents.filter(a => _advFeedback[a]?.length > 0).map(agent => {
    const items = [..._advFeedback[agent]].reverse();
    return `<div class="adv-agent-block">
      <div class="adv-agent-name">${esc(agent)}</div>
      ${items.map(it=>`<div class="adv-item">
        <span class="adv-item-date">${esc(it.date.slice(5))}</span>
        <span class="adv-item-text">${esc(it.improvement)}</span>
      </div>`).join('')}
    </div>`;
  }).join('');
}

function showAdvPrompt() {
  const sel = document.getElementById('adv-prompt-sel').value;
  const box = document.getElementById('adv-prompt-box');
  if (!sel) { box.style.display='none'; return; }
  const prompt = _advPrompts[sel] || '(프롬프트 없음)';
  document.getElementById('adv-prompt-text').textContent = prompt;
  const feedbacks = (_advFeedback[sel]||[]).slice(-5);
  const fbEl = document.getElementById('adv-prompt-feedback');
  if (feedbacks.length) {
    const txt = '[📊 현재 주입 중인 피드백]\n' + feedbacks.map(f=>`• ${f.date}: ${f.improvement}`).join('\n');
    fbEl.textContent = txt;
    fbEl.style.display = 'block';
  } else {
    fbEl.style.display = 'none';
  }
  box.style.display = 'block';
}

async function sendAdvChat() {
  const input = document.getElementById('adv-chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  autoResize(input);
  appendAdvMsg('user', msg);
  const btn = document.getElementById('adv-send-btn');
  btn.disabled = true; btn.textContent = '...';
  const box = document.getElementById('adv-chat-box');
  const thinking = document.createElement('div');
  thinking.className = 'adv-thinking';
  thinking.textContent = '분석 중...';
  box.appendChild(thinking);
  box.scrollTop = box.scrollHeight;
  try {
    const res = await fetch('/api/tendency-advisor', {
      method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({message: msg, history: _advHistory}),
    });
    const data = await res.json();
    thinking.remove();
    _advHistory.push({role:'user', content: msg});
    _advHistory.push({role:'assistant', content: data.reply});
    appendAdvMsg('ai', data.reply);
    if (data.suggestions?.length) {
      _advSuggestions = data.suggestions;
      renderAdvSuggestions();
    }
  } catch(e) {
    thinking.remove();
    appendAdvMsg('ai', '오류가 발생했습니다: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '전송';
  }
}

function appendAdvMsg(role, text) {
  const box = document.getElementById('adv-chat-box');
  const ph = document.getElementById('adv-chat-placeholder');
  if (ph) ph.remove();
  const div = document.createElement('div');
  div.className = role==='user' ? 'adv-msg-user' : 'adv-msg-ai';
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function renderAdvSuggestions() {
  const section = document.getElementById('adv-sugg-section');
  const list = document.getElementById('adv-sugg-list');
  list.innerHTML = _advSuggestions.map((s,i)=>`
    <div class="adv-sugg-item">
      <div class="adv-sugg-agent">${esc(s.agent)}</div>
      <div class="adv-sugg-text">${esc(s.improvement)}</div>
      <button class="btn btn-pri" style="font-size:11px;padding:4px 10px;margin-top:6px"
        onclick="saveAdvImprovement(${i},this)">저장</button>
    </div>`).join('');
  section.style.display = 'block';
}

async function saveAdvImprovement(idx, btn) {
  const s = _advSuggestions[idx];
  if (!s) return;
  btn.disabled = true; btn.textContent = '저장 중...';
  try {
    const res = await fetch('/api/agent-feedback/add', {
      method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({agent: s.agent, improvement: s.improvement, summary: 'AI 제안'}),
    });
    if (res.ok) {
      btn.textContent = '✓ 저장됨';
      await loadAdvisor();
    } else {
      btn.disabled = false; btn.textContent = '저장';
    }
  } catch(e) {
    btn.disabled = false; btn.textContent = '저장';
  }
}

function clearAdvChat() {
  _advHistory = []; _advSuggestions = [];
  document.getElementById('adv-chat-box').innerHTML =
    '<div id="adv-chat-placeholder" class="adv-thinking" style="animation:none;color:#484f58;font-style:normal;font-size:12px;padding:8px">에이전트 성과와 개선 방향에 대해 대화하세요.</div>';
  document.getElementById('adv-sugg-section').style.display = 'none';
}

function onAdvKey(e) {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendAdvChat(); }
}

// ── 초기화 & 자동 갱신 ──────────────────────────────────
switchMain('agents');

// 60초 자동 갱신
setInterval(()=>{
  if(document.getElementById('tab-agents').classList.contains('active')){
    if(_currentView === 'night'){ loadNightPortfolio(); loadNightAgentScores(); }
    else { loadPortfolio(); loadAgentScores(); }
  }
  if(document.getElementById('tab-trades').classList.contains('active')){
    loadTrades(false);
  }
}, 60000);

// 관리 탭 30초 자동 갱신
setInterval(()=>{
  if(document.getElementById('tab-errors').classList.contains('active')){ loadStats(); loadErrors(); }
  if(document.getElementById('tab-tokens').classList.contains('active')){
    if(_currentView === 'night') loadNightTokens();
    else loadTokens();
  }
}, 30000);
</script>
</body>
</html>
"""
