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
  └─ [6] 매도실행가 (SellExecutorAgent)
         • HIGH priority (손절): 즉시 시장가, 분할 없음
         • NORMAL: SPLIT 분할 매도 or 지정가 → 시장가 폴백
         • 하한가 근접 시 즉시 시장가 강행
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
| Telegram 원격 제어 | 14개 명령어로 모니터링·제어·수동 매매 |
| 모의/실전 분리 | 별도 앱키·계좌 사용, 코드 변경 없이 모드 전환 |
| 장 외 분석 | 08:50~09:00 분석 실행, 주문은 09:00 이후만 허용 |

---

## 프로젝트 구조

```
kitty/
├── agents/
│   ├── base.py                # 멀티 AI 공통 기반 (Anthropic/OpenAI/Gemini)
│   ├── sector_analyst.py      # [1] 섹터분석가
│   ├── stock_evaluator.py     # [2] 종목평가가
│   ├── stock_picker.py        # [3] 종목발굴가
│   ├── asset_manager.py       # [4] 자산운용가
│   ├── buy_executor.py        # [5] 매수실행가 (스마트 주문)
│   └── sell_executor.py       # [6] 매도실행가 (스마트 주문)
├── broker/
│   └── kis.py                 # KIS API (시세·잔고·주문·취소·체결조회)
├── telegram/
│   └── bot.py                 # Telegram 봇 (14개 명령어)
├── utils/
│   ├── logger.py              # 로깅 설정
│   └── portfolio.py           # 포트폴리오·잔고 출력 유틸
├── config.py                  # 환경 설정 (Pydantic)
├── main.py                    # 메인 루프
└── report.py                  # 일별 JSON 리포트
```

---

## 설치 및 실행

### 요구사항

- Python 3.11+
- 한국투자증권 Open API 계정 → [apiportal.koreainvestment.com](https://apiportal.koreainvestment.com)
  - 실전투자 앱 + 모의투자 앱 각각 별도 등록 필요
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))
- AI API 키 (Anthropic / OpenAI / Google 중 1개)

### 설치

```bash
# 의존성 설치
pip install -r requirements.txt
pip install -e .
```

### 환경 설정

```bash
cp .env.example .env
```

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

### 실행

```bash
# 가상환경 활성화 후
source .venv/bin/activate
python -m kitty.main

# 또는 venv 직접 지정
.venv/bin/python -m kitty.main
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

### 제어

| 명령어 | 설명 |
|--------|------|
| `/pause` | 매매 일시정지 |
| `/resume` | 매매 재개 |
| `/cycle` | 즉시 사이클 강제 실행 |
| `/stop` | 시스템 종료 |

### 설정 / 수동 매매

| 명령어 | 설명 |
|--------|------|
| `/setbuy <금액>` | 런타임 최대 매수금액 변경 |
| `/buy <종목코드> <수량>` | 수동 시장가 매수 |
| `/sell <종목코드> <수량>` | 수동 시장가 매도 |

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

---

## 주의사항

- **모의투자로 먼저 검증**하세요. `TRADING_MODE=paper`가 기본값입니다.
- 실전 전환 시 `TRADING_MODE=live`로 변경하면 자동으로 실전 앱키·계좌 사용.
- `.env` 파일에는 API 키가 포함되므로 **절대 Git에 커밋하지 마세요**.
- Windows에서 실행 시 **절전모드를 비활성화**하세요 (WSL 프로세스 중단 방지).
- AI 에이전트의 판단은 참고용이며, 투자 손익의 책임은 사용자에게 있습니다.
