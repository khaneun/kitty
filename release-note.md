# Release Notes

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
