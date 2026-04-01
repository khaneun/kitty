# Release Notes

---

## v1.3.0 — 2026-04-01

### 신규 기능

**투자 성향 에이전트 (TendencyAgent)** (`kitty/agents/tendency.py`)

7번째 에이전트. 다른 에이전트의 경계선 판단에 성향 지침을 주입해 의사결정에 성향을 반영함.
AI 호출 없이 결정론적으로 지침 문자열을 생성하므로 사이클 속도에 영향 없음.

- 3가지 성향 프로필 내장: `aggressive` / `balanced` / `conservative`
- 초기 성향: **공격적 (aggressive)** — 익절 기준 +3%, 손절 기준 -2%, 현금 비중 최소 15%, 종목 최대 30%
- `get_directive()` → 현재 성향 지침 문자열 반환 (결정론적, AI 호출 없음)
- `set_profile(name)` → 런타임 성향 전환
- `chat()` 메서드로 모니터 채팅에서 직접 질문 가능
- 종목평가가·종목발굴가·자산운용가 프롬프트에 `tendency_directive` 자동 주입

**모니터 UX 전면 개편** (`monitor/app.py`)

| 변경 전 | 변경 후 |
|---------|---------|
| 5탭 동등 구조 (상태·에러·성적표·토큰·채팅) | 성적표 메인 + 관리 서브탭 구조 |
| 성적표가 3번째 탭 | 성적표가 기본 진입 화면 |
| 채팅이 별도 탭 | 우하단 FAB(💬) → 슬라이드업 팝업 |

- **성적표(🤖)**: 앱 진입 시 바로 표시되는 메인 화면
- **관리(⚙️)**: 클릭 시 서브탭 노출 → 🏥 상태 / 📋 에러 / 🔢 토큰
- **💬 FAB**: 화면 우하단에 항상 표시. 클릭 시 슬라이드업 채팅 팝업. 배경 터치 또는 ✕로 닫기

**성적표 상단 성향 카드** (`monitor/app.py`)

성적표 탭 최상단에 현재 투자 성향 요약 카드 표시.
성향 배지(공격적=주황 / 균형=파랑 / 보수적=초록) + 설명 + 익절·손절·현금·최대비중 파라미터 한눈에 표시.

- `GET /api/tendency` 엔드포인트 추가 — `logs/agent_context.json`에서 성향 정보 반환

**Telegram `/dashboard` 명령어** (`kitty/telegram/bot.py`, `kitty/config.py`)

텔레그램에서 `/dashboard` 입력 시 모니터 대시보드 URL을 바로 반환.

- `MONITOR_HOST` 환경변수로 호스트 지정 가능. 미설정 시 EC2 인스턴스 메타데이터 서비스(`169.254.169.254`)에서 퍼블릭 IP 자동 조회
- `MONITOR_PORT` 환경변수로 포트 지정 (기본값 8080)
- `kitty/config.py`에 `monitor_host`, `monitor_port` 필드 추가

### 버그 수정

**"연결 중..." 고정 문제** (`monitor/app.py`)

성적표를 기본 뷰로 변경하면서 `loadHealth()`가 초기에 호출되지 않아 헤더의 갱신 시각이 "연결 중..."에서 변경되지 않던 문제 수정.
`loadPortfolio()` 응답의 `ts`로 갱신 시각을 업데이트하도록 변경.

**투자성향관리자 "데이터 없음" 노출** (`monitor/app.py`)

`AGENTS` 목록에 `투자성향관리자`가 포함되어 에이전트 점수 카드에 "데이터 없음"으로 표시되던 문제 수정.
성향 정보는 별도 성향 카드로만 표시하며, 성과 평가 대상에서 제외.

---

## v1.2.0 — 2026-04-01

### 신규 기능

**에이전트 채팅 탭 💬** (`monitor/app.py`, `kitty/agents/base.py`, `kitty/main.py`)

모니터 대시보드에 5번째 탭 추가. 각 에이전트에게 자유롭게 질문할 수 있음.
에이전트 선택 드롭다운, 채팅 히스토리, 입력창(Enter 전송 / Shift+Enter 줄바꿈)으로 구성.

- `BaseAgent.chat(message, context)` 메서드 추가 — trading `_conversation`을 오염시키지 않는 one-shot AI 호출 (Anthropic / OpenAI / Gemini 모두 지원)
- `kitty/main.py`: `_save_agent_context()` — 각 에이전트 실행 후 마지막 출력을 `logs/agent_context.json`에 저장
- `kitty/main.py`: `_chat_handler(agents_map)` — 백그라운드 태스크, `commands/chat/req_*.json` 2초 폴링 → 해당 에이전트 컨텍스트 로딩 → `chat()` 호출 → `res_{id}.json` 기록
- `monitor/app.py`: `POST /api/chat` — 질문 파일 생성 후 id 반환
- `monitor/app.py`: `GET /api/chat/{id}` — 응답 파일 폴링 (최대 60초 대기)

**포트폴리오 현황 표시** (`kitty/utils/portfolio.py`, `monitor/app.py`)

성적표 탭 상단에 현재 주식 포트폴리오 실시간 표시.
총평가금액·평가손익·주문가능현금 카드 3개 + 보유 종목 테이블 (종목명/코드, 수량, 평균단가, 현재가, 수익률, 평가금액).

- `kitty/utils/portfolio.py`: 포트폴리오 출력 시 `logs/portfolio_snapshot.json` 자동 저장 (ts, trading_mode, available_cash, total_eval, total_pnl, holdings)
- `monitor/app.py`: `GET /api/portfolio` — snapshot 반환
- `monitor/app.py`: `loadPortfolio()` JS 함수 — GNB 모드 셀렉터와 자동 동기화

**GNB 모드 셀렉터** (`monitor/app.py`)

모니터 헤더에 paper/live 전환 콤보박스 추가.
live 전환 시 확인 다이얼로그 필수, 전환 요청은 `commands/mode_request.json`을 통해 kitty-trader에 전달.
포트폴리오 로딩 시 현재 모드가 셀렉터에 자동 반영됨.

**파일 기반 IPC 채널 추가** (`start.sh`, `docker-compose.yml`)

kitty-trader ↔ kitty-monitor 간 양방향 통신을 위한 `commands/` 공유 볼륨 추가.

- kitty-trader: `commands:/app/commands` (읽기-쓰기)
- kitty-monitor: `commands:/commands` (읽기-쓰기)

### 버그 수정

**Telegram `/deploy` 볼륨 누락** (`kitty/telegram/bot.py`)

`/deploy` 명령으로 컨테이너를 재시작할 때 `feedback`·`token_usage` 볼륨 마운트가 빠져 있어 재시작 후 피드백·토큰 데이터가 초기화되던 문제 수정.

**kitty 시작 실패 루프** (`kitty/main.py`)

EC2 부팅 직후 네트워크 미준비 상태에서 `reporter.start_polling()` 호출이 예외를 던지면 프로세스가 죽고 재시작을 반복하던 문제 수정.
- `start_polling()` 최대 5회 재시도, 실패마다 10×n 초 대기
- 시작 시 `print_portfolio_and_balance()` 호출을 try/except로 감싸 KIS API 일시 오류로 인한 크래시 방지

---

## v1.1.0 — 2026-04-01

### 신규 기능

**토큰 사용량 자동 추적** (`kitty/agents/base.py`)

모든 AI 호출(Anthropic / OpenAI / Gemini) 완료 후 `token_usage/YYYY-MM-DD.json`에
에이전트명·모델·입력/출력 토큰 수를 자동 기록.
파일은 날짜별 JSON 배열로 누적되며 Docker 볼륨으로 영속 보관.

**4탭 모바일 대시보드** (`monitor/app.py`)

기존 2탭 에러/성적표 구조를 아래 4탭으로 전면 개편.

| 탭 | 내용 |
|----|------|
| 🏥 상태 | ok/warning/critical 배지, 오늘 에러·경고·1시간 에러 건수, 마지막 로그 시각, 최근 에러 5건 |
| 📋 에러 | 14일 추이 바 차트, 날짜·레벨·키워드 필터, 로그 테이블 (클릭 시 전문) |
| 🤖 성적표 | 에이전트별 최신 점수 카드 + 미니 바, 7일 히트맵 (셀 클릭 시 요약·개선포인트) |
| 🔢 토큰 | 오늘 입력/출력 토큰·비용, 14일 누적 비용, 에이전트별 바 차트, 14일 일별 추이 |

**신규 API 엔드포인트** (`monitor/app.py`)

- `GET /api/health` — 실시간 상태 (status / err_today / warn_today / err_1h / last_log_ts / 최근 에러 5건)
- `GET /api/token-usage` — 14일 일별·에이전트별 토큰 집계, 모델별 USD 비용 추산

**모델별 비용 추산 지원**

gpt-4o / gpt-4o-mini / gpt-4-turbo / claude-opus-4-6 / claude-sonnet-4-6 /
claude-haiku-4-5 / gemini-1.5-pro / gemini-1.5-flash / gemini-2.0-flash

**token_usage 볼륨 마운트** (`start.sh`, `docker-compose.yml`)

- kitty-trader: `token_usage:/app/token_usage` (쓰기)
- kitty-monitor: `token_usage:/token_usage:ro` (읽기)

---

## v1.0.0 — 2026-04-01

### 버그 수정

**주문 수량·가격 float → int 변환** (`buy_executor.py`, `sell_executor.py`)

AI(자산운용가)가 반환하는 JSON의 `quantity`, `price` 값이 float으로 전달될 경우
`str(10.0)` → `"10.0"` 형태로 KIS API에 전달되어 주문 거부 오류가 발생하던 문제 수정.
`int()` 캐스팅을 추가해 정수형으로 보장.

**매도 지정가 계산 반올림 처리** (`sell_executor.py`)

SINGLE 매도 시 현재가에 0.2% 가산하는 계산(`current_price * 1.002`)에서
`int()`(버림)를 `round()`(반올림)으로 변경해 호가 단위 정합성 개선.

**매수/매도 실행가 평가 누락 수정** (`evaluator/performance.py`)

주문을 시도했으나 전부 체결 실패(`FAILED`)인 경우, 기존 로직은 평가를 건너뛰어
피드백이 전혀 저장되지 않던 문제 수정.
이제 시도한 주문이 있으나 체결 건수가 0이면 **1점**으로 기록되어
에이전트 자기개선 피드백 루프에 반영됨.

**섹터분석가 보유 종목 파싱 오류 수정** (`sector_analyst.py`)

KIS API가 `pchs_avg_pric`(평균 매수가)를 `'823666.6660'`처럼 소수 문자열로 반환하는데
`int()` 직접 변환 시 `ValueError`가 발생해 보유 종목이 있을 때마다 매 사이클 크래시.
`int(float(...))` 로 수정.

### 개선

**EC2 부팅 시 최신 코드 자동 반영** (`start.sh`)

기존 `start.sh`는 `git pull` 없이 로컬 파일로 Docker 빌드를 수행해,
코드 수정 후 `git push`만으로는 다음 날 자동 부팅에 반영되지 않는 문제가 있었음.
`start.sh` 첫 단계에 `git pull origin main`을 추가하고 파일을 repo에 포함.
이제 `git push` 후 다음 EC2 부팅 시 자동으로 최신 코드가 반영됨.

**kitty-monitor 서비스 추가** (`monitor/`, `start.sh`, `docker-compose.yml`)

EC2 포트 8080에 독립 모니터링 서비스 배포.
HTTP Basic Auth, Telegram 버스트/CRITICAL 즉시 알림,
SQLite 30일 로그 보관, 파일 위치 추적으로 재시작 시 중복 수집 방지.
