"""Kitty 모니터 — FastAPI + SQLite + 4탭 모바일 대시보드

수집 대상: /logs/kitty_YYYY-MM-DD.log 파일의 ERROR / WARNING / CRITICAL 라인
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
    cutoff = (datetime.utcnow() - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
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
        raw = path.read_bytes()
    except OSError:
        return []
    new_bytes = raw[start:]
    if not new_bytes:
        return []
    entries = []
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
        (filename, len(raw)),
    )
    conn.commit()
    return entries


def _last_log_ts() -> Optional[str]:
    """가장 최근 로그 라인의 타임스탬프 (모든 레벨)"""
    latest = None
    for path in sorted(LOG_DIR.glob("kitty_*.log"), reverse=True)[:2]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in reversed(text.splitlines()):
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
    for path in sorted(LOG_DIR.glob("kitty_*.log")):
        scan_file(path, conn)
    cleanup_old(conn)
    conn.close()
    while True:
        await asyncio.sleep(POLL_SEC)
        conn = _db()
        new: list[dict] = []
        for path in sorted(LOG_DIR.glob("kitty_*.log")):
            new.extend(scan_file(path, conn))
        if datetime.now().hour == 0 and datetime.now().minute < 1:
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
    today = datetime.now().strftime("%Y-%m-%d")
    hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
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
    today = datetime.now().strftime("%Y-%m-%d")
    with _db() as c:
        daily = c.execute("""
            SELECT date, level, COUNT(*) cnt FROM errors
            WHERE date >= date('now','-13 days')
            GROUP BY date, level ORDER BY date
        """).fetchall()
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
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

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


@app.get("/", response_class=HTMLResponse)
def dashboard(req: Request):
    _auth(req)
    return _HTML


# ── 대시보드 HTML ─────────────────────────────────────────────────────────────

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
.logo{font-size:15px;font-weight:700;color:#f0f6fc;flex-shrink:0}
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
table.pf{width:100%;border-collapse:collapse;font-size:12px;min-width:420px}
table.pf th{background:#161b22;padding:7px 10px;text-align:right;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;border-bottom:1px solid #30363d}
table.pf th:first-child{text-align:left}
table.pf td{padding:7px 10px;border-bottom:1px solid #161b22;text-align:right;vertical-align:middle}
table.pf td:first-child{text-align:left}
table.pf tr:last-child td{border-bottom:none}
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
</style>
</head>
<body>
<header>
  <div class="logo">🐱 Kitty Monitor</div>
  <div class="gnb">
    <select id="gnb-mode" class="gnb-select" onchange="onModeChange(this.value)" title="매매 모드">
      <option value="paper">📄 paper</option>
      <option value="live">🔴 live</option>
    </select>
  </div>
  <div class="upd"><span class="dot"></span><span id="upd-txt">연결 중...</span></div>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('health')">🏥 상태</div>
  <div class="tab" onclick="switchTab('errors')">📋 에러</div>
  <div class="tab" onclick="switchTab('agents')">🤖 성적표</div>
  <div class="tab" onclick="switchTab('tokens')">🔢 토큰</div>
</div>

<!-- ══ 상태 탭 ══ -->
<div id="tab-health" class="tab-content active">
<div class="wrap">
  <div id="status-badge" class="status-badge status-ok">⏳ 로딩 중...</div>
  <div class="cards">
    <div class="card"><div class="num red"   id="h-err-today">-</div><div class="lbl">오늘 에러</div></div>
    <div class="card"><div class="num yellow" id="h-warn-today">-</div><div class="lbl">오늘 경고</div></div>
    <div class="card"><div class="num red"   id="h-err-1h">-</div><div class="lbl">1시간 에러</div></div>
  </div>
  <div class="section">
    <div class="sec-title">최근 마지막 로그</div>
    <div id="h-last-log" style="font-size:13px;color:#8b949e;padding:4px 0">-</div>
  </div>
  <div class="section">
    <div class="sec-title">최근 에러 5건</div>
    <div id="h-recent-errors"><div class="empty">없음 ✅</div></div>
  </div>
</div>
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

<!-- ══ 에이전트 성적표 탭 ══ -->
<div id="tab-agents" class="tab-content">
<div class="wrap">
  <!-- 포트폴리오 현황 -->
  <div class="section">
    <div class="sec-title">현재 포트폴리오</div>

    <div class="cards" id="pf-summary-cards" style="margin-bottom:10px">
      <div class="card"><div class="num blue"  id="pf-total-eval">-</div><div class="lbl">총평가금액</div></div>
      <div class="card"><div class="num"       id="pf-total-pnl">-</div><div class="lbl">평가손익</div></div>
      <div class="card"><div class="num gray"  id="pf-cash">-</div><div class="lbl">주문가능현금</div></div>
    </div>
    <div class="pf-wrap">
      <table class="pf">
        <thead><tr>
          <th>종목</th><th>수량</th><th>평균단가</th><th>현재가</th><th>수익률</th><th>평가금액</th>
        </tr></thead>
        <tbody id="pf-tbody"><tr><td colspan="6" class="empty">로딩 중...</td></tr></tbody>
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

<!-- 모달 -->
<div class="modal-bg" id="modal" onclick="closeModal(event)">
  <div class="modal">
    <button class="close-btn" onclick="document.getElementById('modal').classList.remove('show')">✕</button>
    <h3 id="modal-title"></h3>
    <pre id="modal-body"></pre>
  </div>
</div>

<script>
const TAB_NAMES = ['health','errors','agents','tokens'];

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i)=>{
    t.classList.toggle('active', TAB_NAMES[i]===name);
  });
  document.querySelectorAll('.tab-content').forEach(c=>{
    c.classList.toggle('active', c.id==='tab-'+name);
  });
  if(name==='agents'){ loadPortfolio(); loadAgentScores(); }
  if(name==='tokens') loadTokens();
}

const today = new Date().toISOString().slice(0,10);
document.getElementById('f-date').value = today;

const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const badge = lvl => {
  const cls = lvl==='ERROR'?'ERR-b':lvl==='WARNING'?'WARN-b':'CRIT-b';
  return `<span class="badge ${cls}">${lvl}</span>`;
};
const scoreColor = s => s>=7?'#3fb950':s>=4?'#d29922':'#f85149';
const scoreBg    = s => s>=7?'s-hi':s>=4?'s-mid':'s-lo';
const fmtNum = n => n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n);

// ── 상태 탭 ─────────────────────────────────────────────
async function loadHealth() {
  try {
    const d = await fetch('/api/health').then(r=>r.json());
    document.getElementById('h-err-today').textContent  = d.err_today;
    document.getElementById('h-warn-today').textContent = d.warn_today;
    document.getElementById('h-err-1h').textContent     = d.err_1h;
    document.getElementById('h-last-log').textContent   = d.last_log_ts || '로그 없음';
    if(d.last_log_ts) document.getElementById('upd-txt').textContent = '갱신 '+d.last_log_ts.slice(5,16);

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
    if(d.latest) document.getElementById('upd-txt').textContent = '갱신 '+d.latest.slice(5,16);

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
  document.getElementById('f-date').value=today;
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

// ── 포트폴리오 탭 ────────────────────────────────────────
async function loadPortfolio() {
  try {
    const d = await fetch('/api/portfolio').then(r=>r.json());
    const fmtW = n => n.toLocaleString('ko-KR')+'원';
    const pnlColor = n => n>=0?'#3fb950':'#f85149';

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
    document.getElementById('pf-ts').textContent = d.ts ? '기준: '+d.ts : '';

    const tbody = document.getElementById('pf-tbody');
    if(!d.holdings || !d.holdings.length){
      tbody.innerHTML='<tr><td colspan="6" class="empty">보유 종목 없음</td></tr>';
      return;
    }
    tbody.innerHTML = d.holdings.map(h=>{
      const color = pnlColor(h.pnl_rt);
      const arrow = h.pnl_rt>=0?'▲':'▼';
      return `<tr>
        <td><div class="pf-name">${esc(h.name)}</div><div class="pf-sym">${esc(h.symbol)}</div></td>
        <td>${h.qty.toLocaleString()}주</td>
        <td>${h.avg.toLocaleString()}원</td>
        <td>${h.current.toLocaleString()}원</td>
        <td style="color:${color};font-weight:700">${arrow}${Math.abs(h.pnl_rt).toFixed(2)}%</td>
        <td style="color:${color}">${h.eval_amt.toLocaleString()}원</td>
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
    const allDates = [...new Set(agents.flatMap(a=>data[a].map(e=>e.date)))].sort().slice(-7);

    document.getElementById('agent-cards').innerHTML = agents.map(agent=>{
      const entries = data[agent];
      if(!entries.length) return `
        <div class="agent-card">
          <div class="agent-name">${agent}</div>
          <div class="agent-score" style="color:#484f58">-</div>
          <div class="agent-date">데이터 없음</div>
        </div>`;
      const latest = entries[entries.length-1];
      const color  = scoreColor(latest.score);
      const recent = entries.slice(-7);
      const bars = recent.map(e=>`
        <div class="s-bar-row">
          <div class="s-bar-dt">${e.date.slice(5)}</div>
          <div class="s-bar-track">
            <div class="s-bar-fill" style="width:${e.score*10}%;background:${scoreColor(e.score)}"></div>
          </div>
          <div class="s-bar-n">${e.score}</div>
        </div>`).join('');
      return `
        <div class="agent-card">
          <div class="agent-name">${agent}</div>
          <div class="agent-score" style="color:${color}">${latest.score}<span style="font-size:14px;color:#8b949e">/10</span></div>
          <div class="agent-date">${latest.date}</div>
          <div class="score-bars">${bars}</div>
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
  document.getElementById('modal-title').textContent = `${agent} — ${date} (${score}/10)`;
  document.getElementById('modal-body').textContent  =
    `📊 요약\n${summary||'없음'}\n\n💡 개선 포인트\n${improvement||'없음'}`;
  document.getElementById('modal').classList.add('show');
}
function closeModal(e){ if(e.target.id==='modal') document.getElementById('modal').classList.remove('show'); }

// ── 초기화 & 자동 갱신 ──────────────────────────────────
loadHealth();
loadStats();
loadErrors();
setInterval(()=>{ loadHealth(); loadStats(); loadErrors(); }, 30000);
setInterval(()=>{
  if(document.getElementById('tab-agents').classList.contains('active')){ loadPortfolio(); loadAgentScores(); }
  if(document.getElementById('tab-tokens').classList.contains('active')) loadTokens();
}, 60000);
</script>
</body>
</html>
"""
