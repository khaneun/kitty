# monitor/ — 대시보드 모니터

> 루트 CLAUDE.md도 함께 로드됨.

## 구조 특이사항 — 단일 파일 아키텍처

`monitor/app.py` 하나에 FastAPI 백엔드 + HTML/CSS/JS 전체가 담겨 있음.

```python
# 파일 구조
[Python 코드: 1~830행쯤]
_HTML = r"""
  <!DOCTYPE html>
  <style>...</style>
  [HTML 본문]
  <script>...</script>
"""

@app.get("/", response_class=HTMLResponse)
def root(): return _HTML
```

**편집 시 주의**: `_HTML` raw string 안의 `{`, `}` 는 Python f-string 처리 안 됨 (raw string이므로 안전). JS 템플릿 리터럴 \`${...}\` 도 그대로 사용 가능.

## 탭 구조 & JS 상태 관리

```
GNB: [🐱 Kitty | 🌙 Night]  ← switchView('kitty'|'night')
Main tabs: 🤖 성적표 | 📒 매매일지 | ⚙️ 관리  ← switchMain()
Sub tabs (관리): 📋 에러 | 🔢 토큰 | 🧠 성향관리  ← switchAdmin()
```

**JS 전역 상태**:
```javascript
let _currentView = 'kitty';   // 현재 KR/Night 뷰
let _adminTab = 'errors';      // 현재 관리 서브탭
let _advFeedback = {};         // 성향관리 피드백 데이터
let _advHistory = [];          // 성향관리자 채팅 히스토리
let _advSuggestions = [];      // AI 제안 개선사항
```

## 새 탭/섹션 추가 패턴

### 서브탭 추가 (에러/토큰/성향관리 수준)
1. **서브탭 버튼** (HTML 서브탭 nav):
   ```html
   <div class="subtab" id="sub-tab-NEW" onclick="switchAdmin('new')">🔧 NEW</div>
   ```
2. **탭 컨텐츠** (HTML 본문):
   ```html
   <div id="tab-new" class="tab-content">...</div>
   ```
3. **switchMain() 업데이트** (JS):
   ```javascript
   ['errors','tokens','advisor','new','agents','trades'].forEach(n => ...)
   ```
4. **switchAdmin() 업데이트** (JS):
   ```javascript
   ['errors','tokens','advisor','new'].forEach(n => ...)
   if(name==='new'){ loadNew(); }
   ```
5. **API 엔드포인트** (Python):
   ```python
   @app.get("/api/new-data")
   def api_new(req: Request):
       _auth(req)
       ...
   ```

## 인증 패턴

```python
def _auth(req: Request):
    auth = req.headers.get("Authorization", "")
    # HTTP Basic Auth — 브라우저가 자동 처리
    # 401 반환 시 브라우저 로그인 다이얼로그 표시
```

JS `fetch()` 호출 시 별도 헤더 불필요 — 브라우저가 Basic Auth 자동 첨부.

## 볼륨 마운트 & 경로

```python
LOG_DIR        = Path("/logs")           # kitty-trader logs (ro)
FEEDBACK_DIR   = Path("/feedback")       # KR 피드백 (rw — 성향관리 탭 저장용)
TOKEN_DIR      = Path("/token_usage")    # KR 토큰 (ro)
NIGHT_LOG_DIR  = Path("/night-logs")     # night logs (ro)
NIGHT_FEEDBACK_DIR = Path("/night-feedback")  # Night 피드백 (rw)
REPORTS_DIR    = Path("/reports")        # KR 일일 리포트 (ro)
CMD_DIR        = Path("/commands")       # 채팅 요청/응답 (rw)
DB_PATH        = Path("/data/monitor.db")  # SQLite 에러 로그 DB
```

## API 엔드포인트 목록

| 엔드포인트 | 설명 |
|---|---|
| `GET /api/health` | 헬스 체크 |
| `GET /api/stats` | 에러 통계 |
| `GET /api/errors` | 에러 로그 조회 |
| `GET /api/portfolio` | KR 포트폴리오 스냅샷 |
| `GET /api/tendency` | KR 투자성향 현황 |
| `GET /api/agent-scores` | KR 에이전트 일별 점수 |
| `GET /api/token-usage` | KR 토큰 사용량 |
| `GET /api/agent-prompts` | 에이전트 기본 프롬프트 |
| `GET /api/agent-feedback` | 에이전트 개선 피드백 리스트 |
| `POST /api/agent-feedback/add` | 개선 피드백 저장 |
| `POST /api/tendency-advisor` | 성향관리자 AI 채팅 |
| `POST /api/chat` | 에이전트 채팅 (commands/ 경유) |
| `GET /api/chat/{req_id}` | 채팅 응답 폴링 |
| `POST /api/set-mode` | paper ↔ live 전환 |
| `GET /api/trades` | 매매일지 조회 |
| `GET /api/night/*` | Night 버전 동일 엔드포인트들 |

## CSS 클래스 레퍼런스 (자주 쓰는 것)

```css
.wrap          /* 페이지 최대 폭 컨테이너 */
.section       /* 카드형 섹션 박스 */
.sec-title     /* 섹션 제목 */
.cards         /* 요약 카드 3열 그리드 */
.card          /* 개별 카드 */
.num           /* 큰 숫자 표시 */
.empty         /* 데이터 없음 안내 */
.btn           /* 기본 버튼 */
.btn-pri       /* 파란 강조 버튼 */
.tbl-wrap      /* 테이블 수평 스크롤 래퍼 */
table.log      /* 에러/매매일지 테이블 */
table.pf       /* 포트폴리오 테이블 */
.gnb-select    /* GNB용 드롭다운 */
```

**색상 팔레트** (dark 테마):
- 배경: `#0d1117` (페이지), `#161b22` (카드), `#21262d` (입력)
- 텍스트: `#f0f6fc` (강조), `#c9d1d9` (본문), `#8b949e` (보조), `#484f58` (비활성)
- 포인트: `#58a6ff` (파랑), `#3fb950` (초록), `#f85149` (빨강), `#d29922` (노랑)
- 테두리: `#30363d` (외부), `#21262d` (내부)

## 성능 고려사항

- DB는 SQLite (`/data/monitor.db`) — 동시 쓰기 시 lock 주의
- `setInterval` 60초 자동 갱신 (성적표), 30초 (관리 탭)
- 에러 로그는 `RETAIN_DAYS=30` 이후 자동 정리
- 대형 에이전트 프롬프트 표시 시 `max-height + overflow-y:auto` 처리 필수

## 자주 하는 모니터 작업

```bash
# 모니터만 재배포 (app.py만 수정 시)
ssh -i ~/kitty-key.pem ec2-user@<IP> "
  cd ~/kitty && git pull
  docker stop kitty-monitor && docker rm kitty-monitor
  docker build -t kitty-monitor ./monitor
  # 환경변수 재추출 후 docker run ... (start.sh 참조)
"

# 로컬 개발 서버 (EC2 없이 테스트)
cd monitor
pip install fastapi uvicorn httpx
uvicorn app:app --reload --port 8080
```
