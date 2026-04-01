"""Kitty 에러 로그 모니터 — FastAPI + SQLite + 모바일 대시보드

수집 대상: /logs/kitty_YYYY-MM-DD.log 파일의 ERROR / WARNING / CRITICAL 라인
저장소   : /data/monitor.db (SQLite, 30일 보관)
대시보드 : http://EC2-IP:8080  (HTTP Basic Auth)
텔레그램 : CRITICAL 즉시 알림 + 5분 내 ERROR 3건 이상 버스트 알림 (선택)
"""
import asyncio
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
from fastapi.responses import HTMLResponse, JSONResponse

# ── 환경변수 ────────────────────────────────────────────────────────────────
LOG_DIR        = Path(os.getenv("LOG_DIR", "/logs"))
DB_PATH        = Path(os.getenv("DB_PATH", "/data/monitor.db"))
PASSWORD       = os.getenv("MONITOR_PASSWORD", "kitty")   # Basic Auth 비밀번호
POLL_SEC       = int(os.getenv("POLL_SEC", "15"))          # 로그 폴링 주기(초)
RETAIN_DAYS    = int(os.getenv("RETAIN_DAYS", "30"))       # DB 보관 일수
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT        = os.getenv("TELEGRAM_CHAT_ID", "")
BURST_WINDOW   = 300    # 버스트 감지 윈도우 (초)
BURST_THRESH   = 3      # 버스트 임계 건수

# ── 로그 파싱 정규식 ─────────────────────────────────────────────────────────
# 2026-04-01 16:08:00.604 | ERROR     | kitty.broker.kis:_get_token:74 - 메시지
_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+"
    r" \| (ERROR|WARNING|CRITICAL)\s+"
    r"\| (\S+) - (.+)$"
)

# ── SQLite ──────────────────────────────────────────────────────────────────

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
    """파일의 새 라인만 읽어 ERROR/WARNING/CRITICAL 파싱 후 DB 저장, 새 항목 반환"""
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
            "ts":       ts_str,
            "date":     ts_str[:10],
            "level":    level,
            "module":   module,
            "message":  message.strip(),
            "log_file": filename,
        })

    insert_entries(conn, entries)
    conn.execute(
        "INSERT INTO file_pos(filename,position) VALUES(?,?) "
        "ON CONFLICT(filename) DO UPDATE SET position=excluded.position",
        (filename, len(raw)),
    )
    conn.commit()
    return entries


# ── 텔레그램 알림 ────────────────────────────────────────────────────────────

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

# 버스트 감지: module → [(timestamp, ...)]
_burst_buf: dict[str, list[float]] = defaultdict(list)
# 중복 알림 억제: 최근 발송 시각
_last_alert: dict[str, float] = {}


async def _check_alerts(new_entries: list[dict]) -> None:
    now = asyncio.get_event_loop().time()
    for e in new_entries:
        key = e["module"].split(":")[0]   # 모듈명만

        # CRITICAL → 즉시 알림 (1시간 중복 억제)
        if e["level"] == "CRITICAL":
            if now - _last_alert.get(f"CRIT:{key}", 0) > 3600:
                _last_alert[f"CRIT:{key}"] = now
                await tg_send(
                    f"🔴 *CRITICAL* `{e['module']}`\n"
                    f"`{e['ts']}`\n{e['message'][:200]}"
                )

        # 버스트 감지 (5분 내 ERROR 3회 이상)
        if e["level"] == "ERROR":
            buf = _burst_buf[key]
            buf.append(now)
            # BURST_WINDOW 초 이전 항목 제거
            cutoff = now - BURST_WINDOW
            _burst_buf[key] = [t for t in buf if t >= cutoff]
            if len(_burst_buf[key]) >= BURST_THRESH:
                alert_key = f"BURST:{key}"
                if now - _last_alert.get(alert_key, 0) > BURST_WINDOW:
                    _last_alert[alert_key] = now
                    cnt = len(_burst_buf[key])
                    await tg_send(
                        f"⚠️ *에러 버스트* `{key}` — {BURST_WINDOW//60}분 내 {cnt}건\n"
                        f"최근: {e['message'][:150]}"
                    )


async def _watcher() -> None:
    # 최초 기동: 기존 모든 로그 파일 스캔
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
        # 매일 자정 이후 첫 폴링 시 정리
        if datetime.now().hour == 0 and datetime.now().minute < 1:
            cleanup_old(conn)
        conn.close()
        if new:
            await _check_alerts(new)


# ── FastAPI ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_watcher())
    yield
    task.cancel()

app = FastAPI(title="Kitty Monitor", lifespan=lifespan)


def _auth(req: Request) -> None:
    """HTTP Basic Auth. MONITOR_PASSWORD 미설정 시 인증 없음."""
    if not PASSWORD:
        return
    import base64
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        from fastapi import HTTPException
        raise HTTPException(
            401, "인증 필요",
            headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'},
        )
    try:
        _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(401, "잘못된 인증 형식",
                            headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'})
    if not compare_digest(pwd, PASSWORD):
        from fastapi import HTTPException
        raise HTTPException(401, "비밀번호 오류",
                            headers={"WWW-Authenticate": 'Basic realm="Kitty Monitor"'})


@app.get("/health")
def health():
    return {"status": "ok"}


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
    if date:  cond.append("date=?");             params.append(date)
    if level: cond.append("level=?");            params.append(level.upper())
    if q:     cond.append("message LIKE ?");     params.append(f"%{q}%")
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
    with _db() as c:
        daily  = c.execute("""
            SELECT date, level, COUNT(*) cnt FROM errors
            WHERE date >= date('now','-13 days')
            GROUP BY date, level ORDER BY date
        """).fetchall()
        totals = c.execute(
            "SELECT level, COUNT(*) cnt FROM errors GROUP BY level"
        ).fetchall()
        latest = c.execute("SELECT MAX(ts) FROM errors").fetchone()[0]
        today  = datetime.now().strftime("%Y-%m-%d")
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
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
/* 헤더 */
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 16px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100}
.logo{font-size:15px;font-weight:700;color:#f0f6fc}
.update-info{font-size:11px;color:#8b949e;display:flex;align-items:center;gap:6px}
.dot{width:7px;height:7px;border-radius:50%;background:#3fb950;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
/* 컨테이너 */
.wrap{padding:12px 14px;max-width:860px;margin:0 auto}
/* 카드 */
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 10px;text-align:center}
.card .num{font-size:26px;font-weight:700;line-height:1}
.card .lbl{font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.red{color:#f85149}.yellow{color:#d29922}.blue{color:#58a6ff}
/* 차트 */
.section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:14px}
.sec-title{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:3px;font-size:10px}
.bar-dt{width:44px;color:#8b949e;flex-shrink:0;text-align:right}
.bar-track{flex:1;height:11px;background:#21262d;border-radius:3px;display:flex;overflow:hidden}
.bar-e{background:#f85149;height:100%}.bar-w{background:#d29922;height:100%}
.bar-n{width:28px;text-align:right;color:#8b949e;flex-shrink:0;font-size:10px}
/* 필터 */
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.filters input,.filters select{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 10px;font-size:13px;flex:1;min-width:70px;outline:none}
.filters input:focus,.filters select:focus{border-color:#58a6ff}
.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{background:#30363d}
.btn-pri{background:#238636;border-color:#2ea043;color:#fff}
.btn-pri:hover{background:#2ea043}
/* 테이블 */
.tbl-wrap{overflow-x:auto;border:1px solid #30363d;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:480px}
th{background:#161b22;padding:8px 10px;text-align:left;color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;white-space:nowrap;border-bottom:1px solid #30363d}
td{padding:8px 10px;border-bottom:1px solid #161b22;vertical-align:top}
tr:hover td{background:#161b22}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap}
.ERR-b{background:#3d1a1a;color:#f85149}.WARN-b{background:#3d2c00;color:#d29922}.CRIT-b{background:#3d1a1a;color:#ff7b72}
.ts-col{color:#8b949e;font-size:11px;white-space:nowrap}
.mod-col{color:#79c0ff;font-size:10px;white-space:nowrap;max-width:120px;overflow:hidden;text-overflow:ellipsis}
.msg-col{color:#c9d1d9;word-break:break-word;cursor:pointer}
.msg-col:hover{color:#f0f6fc}
.meta{font-size:11px;color:#8b949e;margin-bottom:6px}
.empty{text-align:center;color:#484f58;padding:32px;font-size:13px}
/* 모달 */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;padding:16px}
.modal-bg.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;max-width:600px;width:100%;max-height:80vh;overflow-y:auto}
.modal h3{font-size:13px;margin-bottom:10px;color:#f0f6fc}
.modal pre{font-size:12px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.close-btn{float:right;background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;line-height:1}
</style>
</head>
<body>
<header>
  <div class="logo">🐱 Kitty Monitor</div>
  <div class="update-info"><span class="dot"></span><span id="upd">연결 중...</span></div>
</header>
<div class="wrap">
  <!-- 요약 카드 -->
  <div class="cards">
    <div class="card"><div class="num red" id="c-err">-</div><div class="lbl">오늘 에러</div></div>
    <div class="card"><div class="num yellow" id="c-warn">-</div><div class="lbl">오늘 경고</div></div>
    <div class="card"><div class="num blue" id="c-total">-</div><div class="lbl">전체</div></div>
  </div>
  <!-- 차트 -->
  <div class="section">
    <div class="sec-title">14일 에러 추이</div>
    <div id="chart"></div>
  </div>
  <!-- 필터 -->
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
  <div class="meta" id="meta"></div>
  <!-- 테이블 -->
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>시각</th><th>레벨</th><th>모듈</th><th>메시지 (클릭=전체)</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
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
// 오늘 날짜 기본값
const todayStr = new Date().toISOString().slice(0,10);
document.getElementById('f-date').value = todayStr;

const badge = lvl => {
  const cls = lvl==='ERROR'?'ERR-b':lvl==='WARNING'?'WARN-b':'CRIT-b';
  return `<span class="badge ${cls}">${lvl}</span>`;
};
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

async function loadStats() {
  try {
    const d = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('c-err').textContent = (d.today['ERROR']||0)+(d.today['CRITICAL']||0);
    document.getElementById('c-warn').textContent = d.today['WARNING']||0;
    const tot = Object.values(d.totals).reduce((a,v)=>a+v,0);
    document.getElementById('c-total').textContent = tot;
    if(d.latest) document.getElementById('upd').textContent = '갱신: '+d.latest.slice(5,16);

    // 차트 생성
    const dates = [...new Set(d.daily.map(x=>x.date))].sort();
    const maxN  = Math.max(1,...dates.map(dt=>
      d.daily.filter(x=>x.date===dt).reduce((a,x)=>a+x.cnt,0)
    ));
    document.getElementById('chart').innerHTML = dates.map(dt => {
      const rows = d.daily.filter(x=>x.date===dt);
      const err  = rows.filter(x=>x.level==='ERROR'||x.level==='CRITICAL').reduce((a,x)=>a+x.cnt,0);
      const warn = rows.filter(x=>x.level==='WARNING').reduce((a,x)=>a+x.cnt,0);
      const ep   = err/maxN*100, wp = warn/maxN*100;
      return `<div class="bar-row">
        <div class="bar-dt">${dt.slice(5)}</div>
        <div class="bar-track">
          <div class="bar-e" style="width:${ep}%"></div>
          <div class="bar-w" style="width:${wp}%"></div>
        </div>
        <div class="bar-n">${err+warn}</div>
      </div>`;
    }).join('');
  } catch(e) { console.error('stats',e); }
}

async function loadErrors() {
  const date  = document.getElementById('f-date').value;
  const level = document.getElementById('f-level').value;
  const q     = document.getElementById('f-q').value.trim();
  const p = new URLSearchParams({limit:200});
  if(date)  p.set('date',date);
  if(level) p.set('level',level);
  if(q)     p.set('q',q);
  try {
    const d = await fetch('/api/errors?'+p).then(r=>r.json());
    document.getElementById('meta').textContent =
      `총 ${d.total}건 중 ${Math.min(d.total,200)}건 표시`;
    const tbody = document.getElementById('tbody');
    if(!d.rows.length) {
      tbody.innerHTML='<tr><td colspan="4" class="empty">해당 조건의 에러 없음 ✅</td></tr>';
      return;
    }
    tbody.innerHTML = d.rows.map(r => {
      const modShort = r.module.split(':')[0].split('.').slice(-2).join('.');
      const msgShort = esc(r.message.length>90 ? r.message.slice(0,90)+'…' : r.message);
      return `<tr>
        <td class="ts-col">${r.ts.slice(5,16)}</td>
        <td>${badge(r.level)}</td>
        <td class="mod-col" title="${esc(r.module)}">${esc(modShort)}</td>
        <td class="msg-col" onclick="showModal(${r.id},'${esc(r.ts)}','${esc(r.module)}','${esc(r.level)}',${JSON.stringify(r.message)})">${msgShort}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('errors',e); }
}

function showModal(id, ts, module, level, msg) {
  document.getElementById('modal-title').textContent = `[${level}] ${ts}`;
  document.getElementById('modal-body').textContent = `모듈: ${module}\n\n${msg}`;
  document.getElementById('modal').classList.add('show');
}
function closeModal(e) {
  if(e.target.id==='modal') document.getElementById('modal').classList.remove('show');
}
function clearFilter() {
  document.getElementById('f-date').value = todayStr;
  document.getElementById('f-level').value = '';
  document.getElementById('f-q').value = '';
  loadErrors();
}

// 초기 로딩
loadStats();
loadErrors();
// 30초 자동 갱신
setInterval(()=>{ loadStats(); loadErrors(); }, 30000);
</script>
</body>
</html>
"""
