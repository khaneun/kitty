# Kitty - 한국투자증권 자동 매매 시스템

멀티 에이전트 AI 기반의 한국 주식 자동 매매 시스템입니다.
한국투자증권(KIS) Open API와 연동하여 AI 에이전트들이 협력해 시장을 분석하고 자율적으로 매매합니다.

---

## 에이전트 파이프라인

매매 사이클(기본 5분)마다 6개의 전문 에이전트가 순서대로 실행됩니다.

```
08:50  사이클 시작 (장 시작 10분 전부터 분석)
  │
  ├─ [1] 섹터분석가 (SectorAnalystAgent)
  │      뉴스·경제지표·글로벌 동향 기반 산업 섹터 거시 분석
  │      → 유망/위험 섹터 판단 + 섹터별 후보 종목 코드 제시
  │
  ├─ 시세 조회 (후보 종목 + 보유 종목 전체)
  │
  ├─ [2] 종목평가가 (StockEvaluatorAgent)
  │      보유 종목 손익 + 섹터 전망 종합 평가
  │      → HOLD / BUY_MORE / PARTIAL_SELL / SELL 신호
  │
  ├─ [3] 종목발굴가 (StockPickerAgent)
  │      섹터 분석 + 실제 시세 기반 신규 진입 후보 선정
  │      → 순수 종목 가치 평가 (잔고 고려 없음)
  │
  ├─ [4] 자산운용가 (AssetManagerAgent)        ← 핵심 의사결정
  │      ┌────────────────────────────────────────────┐
  │      │ 종목평가 신호 + 발굴 후보 + 실제 가용 잔고 종합 │
  │      │ • 잔고 70%만 투입, 30% 현금 유보             │
  │      │ • 잔고 부족 → 약한 종목 매도 후 우량 종목 매수 │
  │      │   (Rotation)                               │
  │      │ • 잔고 없음 → 교체 가능 종목 검토             │
  │      │ • 종목당 최대 비중 20% 초과 금지              │
  │      │ → 최종 실행 주문 리스트 (SPLIT/SINGLE,       │
  │      │                         HIGH/NORMAL)       │
  │      └────────────────────────────────────────────┘
  │
09:00  주문 실행 시작 (장 개시부터)
  │
  ├─ [5] 매수실행가 (BuyExecutorAgent)
  │      • SPLIT: 수량 3등분 → 지정가 시도 → 8초 대기
  │        → 미체결 시 취소 → 시장가 전환 (최대 3회)
  │      • SINGLE: 지정가 → 시장가 폴백
  │      • 상한가 근접 종목 자동 스킵
  │
  ├─ [6] 매도실행가 (SellExecutorAgent)
  │      • HIGH priority (손절): 즉시 시장가, 분할 없음
  │      • NORMAL: SPLIT 분할 매도 or 지정가 → 시장가 폴백
  │      • 하한가 근접 시 즉시 시장가 강행
  │
15:35  성과 평가 (장 마감 5분 후, 하루 1회)
       PerformanceEvaluator 실행
       → 에이전트별 정량 점수 계산 + AI 피드백 생성
       → feedback/*.json 저장 (다음 사이클부터 system_prompt에 반영)
       → Telegram으로 평가 결과 전송
```

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 자율 종목 선정 | 고정 watchlist 없음. AI가 매 사이클 섹터 분석으로 후보 발굴 |
| 잔고 기반 자산 배분 | 가용 잔고 70%만 투입, Rotation 전략 자동 실행 |
| 스마트 주문 실행 | 분할 주문 + 지정가 → 취소 → 시장가 전환 (최대 3회) |
| 손절 즉시 실행 | HIGH priority 시장가 주문, 어떠한 지연도 없음 |
| AI 백엔드 선택 | Anthropic Claude / OpenAI GPT-4o / Google Gemini |
| Telegram 원격 제어 | 20개 명령어로 모니터링·제어·수동 매매·AWS 관리 |
| 모의/실전 분리 | 별도 앱키·계좌 사용, Telegram `/setmode`로 런타임 전환 |
| 장 외 분석 | 08:50~09:00 분석 실행, 주문은 09:00 이후만 허용 |
| 에이전트 자기개선 | 장 마감 후 성과 평가 → 피드백 누적 → system_prompt 자동 반영 |
| AWS 자동 스케줄 | EventBridge로 EC2 장 시작 전 자동 켜기/끄기 |

---

## 프로젝트 구조

```
kitty/
├── agents/
│   ├── base.py                # 멀티 AI 공통 기반 (피드백 자동 로딩 포함)
│   ├── sector_analyst.py      # [1] 섹터분석가
│   ├── stock_evaluator.py     # [2] 종목평가가
│   ├── stock_picker.py        # [3] 종목발굴가
│   ├── asset_manager.py       # [4] 자산운용가
│   ├── buy_executor.py        # [5] 매수실행가 (스마트 주문, AI 응답 float → int 보장)
│   └── sell_executor.py       # [6] 매도실행가 (스마트 주문, AI 응답 float → int 보장)
├── broker/
│   └── kis.py                 # KIS API (시세·잔고·주문·취소·체결조회)
├── evaluator/
│   └── performance.py         # 장 마감 후 에이전트 성과 평가 엔진
│                              #   체결 0건 시 1점 기록 (평가 누락 방지)
├── feedback/
│   └── store.py               # 피드백 영속 저장소 (feedback/*.json)
├── telegram/
│   └── bot.py                 # Telegram 봇 (20개 명령어)
├── utils/
│   ├── logger.py              # 로깅 설정
│   └── portfolio.py           # 포트폴리오·잔고 출력 유틸
├── config.py                  # 환경 설정 (Pydantic)
├── main.py                    # 메인 루프
└── report.py                  # 일별 JSON 리포트
start.sh                       # EC2 부팅 스크립트 (git pull → Secrets 로딩 → Docker 빌드)
release-note.md                # 변경 이력
```

---

## 설치 및 실행

### 요구사항

- Python 3.11+
- 한국투자증권 Open API 계정 → [apiportal.koreainvestment.com](https://apiportal.koreainvestment.com)
  - 실전투자 앱 + 모의투자 앱 각각 별도 등록 필요
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))
- AI API 키 (Anthropic / OpenAI / Google 중 1개)

### 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env
# .env 편집 후
python -m kitty.main
```

### Docker 실행

```bash
docker build -t kitty-trader .
docker run -d \
  --name kitty-trader \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/feedback:/app/feedback \
  kitty-trader
```

### 환경 설정 (.env)

```env
# AI 설정
AI_PROVIDER=openai          # anthropic | openai | gemini
AI_MODEL=gpt-4o             # 비우면 provider 기본값 사용

# API Keys
ANTHROPIC_API_KEY=
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=

# 실전투자 (TRADING_MODE=live)
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NUMBER=          # 10자리 (앞8자리+뒤2자리)

# 모의투자 (TRADING_MODE=paper) — apiportal에서 별도 앱 등록
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

## Telegram 명령어

설정된 `TELEGRAM_CHAT_ID` 외 사용자는 자동 차단됩니다.

### 조회

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

### 제어

| 명령어 | 설명 |
|--------|------|
| `/pause` | 매매 일시정지 |
| `/resume` | 매매 재개 |
| `/cycle` | 즉시 사이클 강제 실행 |
| `/stop` | 프로세스 종료 (컨테이너 재시작 정책으로 자동 복구) |

### 설정 / 수동 매매

| 명령어 | 설명 |
|--------|------|
| `/setbuy <금액>` | 런타임 최대 매수금액 변경 |
| `/setmode <paper\|live>` | 매매 모드 전환 (live 전환 시 confirm 단계 추가) |
| `/buy <종목코드> <수량>` | 수동 시장가 매수 |
| `/sell <종목코드> <수량>` | 수동 시장가 매도 |

### AWS 원격 제어

| 명령어 | 설명 |
|--------|------|
| `/deploy` | git pull + Docker 재빌드 + 컨테이너 교체 |
| `/restart` | 컨테이너 재시작 (같은 이미지, 빠름) |
| `/shutdown` | 컨테이너 전체 중단 |
| `/startall` | 중단된 컨테이너 재시작 |

> `/deploy`, `/restart`, `/shutdown` 실행 시 컨테이너가 교체·중단되어 봇 연결이 잠시 끊깁니다.

---

## AWS 배포

### 구성 요소

| 리소스 | 역할 |
|--------|------|
| EC2 (t3.small) | Docker 컨테이너 실행 호스트 |
| AWS Secrets Manager (`kitty/prod`) | API 키 등 민감 정보 저장 |
| IAM Role (`kitty-ec2-role`) | EC2 → Secrets Manager 접근 권한 |
| EventBridge Scheduler | EC2 자동 시작/중지 스케줄 |

### EventBridge 스케줄 (KST 기준)

| 스케줄 | 시각 | 요일 |
|--------|------|------|
| EC2 시작 | 08:40 | 월~금 |
| EC2 중지 | 15:40 | 월~금 |

### EC2 부팅 시 자동 실행 흐름

```
EC2 시작
  → systemd: docker.service
  → systemd: kitty.service (start.sh)
      → git pull origin main          ← GitHub 최신 코드 자동 반영
      → AWS Secrets Manager에서 시크릿 로딩
      → Docker 빌드 (캐시 활용)
      → kitty-trader 컨테이너 시작
          → logs/, feedback/ 볼륨 마운트
          → /var/run/docker.sock 마운트 (Telegram AWS 제어용)
```

> 코드 수정 후 `git push`만 하면 다음 영업일 EC2 부팅 시 자동으로 반영됩니다.

### Secrets Manager에 저장되는 값

```
kitty/prod
├── ANTHROPIC_API_KEY
├── OPENAI_API_KEY
├── KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NUMBER
├── KIS_PAPER_APP_KEY / KIS_PAPER_APP_SECRET / KIS_PAPER_ACCOUNT_NUMBER
├── TELEGRAM_BOT_TOKEN
└── TELEGRAM_CHAT_ID
```

---

## 에이전트 자기개선 (피드백 루프)

매일 장 마감 직후 `PerformanceEvaluator`가 자동 실행됩니다.

### 평가 지표

| 에이전트 | 평가 기준 | 점수 산정 |
|----------|-----------|-----------|
| 섹터분석가 | 섹터 방향 예측 적중률 (bullish/bearish vs 실제 등락) | 적중률 × 10 |
| 종목발굴가 | 추천 종목의 당일 수익률 평균 | 수익률 구간별 2~9점 |
| 종목평가가 | HOLD/BUY_MORE/SELL 판단 정확도 | 정확도 × 10 |
| 자산운용가 | 최종 주문 방향성 점수 (매수→상승, 매도→하락) | 방향성 평균 구간별 2~9점 |
| 매수실행가 | 체결가 vs EOD 가격 (저가 매수 효율) | 효율 구간별 3~9점, 체결 0건 시 1점 |
| 매도실행가 | 체결가 vs EOD 가격 (고가 매도 효율) | 효율 구간별 3~9점, 체결 0건 시 1점 |

### 피드백 반영 방식

```
평가 완료
  → feedback/{에이전트명}.json 에 날짜별 누적 저장 (최근 10일 보관)
  → 각 에이전트 system_prompt 끝에 최근 5일 피드백 자동 주입
  → 다음 사이클부터 AI가 과거 실수를 참고해 판단

[system_prompt 주입 예시]
[📊 과거 성과 피드백 - 아래 내용을 참고해 판단을 개선하세요]
• 2026-04-01 (점수 7/10): 반도체 강세 예측 적중, 바이오 방향 실패
• 2026-03-31 (점수 5/10): 4개 섹터 중 2개 방향 예측 실패
[개선 포인트] 글로벌 불확실성 신호 시 더 보수적인 전망 필요
```

---

## 리스크 관리

| 항목 | 기준 |
|------|------|
| 사이클당 투입 한도 | 가용 잔고의 최대 70% |
| 종목당 최대 비중 | 전체 자산의 20% |
| 손절 | 평균매수가 대비 -5%, HIGH priority 즉시 시장가 |
| 익절 | 평균매수가 대비 +15%, PARTIAL_SELL 우선 |
| 상한가 근접 | +29.5% 이상 종목 매수 금지 |
| 하한가 근접 | -29.5% 이하 종목 즉시 시장가 매도 |
| 시장 리스크 HIGH | 신규 매수 중단 |

---

## 스마트 주문 실행 흐름

```
매수 / 매도 주문
    │
    ├─ priority=HIGH? (손절 등)
    │   └─ 즉시 시장가 → 완료
    │
    ├─ order_type=SPLIT? (수량>5 or 명시)
    │   └─ 수량 3등분
    │       └─ 각 청크:
    │           ①  지정가 주문 (현재가)
    │           ②  8초 대기 → 체결 확인
    │           ③  미체결 → 취소 → 2초 대기
    │           ④  시장가 재시도 (최대 3회)
    │
    └─ order_type=SINGLE
        └─ 지정가 → 8초 → 미체결 → 시장가 폴백
```

---

## 로그 및 리포트

**로그**: `logs/kitty_YYYY-MM-DD.log` (30일 보관)

**일별 리포트**: `reports/YYYY-MM-DD.json`

```json
{
  "date": "2026-03-26",
  "total_cycles": 12,
  "cycles": [
    {
      "timestamp": "09:00:05",
      "market_analysis": { "market_sentiment": "bullish", "sectors": [...] },
      "stock_evaluation": { "evaluations": [...] },
      "stock_picks": { "decisions": [...] },
      "asset_management": { "final_orders": [...], "cash_reserve_ratio": 0.3 },
      "buy_results": [...],
      "sell_results": [...]
    }
  ]
}
```

**에이전트 피드백**: `feedback/{에이전트명}.json` (최근 10일, Docker 볼륨으로 영속 보관)

---

## 주의사항

- **모의투자로 먼저 검증**하세요. `TRADING_MODE=paper`가 기본값입니다.
- 실전 전환은 Telegram `/setmode live` → `/setmode confirm` 2단계 확인 후 적용됩니다.
- `.env` 파일에는 API 키가 포함되므로 **절대 Git에 커밋하지 마세요**. AWS 배포 시 Secrets Manager를 사용하세요.
- AI 에이전트의 판단은 참고용이며, 투자 손익의 책임은 사용자에게 있습니다.
