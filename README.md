# 🐱 Kitty — 한국주식 멀티 에이전트 자동 매매 시스템

> AI 에이전트 7개가 협력해 시장을 분석하고, 투자 성향에 맞게 자율적으로 매매합니다.
> 한국투자증권(KIS) Open API + Anthropic / OpenAI / Google Gemini 지원.

---

## 🏗️ 시스템 구조

```
┌─────────────────────────────────────────────────────┐
│                   kitty-trader                       │
│                                                     │
│  ① 투자성향관리자  →  성향 지침 생성 (AI 호출 없음)   │
│  ② 섹터분석가     →  거시 시장·섹터 분석              │
│  ③ 종목평가가     →  보유 종목 HOLD/SELL 판단         │
│  ④ 종목발굴가     →  신규 매수 후보 선정              │
│  ⑤ 자산운용가     →  최종 주문 리스트 결정 (핵심)     │
│  ⑥ 매수실행가     →  스마트 분할 매수 실행            │
│  ⑦ 매도실행가     →  스마트 분할 매도 실행            │
└──────────────┬──────────────────────────────────────┘
               │  commands/ (IPC)     logs/ (read-only)
┌──────────────▼──────────────────────────────────────┐
│                  kitty-monitor :8080                 │
│  🤖 성적표 (메인)  ⚙️ 관리(상태·에러·토큰)  💬 FAB채팅 │
└─────────────────────────────────────────────────────┘
               │
         Telegram Bot  ←→  사용자
```

---

## ⚙️ 매매 사이클 파이프라인

매매 사이클(기본 5분)마다 순서대로 실행됩니다.

```
08:50  사이클 시작 (장 시작 10분 전부터 분석)
  │
  ├─ [0] 🎯 투자성향관리자 (TendencyAgent)       ← AI 호출 없음, 즉시 완료
  │      성향 프로필(공격적/균형/보수적)에서 지침 문자열 생성
  │      → 종목평가가 · 종목발굴가 · 자산운용가 프롬프트에 주입
  │
  ├─ [1] 🔭 섹터분석가 (SectorAnalystAgent)
  │      뉴스 · 경제지표 · 글로벌 동향 기반 산업 섹터 거시 분석
  │      → 유망/위험 섹터 판단 + 섹터별 후보 종목 코드 제시
  │
  ├─ 📡 시세 조회 (후보 종목 + 보유 종목 전체)
  │
  ├─ [2] 📊 종목평가가 (StockEvaluatorAgent)
  │      보유 종목 손익 + 섹터 전망 종합 평가
  │      → HOLD / BUY_MORE / PARTIAL_SELL / SELL 신호
  │
  ├─ [3] 🔍 종목발굴가 (StockPickerAgent)
  │      섹터 분석 + 실제 시세 기반 신규 진입 후보 선정
  │      → 순수 종목 가치 평가 (잔고 고려 없음)
  │
  ├─ [4] 💼 자산운용가 (AssetManagerAgent)       ← 핵심 의사결정
  │      ┌────────────────────────────────────────────┐
  │      │ 종목평가 신호 + 발굴 후보 + 실제 가용 잔고 종합 │
  │      │ • 잔고 70%만 투입, 30% 현금 유보             │
  │      │ • 잔고 부족 → 약한 종목 매도 후 우량 종목 매수 │
  │      │   (Rotation)                               │
  │      │ • 종목당 최대 비중 20% 초과 금지              │
  │      │ → 최종 실행 주문 리스트 (SPLIT/SINGLE,       │
  │      │                         HIGH/NORMAL)       │
  │      └────────────────────────────────────────────┘
  │
09:00  주문 실행 시작 (장 개시부터)
  │
  ├─ [5] 🟢 매수실행가 (BuyExecutorAgent)
  │      • SPLIT: 수량 3등분 → 지정가 → 8초 대기 → 미체결 시 시장가 (최대 3회)
  │      • SINGLE: 지정가 → 시장가 폴백
  │      • 상한가 근접 종목 자동 스킵
  │
  ├─ [6] 🔴 매도실행가 (SellExecutorAgent)
  │      • HIGH priority (손절): 즉시 시장가, 분할 없음
  │      • NORMAL: SPLIT 분할 매도 or 지정가 → 시장가 폴백
  │      • 하한가 근접 시 즉시 시장가 강행
  │
15:35  📈 성과 평가 (장 마감 5분 후, 하루 1회)
       PerformanceEvaluator 실행
       → 에이전트별 정량 점수 계산 + AI 피드백 생성
       → feedback/*.json 저장 (다음 사이클부터 system_prompt에 자동 반영)
       → Telegram으로 평가 결과 전송
```

---

## ✨ 주요 기능

| 기능 | 설명 |
|------|------|
| 🤖 **멀티 에이전트** | 7개 전문 에이전트가 분업·협력, 역할별 독립 판단 |
| 🎯 **투자 성향** | 공격적/균형/보수적 프로필로 익절·손절·집중도 자동 조정 |
| 📡 **자율 종목 선정** | 고정 watchlist 없음. AI가 매 사이클 섹터 분석으로 후보 발굴 |
| 💰 **잔고 기반 배분** | 가용 잔고 70%만 투입, Rotation 전략 자동 실행 |
| ⚡ **스마트 주문** | 분할 주문 + 지정가 → 취소 → 시장가 전환 (최대 3회) |
| 🛑 **손절 즉시 실행** | HIGH priority 시장가 주문, 어떠한 지연도 없음 |
| 🔁 **에이전트 자기개선** | 장 마감 후 성과 평가 → 피드백 누적 → 다음날 system_prompt 자동 반영 |
| 🌐 **AI 백엔드 선택** | Anthropic Claude / OpenAI GPT-4o / Google Gemini 중 선택 |
| 💬 **Telegram 원격 제어** | 21개 명령어로 모니터링·제어·수동 매매·AWS 관리 |
| 📄 **모의/실전 분리** | 별도 앱키·계좌, `/setmode`로 런타임 전환 |
| ⏰ **AWS 자동 스케줄** | EventBridge로 EC2 장 시작 전 자동 켜기/끄기 |
| 📊 **모니터링 대시보드** | kitty-monitor (포트 8080) — 성적표 메인 + 관리 서브탭 + FAB 채팅 |
| 💡 **토큰 비용 추적** | AI 호출마다 자동 기록, 모델별 USD 비용 추산 |

---

## 📁 프로젝트 구조

```
kitty/
├── agents/
│   ├── base.py             # 멀티 AI 공통 기반 (피드백 로딩, 토큰 기록, chat())
│   ├── tendency.py         # 🎯 [0] 투자성향관리자 — 성향 프로필 지침 주입 (AI 호출 없음)
│   ├── sector_analyst.py   # 🔭 [1] 섹터분석가
│   ├── stock_evaluator.py  # 📊 [2] 종목평가가
│   ├── stock_picker.py     # 🔍 [3] 종목발굴가
│   ├── asset_manager.py    # 💼 [4] 자산운용가
│   ├── buy_executor.py     # 🟢 [5] 매수실행가
│   └── sell_executor.py    # 🔴 [6] 매도실행가
├── broker/
│   └── kis.py              # KIS API (시세·잔고·주문·취소·체결조회)
├── evaluator/
│   └── performance.py      # 장 마감 후 에이전트 성과 평가 엔진
├── feedback/
│   └── store.py            # 피드백 영속 저장소 (feedback/*.json)
├── telegram/
│   └── bot.py              # Telegram 봇 (21개 명령어)
├── utils/
│   ├── logger.py           # 로깅 설정
│   └── portfolio.py        # 포트폴리오·잔고 유틸 (portfolio_snapshot.json 저장)
├── config.py               # 환경 설정 (Pydantic)
├── main.py                 # 메인 루프 + 채팅 핸들러
└── report.py               # 일별 JSON 리포트
monitor/
├── app.py                  # FastAPI 모니터링 서버 + 대시보드 HTML
├── requirements.txt
└── Dockerfile
start.sh                    # EC2 부팅 스크립트 (git pull → Secrets → Docker 빌드)
docker-compose.yml          # 로컬 개발용
release-note.md             # 변경 이력
```

---

## 🚀 설치 및 실행

### 📋 요구사항

- Python 3.11+
- 한국투자증권 Open API 계정 → [apiportal.koreainvestment.com](https://apiportal.koreainvestment.com)
  - 실전투자 앱 + 모의투자 앱 각각 별도 등록 필요
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))
- AI API 키 (Anthropic / OpenAI / Google 중 1개)

### 🖥️ 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env
# .env 편집 후
python -m kitty.main
```

### 🐳 Docker 실행

```bash
# kitty-trader
docker build -t kitty-trader .
docker run -d \
  --name kitty-trader \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/feedback:/app/feedback \
  -v $(pwd)/token_usage:/app/token_usage \
  -v $(pwd)/commands:/app/commands \
  -v /var/run/docker.sock:/var/run/docker.sock \
  kitty-trader

# kitty-monitor (포트 8080)
docker build -t kitty-monitor ./monitor
docker run -d \
  --name kitty-monitor \
  --restart unless-stopped \
  -v $(pwd)/logs:/logs:ro \
  -v $(pwd)/feedback:/feedback:ro \
  -v $(pwd)/token_usage:/token_usage:ro \
  -v $(pwd)/commands:/commands \
  -v $(pwd)/monitor-data:/data \
  -e MONITOR_PASSWORD=kitty \
  -p 8080:8080 \
  kitty-monitor
```

또는 docker-compose로 두 서비스 동시 실행:

```bash
docker compose up -d
```

### 🔧 환경 설정 (.env)

```env
# AI 설정
AI_PROVIDER=openai          # anthropic | openai | gemini
AI_MODEL=gpt-4o             # 비우면 provider 기본값 사용

# API Keys (사용하는 provider만 입력)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=

# 실전투자 (TRADING_MODE=live)
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NUMBER=          # 10자리

# 모의투자 (TRADING_MODE=paper)
KIS_PAPER_APP_KEY=
KIS_PAPER_APP_SECRET=
KIS_PAPER_ACCOUNT_NUMBER=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# 매매 설정
TRADING_MODE=paper           # paper | live
MAX_BUY_AMOUNT=1000000       # 1회 최대 매수금액 (원)
MAX_POSITION_SIZE=5000000    # 종목당 최대 보유금액 (원)
```

---

## 📱 Telegram 명령어

설정된 `TELEGRAM_CHAT_ID` 외 사용자는 자동 차단됩니다.

### 📋 조회

| 명령어 | 설명 |
|--------|------|
| `/help` | 전체 명령어 목록 |
| `/status` | 모드·AI·가동시간·마지막 사이클 |
| `/portfolio` | 보유 종목별 수량·평균가·손익률 |
| `/balance` | 가용현금·총평가·평가손익 |
| `/analysis` | 최근 섹터 분석 결과 |
| `/evaluation` | 최근 종목 평가 결과 |
| `/report` | 오늘 매매 사이클 요약 |
| `/logs [n]` | 최근 로그 n줄 출력 (기본 50, 최대 200) |
| `/dashboard` | 모니터 대시보드 URL |

### 🕹️ 제어

| 명령어 | 설명 |
|--------|------|
| `/pause` | 매매 일시정지 |
| `/resume` | 매매 재개 |
| `/cycle` | 즉시 사이클 강제 실행 |
| `/stop` | 프로세스 종료 (재시작 정책으로 자동 복구) |

### ⚙️ 설정 / 수동 매매

| 명령어 | 설명 |
|--------|------|
| `/setbuy <금액>` | 런타임 최대 매수금액 변경 |
| `/setmode <paper\|live>` | 매매 모드 전환 (live 전환 시 confirm 2단계) |
| `/buy <종목코드> <수량>` | 수동 시장가 매수 |
| `/sell <종목코드> <수량>` | 수동 시장가 매도 |

### ☁️ AWS 원격 제어

| 명령어 | 설명 |
|--------|------|
| `/deploy` | git pull + Docker 재빌드 + 컨테이너 교체 |
| `/restart` | 컨테이너 재시작 (같은 이미지, 빠름) |
| `/shutdown` | 서비스 전체 중단 |
| `/startall` | 중단된 서비스 재시작 |

> `/deploy`, `/restart`, `/shutdown` 실행 시 봇 연결이 잠시 끊깁니다.

---

## ☁️ AWS 배포

### 인프라 구성

| 리소스 | 역할 |
|--------|------|
| 🖥️ EC2 (t3.small) | Docker 컨테이너 실행 호스트 |
| 🔐 Secrets Manager (`kitty/prod`) | API 키 등 민감 정보 저장 |
| 🔑 IAM Role (`kitty-ec2-role`) | EC2 → Secrets Manager 접근 권한 |
| ⏰ EventBridge Scheduler | EC2 자동 시작/중지 스케줄 |

### ⏰ EventBridge 스케줄 (KST)

| 동작 | 시각 | 요일 |
|------|------|------|
| EC2 시작 | 08:40 | 월~금 |
| EC2 중지 | 15:40 | 월~금 |

### 부팅 자동 실행 흐름

```
EC2 시작
  → systemd: kitty.service (start.sh)
      → git pull origin main          ← 최신 코드 자동 반영
      → AWS Secrets Manager 시크릿 로딩
      → Docker 빌드 (캐시 활용)
      → kitty-trader 컨테이너 시작
          → logs/ · feedback/ · token_usage/ · commands/ 볼륨 마운트
          → /var/run/docker.sock 마운트 (Telegram AWS 제어용)
      → kitty-monitor 컨테이너 시작 (포트 8080)
          → logs/ · feedback/ · token_usage/ 읽기 전용 마운트
          → commands/ 읽기-쓰기 마운트 (IPC 채널)
          → monitor-data/ (SQLite DB 영속 보관)
```

> `git push` 후 다음 영업일 EC2 부팅 시 자동으로 최신 코드가 반영됩니다.

### 🔐 Secrets Manager 키 목록 (`kitty/prod`)

```
ANTHROPIC_API_KEY · OPENAI_API_KEY
KIS_APP_KEY · KIS_APP_SECRET · KIS_ACCOUNT_NUMBER
KIS_PAPER_APP_KEY · KIS_PAPER_APP_SECRET · KIS_PAPER_ACCOUNT_NUMBER
TELEGRAM_BOT_TOKEN · TELEGRAM_CHAT_ID
MONITOR_PASSWORD
```

---

## 🎯 투자 성향 (TendencyAgent)

매 사이클 시작 시 AI 호출 없이 즉시 성향 지침을 생성하고, 판단 에이전트들의 프롬프트에 주입합니다.

| 성향 | 익절 기준 | 손절 기준 | 현금 유보 | 종목 최대 비중 |
|------|----------|----------|----------|--------------|
| 🔥 공격적 (기본) | +3% | -2% | 15% | 30% |
| ⚖️ 균형 | +15% | -5% | 30% | 20% |
| 🛡️ 보수적 | +10% | -3% | 50% | 15% |

성향은 `TendencyAgent.set_profile("aggressive" | "balanced" | "conservative")`로 변경합니다.
모니터 대시보드 성적표 탭 상단 성향 카드에서 현재 성향을 확인할 수 있습니다.

---

## 🤖 에이전트 자기개선 (피드백 루프)

매일 15:35 `PerformanceEvaluator`가 자동 실행됩니다.

### 평가 지표

| 에이전트 | 평가 기준 | 점수 |
|----------|-----------|------|
| 🔭 섹터분석가 | bullish/bearish 예측 vs 실제 섹터 등락 | 적중률 × 10 |
| 🔍 종목발굴가 | 추천 종목 당일 수익률 평균 | 구간별 2~9점 |
| 📊 종목평가가 | HOLD/BUY_MORE/SELL 판단 정확도 | 정확도 × 10 |
| 💼 자산운용가 | 최종 주문 방향성 (매수→상승, 매도→하락) | 구간별 2~9점 |
| 🟢 매수실행가 | 체결가 vs EOD (저가 매수 효율) | 구간별 3~9점, 체결 0건 시 1점 |
| 🔴 매도실행가 | 체결가 vs EOD (고가 매도 효율) | 구간별 3~9점, 체결 0건 시 1점 |

### 반영 방식

```
평가 완료
  → feedback/{에이전트명}.json 누적 저장 (최근 10일)
  → 각 에이전트 system_prompt 끝에 최근 5일 피드백 자동 주입
  → 다음 사이클부터 AI가 과거 실수를 참고해 판단 개선
```

---

## 🛡️ 리스크 관리

| 항목 | 기준 |
|------|------|
| 💵 사이클당 투입 한도 | 가용 잔고의 최대 70% |
| 📦 종목당 최대 비중 | 전체 자산의 20% (공격적 성향 시 30%) |
| 🔻 손절 | 평균매수가 대비 -5%, HIGH priority 즉시 시장가 |
| 🔺 익절 | 평균매수가 대비 +15%, PARTIAL_SELL 우선 |
| ⛔ 상한가 근접 | +29.5% 이상 종목 매수 금지 |
| ⛔ 하한가 근접 | -29.5% 이하 종목 즉시 시장가 매도 |
| ⚠️ 시장 리스크 HIGH | 신규 매수 전면 중단 |

---

## 📊 모니터링 대시보드 (kitty-monitor)

```
http://<EC2-IP>:8080        (HTTP Basic Auth)
```

Telegram `/dashboard` 명령으로 URL을 바로 받을 수 있습니다.

### 화면 구성

| 화면 | 내용 |
|------|------|
| 🤖 **성적표** (기본) | 투자 성향 카드 + 포트폴리오 현황 + 에이전트 점수 카드 + 7일 히트맵 |
| ⚙️ 관리 → 🏥 상태 | ok/warning/critical 배지, 1시간 에러 건수, 최근 에러 5건 |
| ⚙️ 관리 → 📋 에러 | 14일 추이 차트, 날짜·레벨·키워드 필터, 로그 전문 조회 |
| ⚙️ 관리 → 🔢 토큰 | 오늘 토큰·비용, 에이전트별 바 차트, 14일 일별 추이 |
| 💬 **FAB** (우하단) | 에이전트 선택 → 슬라이드업 팝업 채팅, 최근 판단 컨텍스트 기반 답변 |

### 파일 기반 IPC (`commands/` 공유 볼륨)

| 파일 | 방향 | 용도 |
|------|------|------|
| `mode_request.json` | monitor → kitty | paper/live 모드 전환 |
| `chat/req_{id}.json` | monitor → kitty | 에이전트 채팅 질문 |
| `chat/res_{id}.json` | kitty → monitor | 에이전트 채팅 답변 |

### 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `MONITOR_PASSWORD` | `kitty` | HTTP Basic Auth 비밀번호 |
| `TELEGRAM_BOT_TOKEN` | — | 버스트/CRITICAL 알림용 (선택) |
| `TELEGRAM_CHAT_ID` | — | 알림 수신 Chat ID (선택) |
| `POLL_SEC` | `15` | 로그 파일 폴링 간격 (초) |
| `RETAIN_DAYS` | `30` | SQLite 에러 보관 기간 (일) |

---

## 📁 데이터 파일

| 경로 | 내용 |
|------|------|
| `logs/kitty_YYYY-MM-DD.log` | 실행 로그 (30일 보관) |
| `logs/portfolio_snapshot.json` | 최신 포트폴리오 스냅샷 |
| `logs/agent_context.json` | 에이전트별 마지막 출력 (채팅 컨텍스트용) |
| `feedback/{에이전트명}.json` | 성과 피드백 누적 (최근 10일) |
| `token_usage/YYYY-MM-DD.json` | AI 토큰 사용량 일별 기록 |
| `reports/YYYY-MM-DD.json` | 일별 매매 사이클 전체 기록 |

---

## ⚠️ 주의사항

- **모의투자로 먼저 검증**하세요. `TRADING_MODE=paper`가 기본값입니다.
- 실전 전환은 `/setmode live` → `/setmode confirm` 2단계 확인 후 적용됩니다.
- `.env` 파일은 **절대 Git에 커밋하지 마세요**. AWS 배포 시 Secrets Manager를 사용하세요.
- AI 에이전트의 판단은 참고용이며, **투자 손익의 책임은 사용자에게 있습니다.**
