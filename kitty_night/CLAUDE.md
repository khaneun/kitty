# kitty_night/ — US 주식 Night 트레이더

> 루트 CLAUDE.md도 함께 로드됨. 공통 아키텍처/인프라는 거기 참조.

## 운영 시간 & MarketPhase

- **미국 정규장**: 09:30~16:00 ET = KST 23:30~06:00 (서머타임 시 22:30~05:00)
- **Pre-market**: 04:00~09:30 ET
- **After-hours**: 16:00~20:00 ET
- Night 트레이더는 `MarketPhase`로 시장 상태를 자동 판별해 사이클 실행 여부 결정

## KIS 해외주식 API 핵심

```python
# broker/kis_overseas.py
_EXCD_MAP = {"NAS":"NASD", "NYS":"NYSE", "AMS":"AMEX", ...}
_to_order_excd(excd)  # 시세용 3자리 → 주문용 4자리 변환 (반드시 사용)

get_balance(OVRS_EXCG_CD="")   # 빈 문자열 = 전 거래소 조회 (특정 코드 지정 시 해당만)
buy(symbol, excd, qty, price=0.0)   # price=0.0 → 시장가 (USD float)
sell(symbol, excd, qty, price=0.0)
get_quote(symbol, excd)   # excd: 3자리 (NAS/NYS/AMS)
```

**rt_cd 체크 필수**: HTTP 200이어도 `rt_cd != "0"`이면 API 에러

## Night 에이전트 파이프라인

```
NightSectorAnalyst
 ↓ US 섹터 전망 (Technology/Healthcare/Energy/...) + bullish 섹터 후보 티커
NightStockPicker
 ↓ 매수 후보 (symbol, name, excd, expected_return_pct, reason)
NightStockEvaluator  ← 보유종목 + 투자성향지침
 ↓ HOLD / BUY_MORE / PARTIAL_SELL / SELL
NightAssetManager    ← Evaluator + Picker 결과 + 가용현금(USD)
 ↓ final_orders
NightSellExecutor    → sell_results (USD 가격)
NightBuyExecutor     → buy_results
TendencyAgent (장 마감 후) → 내일 레벨 조정
```

**Night 특이점**: 가격이 USD float. `price: float`, `quantity: int` (주 단위, 소수 없음)

---

## 수익 구조 분석 — US 주식에서 돈을 벌고 잃는 이유

### 현재 수익 원천
1. **빅테크 모멘텀**: Magnificent 7 (AAPL/MSFT/NVDA/AMZN/GOOGL/META/TSLA) 편중 시 추세 수혜
2. **섹터 로테이션**: AI/반도체 섹터 모멘텀 포착
3. **KR 대비 유리한 점**: 유동성 높음, 스프레드 좁음, 더 많은 종목 선택지

### 현재 수익의 구조적 한계

**① 실적 발표(Earnings) 리스크 — 가장 큰 단일 위험**
- US 주식은 실적 발표 전후 10~30% 갭이 일상적
- 현재 시스템은 실적 발표일을 모름 → 보유 중 갭 하락 손실 위험
- **핵심 개선 과제**: 실적 발표 2일 전 해당 종목 포지션 자동 청산

**② FOMC/매크로 이벤트 무시**
- 금리 결정일(연 8회), CPI 발표일에 전체 시장이 3~5% 움직임
- 이벤트일 전날 포지션 축소 + 이벤트 후 방향 확인 후 진입이 유리

**③ 섹터 내부 구조 미활용**
- "Technology bullish" 판정 시 어떤 sub-sector가 더 강한지 구분 안 됨
  - AI 인프라 (NVDA, AMD) vs. SaaS (MSFT, CRM) vs. 하드웨어 (AAPL) → 전혀 다른 움직임
- 개선: NightSectorAnalyst가 sub-sector 레벨까지 분석

**④ SPY/QQQ 상관관계 미활용**
- 개별 종목은 시장 지수와 80% 이상 상관됨
- SPY(S&P500)/QQQ(NASDAQ) 당일 방향이 음수이면 개별 매수 자제해야 함
- 현재 시스템은 지수 방향을 참고하지 않음

**⑤ VIX (공포지수) 무시**
- VIX > 25: 시장 공포 구간 → 포지션 크기 50% 축소가 통계적으로 유리
- VIX < 15: 안정 구간 → 더 공격적 진입 가능
- 현재 TendencyAgent가 VIX를 참고하지 않음

**⑥ After-hours 갭 대응 없음**
- 실적 발표 / 매크로 뉴스가 After-hours에 나오면 다음 날 갭 오픈
- Night 트레이더가 Pre-market(04:00~09:30 ET)을 확인하지 않음

### 구조적으로 수익을 더 내려면

**단기 (현재 구조 개선)**
- [ ] **실적 발표 캘린더 체크**: 매수 전 Earnings 날짜 확인 → 발표 3일 이내 매수 금지
- [ ] **SPY/QQQ 방향 필터**: 지수 당일 -1% 이하이면 신규 매수 자제
- [ ] **거래량 급증 필터**: 평균 거래량 대비 200% 이상 → 강한 모멘텀 신호로 가중
- [ ] **52주 신고가 돌파**: 신고가 돌파 + 거래량 급증 → 최우선 매수 후보

**중기 (새 신호 / 에이전트)**
- [ ] `MacroSignalAgent`: VIX 수준, 미국채 10년물 방향, 달러 인덱스(DXY) → 시장 리스크 온/오프 판단
- [ ] `EarningsCalendarTool`: polygon.io / alphavantage API로 실적 발표일 조회
- [ ] Sub-sector 분류: Technology → AI인프라 / SaaS / 하드웨어 / 반도체 로 세분화

**US 시장 특유의 수익 전략**
- **OPEX (옵션 만기)**: 매월 3번째 금요일 → 변동성 급증, 이 주에는 포지션 축소
- **1월 효과**: 12월 말 세금 손실 매도 → 1월 초 반등 (소형주에서 강하게 나타남)
- **실적 시즌 플레이**: 실적 발표 후 "Sell the news" 패턴 → 좋은 실적에도 하락하는 경우 많음

**손익비 관점**
- US 주식은 KR보다 모멘텀이 강하므로 take_profit 목표를 더 높게 설정 유리
  (Night TendencyAgent의 L5/L6: +28%~+40% 수준이 현실적)
- 단, 개별 종목 변동성이 크므로 max_weight는 KR보다 낮게 (15~20% 이하 권장)

---

## Night 에이전트 프롬프트 개선 포인트

### NightSectorAnalyst 개선 방향
- SPY/QQQ 당일 방향을 입력으로 받아 전체 시장 리스크 온/오프 먼저 판단
- Sub-sector 레벨 분석 (AI인프라 / SaaS / 바이오-대형 / 바이오-소형 등)
- VIX 수준에 따라 bullish 임계치 상향 조정

### NightStockPicker 개선 방향
- 실적 발표일 N일 이내 종목은 후보에서 자동 제외
- 52주 신고가 종목에 가중치 부여
- 시가총액 $10B 이상 유동성 필터 기본 적용

### NightStockEvaluator 개선 방향
- 보유 기간 길어질수록 실적 발표 리스크 증가 → 4일 이상 보유 시 리스크 경고 추가
- USD 기준이므로 환율 변동은 별도 리스크 (KRW 기준 P&L 계산 시 왜곡 발생)

### NightAssetManager 개선 방향
- 전체 포트 손익 -3% 이하 → Capital Protection Mode (신규 매수 중단)
- 섹터 분산: 같은 sub-sector 2종목 이상 보유 금지
- OPEX 주: 최대 포지션 크기 50%로 자동 제한

---

## 주요 파일 & 수정 위치

| 목적 | 파일 | 핵심 위치 |
|---|---|---|
| 해외 매매 사이클 수정 | `main.py` | `run_night_cycle()` |
| 해외 에이전트 프롬프트 | `agents/{에이전트}.py` | `SYSTEM_PROMPT` |
| 투자성향 레벨값 (Night) | `agents/tendency.py` | `LEVEL_VALUES` (KR과 별도) |
| 거래소 코드 변환 | `broker/kis_overseas.py` | `_to_order_excd()`, `_EXCD_MAP` |
| 해외 포트폴리오 스냅샷 | `utils/portfolio.py` | `print_night_portfolio()` |
| Night 피드백 저장소 | `feedback/store.py` | `FEEDBACK_DIR = Path("night-feedback")` |

## 주의: KR vs Night 코드 혼동 방지

| 항목 | KR (kitty/) | Night (kitty_night/) |
|---|---|---|
| 가격 타입 | `int` (원) | `float` (USD) |
| 거래소 코드 | 없음 | 필수 (`excd` 파라미터) |
| 피드백 경로 | `feedback/` | `night-feedback/` |
| 스냅샷 경로 | `logs/portfolio_snapshot.json` | `night-logs/night_portfolio_snapshot.json` |
| 텔레그램 가격 | `{price:,}원` | `${price:,.2f}` |
