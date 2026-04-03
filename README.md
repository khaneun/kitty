# 🐱 Kitty — AI 멀티 에이전트 자동 매매 시스템

> 7개 AI 에이전트가 협력해 시장을 분석하고 자율적으로 매매합니다.
> 한국 주식(KIS API) + 미국 주식(KIS 해외) 동시 운영. Anthropic / OpenAI / Gemini 지원.

---

## 시스템 구조

```
┌─────────────────────────────────────────────────────┐
│  kitty-trader (한국주식 09:00~15:30 KST)             │
│  kitty-night-trader (미국주식 21:00~06:00 KST)       │
│                                                     │
│  ① 투자성향관리자  →  5차원×6단계 성향 지침 생성       │
│  ② 섹터분석가     →  거시 시장·섹터 분석              │
│  ③ 종목평가가     →  보유 종목 HOLD/SELL 판단         │
│  ④ 종목발굴가     →  신규 매수 후보 선정              │
│  ⑤ 자산운용가     →  최종 주문 리스트 결정            │
│  ⑥ 매수실행가     →  스마트 분할 매수                 │
│  ⑦ 매도실행가     →  스마트 분할 매도                 │
└──────────────┬──────────────────────────────────────┘
               │  commands/ (IPC)     logs/ (read-only)
┌──────────────▼──────────────────────────────────────┐
│              kitty-monitor :8080                     │
│  🐱 Kitty ↔ 🌙 Night 뷰 전환 | 성적표·관리·채팅     │
└─────────────────────────────────────────────────────┘
               │
         Telegram Bot  ←→  사용자
```

---

## 운영 시간표

| 서비스 | 시간 (KST) | 사이클 | 비고 |
|--------|-----------|--------|------|
| kitty-trader | 08:50~15:30 | 5분 | 한국 정규장 |
| kitty-night-trader | 21:00~06:00 | 15분 | 미국 NYSE/NASDAQ, DST 자동 대응 |
| kitty-monitor | 24/7 | — | 대시보드 항시 접근 |

EC2는 24/7 상시 가동. 3개 컨테이너 모두 `restart: unless-stopped`.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 🤖 멀티 에이전트 | 7개 전문 에이전트 분업·협력 (한국/미국 각각 독립 에이전트 세트) |
| 🎯 투자 성향 | 5차원×6단계 레벨 자동 조정 (장 마감 후 AI 갱신) |
| 📡 자율 종목 선정 | 고정 watchlist 없음. 실시간 시장 데이터 기반 |
| ⚡ 스마트 주문 | 분할 주문 + 지정가→시장가 전환 (한국 5주/미국 10주 기준) |
| 🔁 자기개선 | 에이전트별 점수(0~100) + AI 피드백 → 다음 사이클 반영 |
| 🌐 AI 백엔드 | Anthropic Claude / OpenAI GPT / Google Gemini |
| 💬 Telegram | 21개 명령어 원격 제어 + 모니터링 |
| 📊 대시보드 | kitty-monitor — 🐱/🌙 뷰 전환, 성적표, 에러, 토큰, 채팅 |

---

## 프로젝트 구조

```
kitty/                    # 한국주식 트레이더
├── agents/               # 7개 에이전트 (tendency~sell_executor)
├── broker/kis.py         # KIS 국내 API
├── evaluator/            # 성과 평가 엔진
├── feedback/             # 피드백 저장소
├── telegram/bot.py       # Telegram 봇 (21개 명령어)
├── config.py             # Pydantic 설정
├── main.py               # 메인 루프 (5분 사이클)
└── report.py             # 일별 리포트

kitty_night/              # 미국주식 트레이더 (완전 독립)
├── agents/               # 7개 Night 에이전트
├── broker/kis_overseas.py # KIS 해외 API
├── evaluator/            # Night 성과 평가
├── telegram/bot.py       # Night Telegram (메시지 전용)
├── config.py             # NIGHT_ prefix 환경변수
├── main.py               # MarketPhase 기반 루프 (15분 사이클)
├── report.py             # Night 일별 리포트
└── market_calendar.py    # NYSE 캘린더 + DST 처리

monitor/
├── app.py                # FastAPI 대시보드 (🐱/🌙 뷰 전환)
└── Dockerfile

start.sh                  # EC2 부팅 스크립트
docker-compose.yml        # 3개 서비스 (kitty + night + monitor)
Dockerfile                # kitty-trader
Dockerfile.night          # kitty-night-trader
```

---

## 빠른 시작

### 요구사항

- Python 3.11+
- 한국투자증권 Open API 계정 (국내 + 해외)
- Telegram Bot Token
- AI API 키 (Anthropic / OpenAI / Google 중 1개)

### Docker 실행

```bash
# 환경변수 설정
cp .env.example .env          # 한국주식
cp .env.night.example .env.night  # 미국주식

# 전체 실행
docker compose up -d
```

### AWS 배포

```bash
# 로컬에서 원클릭 배포
EC2_IP=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=kitty-trader" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
ssh -i ~/kitty-key.pem ec2-user@$EC2_IP \
  "cd /home/ec2-user/kitty && git pull origin main && bash start.sh"
```

> 상세 배포 절차는 [deployments.md](deployments.md) 참조.

---

## 환경 설정

### 한국주식 (.env)

```env
AI_PROVIDER=openai
AI_MODEL=gpt-4o
TRADING_MODE=paper
KIS_APP_KEY=              # 실전
KIS_PAPER_APP_KEY=        # 모의
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
MAX_BUY_AMOUNT=1000000    # 원
MAX_POSITION_SIZE=5000000  # 원
```

### 미국주식 (.env.night)

```env
NIGHT_AI_PROVIDER=openai
NIGHT_AI_MODEL=gpt-4o
NIGHT_TRADING_MODE=paper
NIGHT_KIS_APP_KEY=         # 해외 실전
NIGHT_KIS_PAPER_APP_KEY=   # 해외 모의
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

> 전체 키 목록은 `.env.example` / `.env.night.example` 참조.

---

## Telegram 명령어

| 분류 | 명령어 | 설명 |
|------|--------|------|
| 조회 | `/status` `/portfolio` `/balance` `/analysis` `/report` `/dashboard` | 상태·잔고·분석·리포트 |
| 제어 | `/pause` `/resume` `/cycle` `/stop` | 일시정지·재개·즉시실행 |
| 설정 | `/setmode <paper\|live>` `/setbuy <금액>` | 모드·한도 전환 |
| 수동 | `/buy <코드> <수량>` `/sell <코드> <수량>` | 수동 매매 |
| AWS | `/deploy` `/restart` `/shutdown` `/startall` | 원격 배포·제어 |

---

## ⚠️ 주의사항

- **모의투자(`paper`)로 먼저 검증**하세요. 기본값은 `paper`입니다.
- `.env` / `.env.night` 파일은 **절대 Git에 커밋하지 마세요**.
- AI 에이전트의 판단은 참고용이며, **투자 손익의 책임은 사용자에게 있습니다.**
