# kitty/ — KR 주식 트레이더

> 루트 CLAUDE.md도 함께 로드됨. 공통 아키텍처/인프라는 거기 참조.

## 운영 시간 & 사이클 구조

- **장 시간**: 09:00~15:30 KST (kitty는 08:50 기동 대기)
- **사이클 주기**: `main.py`의 루프 — 장중 매 N분마다 에이전트 파이프라인 실행
- **장 마감 후**: `update_strategy()` 호출 → 내일 투자 레벨 AI 결정

## KIS API (국내주식) 특이사항

```python
# broker/kis.py
get_balance()     → output1: 보유종목, output2: 잔고 요약
get_quote()       → hts_kor_isnm(종목명), stck_prpr(현재가), hts_avls(시가총액)
buy(symbol, qty, price=0)   # price=0 → 시장가
sell(symbol, qty, price=0)
```

- 국내는 거래소 코드 구분 없음 (KOSPI/KOSDAQ 모두 동일 엔드포인트)
- `evlu_pfls_amt`: 평가손익금액(원), `evlu_pfls_rt`: 수익률(%)
- 잔고조회 `output1`의 `hldg_qty > 0`인 항목만 실제 보유

## 에이전트 파이프라인 상세

```
섹터분석가
 ↓ 섹터 전망 (bullish/neutral/bearish) + 후보 종목코드 리스트
종목발굴가
 ↓ 매수 후보 (symbol, name, reason, expected_return)
종목평가가  ← 현재 보유종목 + 투자성향지침 입력
 ↓ 각 보유종목 → HOLD / BUY_MORE / PARTIAL_SELL / SELL
자산운용가  ← 종목평가가 결과 + 발굴가 결과 + 가용현금
 ↓ final_orders (우선순위 포함)
매도실행가  → sell_results
매수실행가  → buy_results
투자성향관리자 (장 마감 후) → 내일 레벨 조정
```

**결과 구조 (buy/sell results)**:
```python
{"symbol": "005930", "name": "삼성전자", "action": "BUY",
 "quantity": 10, "price": 75000, "status": "FILLED|SKIPPED|FAILED",
 "reason": "..."}
```

---

## 수익 구조 분석 — 왜 돈을 벌고 잃는가

### 현재 수익 원천
1. **섹터 모멘텀 추종**: 섹터분석가가 bullish 판정한 섹터의 강세 종목 매수
2. **손절 규율**: TendencyAgent의 stop_loss 기준 → 큰 손실 방지
3. **분할 매도**: 50% partial sell → 수익 일부 확보 + 추가 상승 추적

### 현재 수익의 구조적 한계

**① 정보 지연 문제**
- 섹터분석가가 "반도체 bullish"를 판단할 때 이미 시장이 반영한 경우가 많음
- 개선 방향: **외국인/기관 매매 동향**을 직접 피드로 받아 섹터 신호 보강

**② 진입 타이밍 문제**
- 현재: 섹터 bullish + 당일 등락률 임계치 이하 → 매수
- 문제: 추세 초기 진입 vs. 추세 말기 추격 매수 구분 불가
- 개선 방향:
  - 52주 신고가 근처 종목 → 돌파 매수 (모멘텀 강)
  - 5일/20일 이평선 배열 확인 (골든크로스 직후 진입)
  - 거래량 급증(평균 대비 200% 이상) + 가격 상승 → 강력 진입 신호

**③ 종목 다양성 부족**
- 보유 종목이 1~2개면 1종목 폭락 시 포트폴리오 전체 타격
- 목표: **4~5종목, 섹터 분산** (현재 자산운용가 로직에 이미 있지만 실행률 낮음)
- 개선: 가용현금이 충분할 때 적극적으로 3번째 종목 진입 유도

**④ 손절 후 재진입 없음**
- 손절된 종목이 반등해도 시스템이 재매수하지 않음
- 개선 방향: 손절 후 N 사이클 후 해당 종목 재평가 로직

**⑤ 장 마감 직전 리스크**
- 14:50 이후 유동성 감소, 스프레드 확대
- 마지막 사이클에서 신규 매수는 피하는 게 유리

### 구조적으로 수익을 더 내려면

**단기 (현재 구조 개선)**
- [ ] 섹터분석가에 **외국인 순매수 상위 종목** 가중치 추가
- [ ] 종목발굴가에 **52주 신고가 돌파 + 거래량 급증** 필터 추가
- [ ] 자산운용가: 보유 종목 < 3개일 때 신규 매수 더 적극적으로 강제
- [ ] 14:30 이후 사이클에서 신규 매수 금지 조건 추가

**중기 (새 에이전트/도구)**
- [ ] `MarketSignalAgent`: 외국인/기관 매매, 프로그램 매매 신호 수집
- [ ] `TechnicalFilter`: RSI 과매수(>70) 진입 금지, MACD 골든크로스 가중
- [ ] 뉴스/공시 이벤트 감지 → 실적 발표일 전날 포지션 축소

**수익 시뮬레이션 관점**
- 연간 수익률 목표를 역산: 목표 수익률 ÷ 평균 보유 기간 → 필요 월 수익률
- 현재 손익비(R:R) 3:1 가정 → 승률 40%만 넘으면 수익 구조

---

## KR 에이전트 프롬프트 개선 포인트

### 섹터분석가 개선 방향
- 단순 "bullish/bearish" 외에 **강도 점수(1~10)** 부여하도록 개선
- 섹터 간 자금 이동 방향 감지 ("반도체에서 바이오로 로테이션")

### 종목발굴가 개선 방향
- 현재: 섹터 전망 기반 후보 선정
- 추가: **모멘텀 스코어** (최근 5일 수익률 순위), **거래량 이상 감지**
- 이미 손절된 종목은 동일 사이클에서 재매수 후보 제외

### 종목평가가 개선 방향
- HOLD 남발 방지: 섹터 전망이 neutral이면 수익 중인 종목도 PARTIAL_SELL 적극 권고
- 보유 기간 고려: 3일 이상 보유 + 수익 정체 → SELL 권고

### 자산운용가 개선 방향
- 전체 포트폴리오 손익이 -3% 이하일 때 신규 매수 중단 (Capital Protection Mode)
- 섹터 중복 방지: 같은 섹터 2종목 이상 보유 시 추가 매수 금지

---

## 주요 파일 & 수정 위치

| 목적 | 파일 | 핵심 함수/클래스 |
|---|---|---|
| 매매 사이클 수정 | `main.py` | `run_cycle()` |
| 에이전트 프롬프트 수정 | `agents/{에이전트}.py` | `SYSTEM_PROMPT` 상수 |
| 투자성향 레벨값 수정 | `agents/tendency.py` | `LEVEL_VALUES` 딕셔너리 |
| 지침 문자열 수정 | `agents/tendency.py` | `_build_directive()` |
| KIS API 수정 | `broker/kis.py` | 각 메서드 |
| 포트폴리오 스냅샷 필드 추가 | `utils/portfolio.py` | `print_portfolio_and_balance()` |
| 피드백 저장 로직 | `feedback/store.py` | `append_entry()`, `get_feedback_prompt()` |

## 테스트 방법

```bash
# 로컬에서 paper 모드 단일 사이클 테스트 (KIS API 연결 필요)
TRADING_MODE=paper python -m kitty.main

# 에이전트 단독 테스트
python -c "
import asyncio
from kitty.agents.sector_analyst import SectorAnalyst
async def test():
    a = SectorAnalyst()
    r = await a.run({})
    print(r)
asyncio.run(test())
"
```
