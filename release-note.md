# Release Notes

---

## v2.5.1 — 2026-04-12

### 버그픽스: GNB 모드 배지 paper 고착 문제 (`monitor/app.py`)

#### 문제

성적표 탭을 열거나 60초 자동 갱신 시 Live 모드임에도 GNB 배지가 "paper"로 표시되는 버그.

#### 원인

`loadPortfolio()`가 포트폴리오 스냅샷의 `trading_mode`로 GNB 배지를 갱신(`_syncGnbMode`)하는 구조였음.
포트폴리오 스냅샷은 사이클이 돌아야만 업데이트되므로, 모드 변경 후 첫 사이클 전까지는 스냅샷이 구 모드(paper)를 유지.
페이지 새로고침 시 `_pendingKittyMode`가 초기화되어 pending 보호 장치가 작동하지 않고, 60초마다 배지가 "paper"로 리셋되는 루프에 빠짐.

#### 수정

- `_syncGnbMode()` 동작 변경: pending 상태(모드 전환 대기 중)일 때만 스냅샷과 대조 후 pending 해제. pending 없을 때는 스냅샷으로 배지를 덮어쓰지 않음.
- `syncModeBadge()` 신규 함수 추가: `/api/kitty/mode` · `/api/night/mode` 엔드포인트(mode_config.json 기준, 모드 변경 즉시 반영)에서 배지를 읽어옴.
- 성적표 탭 진입 시 + 60초 폴링 시 `syncModeBadge()` 호출 → 항상 최신 모드 표시.

---

## v2.5.0 — 2026-04-11

### 에이전트 프롬프트 전면 강화 + 누적 피드백 시스템 + 매매 안전장치

#### 에이전트 프롬프트 강화 (KR + Night 공통, 12개 파일)

모든 6개 에이전트의 시스템 프롬프트를 전면 재설계:

- **섹터분석가**: 4단계 분석 프레임워크 도입 (시장방향 → 리스크 → 섹터트렌드 → 후보선정). 정량 기준 명확화 (bullish: avg > +0.5%), bearish 섹터는 후보 0개. `market_breadth` 필드 추가.
- **종목발굴가**: 5개 필수 진입 필터를 체크리스트로 구조화. R:R 계산을 reason에 반드시 포함. 포지션 사이징 공식 명시. `rr_ratio`, `total_buy_cost_estimate` 필드 추가.
- **종목평가가**: 6단계 의사결정 트리 도입 (비상→하드손절→소프트손절→익절→일반→BUY_MORE). 수익률 0% 근처 매도 금지 강화. `portfolio_risk_summary` 필드 추가.
- **자산운용가**: 예산 산술을 주문 결정 전 반드시 수행. 평가가 결정에 구속력 부여. `budget_calculation` 필드 추가. 자본보호 모드 발동 조건 명확화.
- **매수실행가**: 실행 전략 구조화 (지정가→시장가→분할매수). 사전 검증 항목 정리.
- **매도실행가**: HIGH/NORMAL 우선순위별 실행 전략 분리. 보고 형식 명시.

#### 누적 피드백 시스템 (`kitty/feedback/store.py`, `kitty_night/feedback/store.py`)

기존 단순 나열에서 누적 분석 시스템으로 전면 개편:

- **검증된 좋은 패턴 (Learned Rules)**: 2회 이상 반복된 good_pattern을 bigram 분석으로 추출 → "반드시 유지" 섹션으로 프롬프트 주입
- **반복 이슈 (Recurring Issues)**: 2회 이상 유사한 improvement를 그룹핑 → "최우선 개선" 섹션으로 주입
- 프롬프트 주입 우선순위: 검증된 규칙 → 반복 이슈 → 최근 3일 상세 피드백
- 점수 바(█░) 시각화 + 평균 점수 표시
- **Evaluator 연계**: 과거 피드백 이력을 AI에게 전달 → 반복 문제 인식 및 누적 개선안 작성

#### Night 매매 안전장치 (`kitty_night/`)

- **BuyExecutor 잔고 사전 검증**: 매수 전 `get_available_usd()` 실시간 조회, 잔고 부족 시 수량 자동 조정/스킵. "주문 가능금액 초과" 에러 방지.
- **SellExecutor 보유수량 검증**: 매도 전 보유수량 확인, 초과 매도 방지 + 동일 종목 중복 매도 차단.
- **매매 후 포트폴리오 스냅샷 갱신**: 매수/매도 실행 후 즉시 잔고 재조회 → 대시보드에 최신 정보 표시.

#### 정체 구간 매도 로직 개선 (`kitty_night/agents/stock_evaluator.py`, `asset_manager.py`)

- 기존: -0.5%~+0.5% 구간 = "정체"로 판단, 기회비용 이유로 즉시 매도 권고
- 변경: 수익률 0% 근처는 매도 사유 아님. 섹터가 bearish로 전환된 경우에만 교체 허용. 최근 진입 포지션에 시간 부여.

---

## v2.4.0 — 2026-04-11

### 매매일지 Live/Paper 분리 + Agent 관리 탭

#### 매매일지 자동 모드 필터 (`monitor/app.py`)

- GNB 뷰(🐱 Kitty / 🌙 Night) + 현재 투자 모드(Paper/Live)에 따라 매매일지 자동 필터링
- 별도 드롭다운 없이 헤더에 현재 뷰·모드 표시 ("🌙 Night · Live 전체 거래 N건")
- 각 거래 행에 `L` (Live, 금색) / `P` (Paper, 회색) 배지 표시
- 거래 리포트 JSON에 `mode` 필드 추가 — 사이클마다 실제 거래 모드 기록

#### 관리 탭 이름 변경

- `🧠 성향관리` → `🤖 Agent 관리`

---

## v2.3.0 — 2026-04-11

### Live 모드 안전 전환 + 모드 영속화

#### KIS 해외주식 주문 오류 수정 (`kitty_night/broker/kis_overseas.py`)

- **`주문구분 입력오류` 버그 수정**: 미국 주식 매수(`TTTT1002U`)에서 `ORD_DVSN="01"`(시장가)이 전달되던 문제 수정
  - KIS 해외 매수 API는 `"00"` (지정가), `"32"` (LOO), `"34"` (LOC)만 허용; `"01"` 미지원
  - `price=0` 전달 시 paper/live 구분 없이 `_paper_aggressive_price()`로 지정가 변환 후 `ord_dvsn="00"` 고정
- `reset_token()` 메서드 추가 — 모드 전환 시 캐시된 토큰·시세 강제 만료

#### 투자 모드 런타임 전환 + 재시작 영속화 (`kitty_night/main.py`)

- `night-commands/night_mode_request.json` 폴링으로 런타임 중 Paper ↔ Live 전환
- `night-commands/night_mode_config.json` 읽기 — 컨테이너 재시작 후에도 마지막 모드 자동 복원
- Live 모드 기동 시 30초 카운트다운 + Telegram 경고

#### 모니터 UI 개선 (`monitor/app.py`)

- **GNB 모드 셀렉트 → 읽기 전용 배지**: Paper/Live 토글이 GNB에서 제거됨
- **관리 > 시스템 탭 신설**: 투자 모드 변경(KR·Night 각각 라디오) + LLM 관리 통합
- 모드 전환 Pending 중 포트폴리오·매매일지에 플레이스홀더 표시
- `GET /api/night/mode`, `GET /api/kitty/mode` 엔드포인트 추가 (config 파일 우선)
- `POST /api/night/set-mode`, `POST /api/set-mode` — config 파일 영속 저장 추가

#### Telegram 화이트리스트 (`kitty/telegram/bot.py`, `kitty_night/telegram/bot.py`)

- `_ALLOWED_USER_IDS` 화이트리스트 추가 — chat ID와 **user ID 동시 검증**
- 허가되지 않은 user ID는 `⛔ 권한 없음` 응답 후 차단

#### 배포 스크립트 (`start.sh`)

- `NIGHT_TRADING_MODE` Secrets Manager에서 읽어 `.env.night`에 반영 (재배포 후 모드 초기화 방지)

---

## v2.2.0 — 2026-04-09

### 손실 최소화 전략 강화 — 익절 확대 + 다층 손절 시스템

손실 폭 최소화와 수익 극대화를 동시에 달성하기 위한 전략 체계 전면 강화.
6개 파일(tendency × 2, stock_evaluator × 2, asset_manager × 2, stock_picker × 2)에 걸쳐 적용.

#### 투자 성향 레벨값 조정 (`kitty/agents/tendency.py`, `kitty_night/agents/tendency.py`)

**익절(take_profit) 목표 상향 — 수익을 더 오래 추적**

| 레벨 | 이전 (KR) | 이후 (KR) | Night |
|------|----------|----------|-------|
| L1 | +3% | **+5%** | +5% |
| L2 | +5% | **+8%** | +8% |
| L3 | +7% | **+12%** | +12% |
| L4 | +12% | **+18%** | +18% |
| L5 | +18% | **+25%** | +28% |
| L6 | +25% | **+35%** | +40% |

**손절(stop_loss) 임계값 촘촘하게 조정 — 손실 폭 감소**

| 레벨 | 이전 (KR) | 이후 (KR) | Night |
|------|----------|----------|-------|
| L1 | -2.0% | **-1.5%** | -1.5% |
| L2 | -3.0% | **-2.5%** | -2.5% |
| L3 | -4.0% | **-3.5%** | -4.0% |
| L4 | -6.0% | **-5.0%** | -6.0% |
| L5 | -9.0% | **-7.5%** | -8.5% |
| L6 | -13.0% | **-10.0%** | -12.0% |

**프리셋 손익비(R:R) 개선**

| 프리셋 | TP | SL | R:R (이전) | R:R (이후) |
|--------|----|----|-----------|-----------|
| aggressive | +8% | -2.5% | 1.67:1 | **3.2:1** |
| balanced | +12% | -3.5% | 2.0:1 | **3.4:1** |
| conservative | +25% | -3.5% | 4.5:1 | **7.1:1** |

#### 다층 손절 지침 추가 (`_build_directive()`)

에이전트에 주입되는 투자 성향 지침에 6가지 손실 최소화 기술 규칙 추가:

① **소프트 스탑 (조기 경고)**: 손절 임계치 50% 지점에서 섹터 약세이면 즉시 PARTIAL_SELL 실행
② **하드 스탑**: 손절 임계치 초과 시 PARTIAL_SELL ~50% 무조건 실행 (예외 없음)
③ **비상 스탑**: 손절 기준 2배 초과 또는 하한가 근접 시 전량 SELL
④ **트레일링 스탑**: TP 목표의 50% 달성 후 40%를 반납하면 PARTIAL_SELL로 수익 보전
⑤ **거래량 모멘텀 이탈**: 당일 -1.5% 이하 + 섹터 neutral/bearish → 손절 전이라도 PARTIAL_SELL 고려
⑥ **손익비 원칙**: 신규 매수 종목은 예상 TP÷SL ≥ 2.5:1 이상만 허용

#### Stock Evaluator 기술지표 기반 조기 청산 (`kitty/agents/stock_evaluator.py`, `kitty_night/agents/stock_evaluator.py`)

- 기존 수익률 기반 판단에 "기술지표 기반 조기 청산" 섹션 추가 (평가 기준 2번)
- 정체 판단 기준 강화: ±1% → **±0.5%** (엄격한 기회비용 판단)
- 섹터별 분기 조건도 ±0.5% 기준으로 통일
- 거래량 모멘텀 이탈 신호(`change_rate_today ≤ -1.5%` + 섹터 약세) 시 조기 청산 권고

#### Asset Manager 손실 트리아지 + 자본보호 모드 (`kitty/agents/asset_manager.py`, `kitty_night/agents/asset_manager.py`)

- **손실 트리아지**: 복수 종목 동시 손절 시 손실률 최악 종목부터 우선 처리
- **자본보호 모드**: 포트폴리오 합산 손실 -3% 이상이면 신규 매수 전면 중단, 손절·소프트스탑 최우선 실행
- **신규 매수 품질 게이트**: R:R ≥ 2.5:1 미달 종목 승인 거부
- **포트폴리오 손실 중 매수 제한**: 합산 손실 -3% 이상 상태에서 신규 매수 시 주문금액 50% 상한
- 주문 우선순위 4단계 → **7단계**로 세분화 (비상 스탑 → 하드 스탑 → 소프트 스탑 → 교체 매도 → 익절 → 신규 매수 → BUY_MORE)
- 교체 기준 정체 범위도 ±0.5%로 강화

#### Stock Picker 진입 필터 강화 (`kitty/agents/stock_picker.py`, `kitty_night/agents/stock_picker.py`)

- **R:R ≥ 2.5:1 필터**: 계산값 미달 시 BUY → HOLD로 표기
- **모멘텀 확인**: 당일 등락률 0% 이상인 종목만 신규 진입 (하락 중 진입 금지)
- **거래량 가속 필터**: 거래량 급감 종목 제외
- **추격매수 방지**: 당일 고점 대비 -2~3% 이내 구간에서만 진입 허용

---

## v2.1.0 — 2026-04-09

### Night Mode 포트폴리오 평가 버그 수정

`/night`, `/nportfolio` 명령에서 Total value, P&L, CASH가 모두 0으로 표시되던 버그 수정.

**원인** (`kitty_night/broker/kis_overseas.py`)

`get_balance()`가 KIS API 원본 JSON을 그대로 반환하고 있었음:
```
return resp.json()  # {"output1": [...보유종목...], "output2": [...요약...]}
```

`main.py`에서는 `balance_data.get("holdings", [])` 로 읽으므로 `"holdings"` 키가 없어 항상 빈 리스트 반환.
→ 보유종목 0개, total_eval = available_usd만 반영, total_pnl = 0, 포트폴리오 스냅샷 공백.

**수정** (`kitty_night/broker/kis_overseas.py`)

`get_balance()`에서 `output1`을 순회하여 정규화된 `holdings` 리스트로 변환 후 반환:

```python
return {
    "holdings": [
        {
            "symbol": item["ovrs_pdno"],
            "name": item["ovrs_item_name"],
            "excd": item["ovrs_excg_cd"],
            "quantity": int(item["ovrs_cblc_qty"]),
            "avg_price": float(item["pchs_avg_pric"]),
            "current_price": float(item["now_pric2"]) or avg_price,
            "eval_amount": float(item["ovrs_stck_evlu_amt"]),  # 0이면 가격×수량으로 추정
            "pnl_amount": float(item["frcr_evlu_pfls_amt"]),   # 0이면 (현재가-평균가)×수량으로 추정
            "pnl_rate": float(item["evlu_pfls_rt"]),
        }
        for item in output1 if int(item["ovrs_cblc_qty"]) > 0
    ],
    "output2": data["output2"],
}
```

- 0수량 항목 자동 필터링
- `eval_amount`/`pnl_amount`가 0인 경우 현재가 기반 추정값으로 fallback
- `telegram/bot.py`의 `_cmd_nportfolio()`에서 읽는 필드명(`quantity`, `avg_price`, `eval_amount`, `pnl_rate`)과 완전 일치

---

## v2.0.0 — 2026-04-03

### 🌙 Night Mode — 미국주식 자동 매매 시스템

한국주식(kitty-trader)과 완전히 독립된 미국주식 자동 매매 시스템 추가.
동일한 7-에이전트 아키텍처를 미국 시장에 맞게 재설계. 코드·데이터·환경변수 완전 분리.

**`kitty_night/` 패키지 신규 생성** (26개 파일)

- `agents/` — 7개 Night 에이전트 (NightTendencyAgent ~ NightSellExecutorAgent)
  - NightBaseAgent 기반, kitty와 import 공유 없음
  - 영어 프롬프트, USD 기반, 미국 시장 특화 파라미터
  - 익절 3~30% / 손절 -2~-15% / 분할 기준 10주 / 사이클 15분
- `broker/kis_overseas.py` — KIS 해외주식 API (시세·잔고·주문·취소)
  - 1.2초 주문 스로틀, 0.5초 시세 스로틀
  - USD float 가격 처리 (한국주식 KRW int과 다름)
- `market_calendar.py` — NYSE 거래일 캘린더 + DST 자동 대응
  - MarketPhase: CLOSED → WAITING → PRE_MARKET → MARKET → POST_MARKET
- `config.py` — NightSettings (Pydantic), `NIGHT_` prefix 환경변수
- `main.py` — MarketPhase 기반 메인 루프
  - PRE_MARKET: 분석만 실행 (주문 스킵)
  - MARKET: 전체 매매 사이클 (매도 우선 → 매수)
  - POST_MARKET: 성과 평가 + 성향 자동 조정
  - 바로미터: SPY, QQQ, AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, JPM
- `evaluator/performance.py` — NightPerformanceEvaluator
- `report.py` — NightDailyReport (night-reports/ 저장, 🌙 텔레그램 요약)
- `telegram/bot.py` — NightTelegramReporter (메시지 전용, polling 없음)

**`monitor/app.py` 대시보드 통합**

- GNB에 🐱 Kitty ↔ 🌙 Night 뷰 전환 버튼 추가
- Night 전용 API 4개: `/api/night/portfolio`, `/api/night/tendency`, `/api/night/agent-scores`, `/api/night/token-usage`
- Night 탭: 성향 카드 + USD 포트폴리오 테이블 + 에이전트 점수 그리드/히트맵
- Night 로그 감시 (`kitty-night_*.log` 패턴)

**Docker 인프라**

- `Dockerfile.night` 신규 — python:3.11-slim, kitty_night/ 패키지 빌드
- `docker-compose.yml` — 3개 서비스 (kitty + kitty-night + monitor)
- `start.sh` — Night 컨테이너 빌드/실행 + `.env.night` 자동 생성/삭제
- `requirements.night.txt` 신규 — exchange-calendars, pydantic 등

**AWS 인프라 변경**

- EC2 24/7 상시 가동으로 전환 (기존 EventBridge 시작/중지 스케줄 비활성화)
- Secrets Manager `kitty/prod`에 Night 키 추가 (NIGHT_KIS_*, NIGHT_AI_* 등)

**`deployments.md` 업데이트**

- EventBridge 비활성화 절차 + 운영 시간표 추가
- kitty-night-trader 배포 절차 (섹션 10)
- Secrets Manager Night 키 목록 (섹션 11)

---

## v1.9.0 — 2026-04-03

### 모니터 대시보드 UX 개선

**관리 메뉴 상태 탭 제거** (`monitor/app.py`)

관리 서브탭에서 🏥 상태 탭 제거. 에러·토큰 2개 탭으로 단순화.

- `sub-tab-health` / `tab-health` HTML 블록 삭제
- `switchAdmin()` 및 자동 갱신 로직에서 `health` 제거
- 관리 진입 시 기본 탭 `health` → `errors` 변경

**KST 시간대 통일** (`monitor/app.py`)

날짜 계산이 UTC 기준으로 처리되던 문제 수정.

- JS `today` 변수: `new Date().toISOString()` → `Date.now() + 9h` offset 적용 (에러 필터 기본 날짜)
- `api_stats` SQLite 쿼리: `date('now','-13 days')` (UTC) → Python `_now() - 13일` (KST) 파라미터 바인딩

**모바일 레이아웃 개선** (`monitor/app.py`)

폰 화면에서 성적표 레이아웃 깨짐 수정.

- 투자 전략 카드 수치 폰트 12px bold → 10px semibold (좁은 셀에서 넘침 방지)
- 포트폴리오 요약 카드 3개: 라벨을 금액 위 좌측으로 이동, 금액 폰트 26px → 17px
- 포트폴리오 테이블 컬럼명에 단위 이동: 수량(주), 평균단가(원), 현재가(원), 수익률(%) — 셀 내 단위 문자 제거

**종합 평가 리포트 접기/펼치기** (`monitor/app.py`)

투자 전략 카드 하단에 날짜별 종합 평가 내용을 접을 수 있는 리포트 섹션 추가.
기존 헤더의 말줄임(`...`) 방식 제거.

- dims 그리드 아래 `td-report` 섹션: 제목("N월 N일 종합 평가 Report") + 미리보기 40자 + `[more]` 버튼
- `toggleReport()`: `more` 클릭 시 전문 펼침, `less` 클릭 시 접기
- `d.ts` 문자열에서 월/일 파싱해 제목 생성

---

## v1.8.0 — 2026-04-02

### 로그 가독성 개선 — 종목명(종목코드) 형식

**로그에 종목명 추가** (`kitty/broker/kis.py`, `kitty/agents/buy_executor.py`, `kitty/agents/sell_executor.py`, `kitty/report.py`, `kitty/telegram/bot.py`)

기존 로그에 종목코드만 출력되던 모든 위치를 `종목명(종목코드)` 형식으로 통일.
종목명을 알 수 없는 경우(조회 실패, 오류 케이스)는 코드만 그대로 출력.

- `broker/kis.py`: `buy()`, `sell()`에 `name: str = ""` 파라미터 추가. 로그 `종목명(코드)` 형식으로 변경
- `buy_executor.py`: `_execute_smart_buy(name="")` 파라미터 추가. `chunk_label`, 스킵/완료/실패 로그 전체 수정. `consolidated` 결과 딕셔너리에 `name` 필드 추가
- `sell_executor.py`: 동일 패턴. 긴급 손절, 하한가 강행, 스마트 매도 완료/실패 로그 수정
- `report.py`: 매수/매도 체결 로그에서 `r.get('name')` 활용
- `telegram/bot.py`: 수동 매수/매도 시 `get_quote()`로 종목명 조회 후 로그에 반영

### 즉시 사이클 실행 후 타이머 리셋

**중복 사이클 방지** (`kitty/main.py`)

텔레그램 `/cycle` 명령으로 즉시 사이클을 실행한 경우, 이후 메인 루프의 5분 타이머가 즉시 실행 시점부터 다시 카운트되도록 수정.

- `_last_cycle_time: float` 변수로 마지막 사이클 실행 시각 추적
- `_cycle_now()`: 실행 전 `_last_cycle_time` 갱신
- 메인 루프: 고정 `asyncio.sleep(300)` → `elapsed` 계산 후 잔여 시간만 대기

### `/dashboard` EC2 퍼블릭 IP 자동 조회 수정

**IMDSv2 지원** (`kitty/telegram/bot.py`)

기존 IMDSv1 방식(`urllib.request.urlopen` 단순 GET)은 최신 EC2 인스턴스에서 IMDSv2 필수 설정 시 항상 실패해 `EC2-IP`로 fallback되던 문제 수정.

- `_fetch_ec2_public_ip()` 정적 메서드 추출
- IMDSv2 2단계 프로토콜: PUT으로 토큰 발급 → GET으로 퍼블릭 IP 조회 (`aiohttp` 비동기)
- 기존 블로킹 `urllib` 제거, 응답에 클릭 가능한 Markdown 링크(`[URL](URL)`) 형식으로 변경

---

## v1.7.0 — 2026-04-01

### 피드백 루프 강화

**의사결정 상세 기록 추가** (`kitty/evaluator/performance.py`)

각 에이전트 평가 시 개별 의사결정 성패를 `✓`/`✗` 기호로 기록한 `decision_details` 문자열을 AI 피드백 생성 프롬프트에 전달.
AI가 구체적인 케이스를 참고해 더 정확한 피드백을 생성할 수 있도록 개선.

- 에이전트별 `_eval_*` 함수에서 `detail_lines` 리스트 생성 및 `decision_details` 반환
- `_ai_feedback(agent_name, metrics, decision_details="")` — 의사결정 상세 섹션 포함 프롬프트
- AI 피드백 출력 필드 추가: `good_pattern` (80자, 유지할 성공 패턴)
- `max_tokens` 200 → 400 (good_pattern 필드 생성 공간 확보)
- AI 피드백 생성 시 Gemini 모델 지원 추가
- 점수 기본값 fallback: 5 → 50 (0~100 스케일에 맞게 조정)

**피드백 저장소 전면 개편** (`kitty/feedback/store.py`)

- `MAX_ENTRIES`: 10일 → 14일 (2주치 보관)
- `PROMPT_ENTRIES`: 최근 5일 system_prompt 주입
- `get_feedback_prompt()` 완전 재작성:
  - 점수 추이 라인 (최근 7일) + 📈 개선 중 / 📉 하락 중 / ➡️ 유지 아이콘
  - 각 항목에 `good_pattern` (✅ 유지) + `improvement` (💡 개선) 표시
  - 하단에 `[최우선 개선 과제]` 섹션 (최근 3일 중복 제거)

**투자성향 장 마감 후 자동 조정** (`kitty/agents/tendency.py`, `kitty/main.py`)

- `TendencyAgent.update_strategy(eval_results)`: 장 마감 성과 평가 후 AI가 내일 각 차원 레벨을 조정
- 1 사이클 최대 ±2 레벨 제한, 결과는 `logs/tendency_state.json`에 즉시 저장
- `main.py`: `reload_feedback()` 직후 `tendency_agent.update_strategy(results)` 자동 호출

---

## v1.6.0 — 2026-04-01

### 수익 극대화 파이프라인

**섹터분석가 실시간 시장 데이터 기반 분석** (`kitty/agents/sector_analyst.py`, `kitty/broker/kis.py`, `kitty/main.py`)

기존 AI 자체 추론/뉴스 기반 섹터 분석을 실제 시장 데이터 기반으로 전환.
추측이나 외부 뉴스 기반 판단을 금지하고, 실측 수치만으로 판단하도록 시스템 프롬프트 변경.

- `kis.py`: `get_volume_rank(count=20)` 추가 — KIS TR `FHPST01710000`으로 거래량 상위 종목 조회 (symbol, name, current_price, change_rate, volume, turnover)
- `main.py`: `_BAROMETER_SYMBOLS` — 시장 체온계 ETF·종목 10개 (코스피200, 코스닥150, 반도체, 2차전지, 바이오 등)
- `main.py`: `_collect_market_data(broker)` — 바로미터 시세 + 거래량 상위 20개 수집 (사이클 step 1.5)
- `sector_analyst.py`: `run()` — `market_data` 컨텍스트 수신, 상승/하락 종목 수·평균 등락률·거래량 상위 포함 프롬프트 생성

**하드코딩 임계값 전면 제거** (`kitty/agents/stock_evaluator.py`, `stock_picker.py`, `asset_manager.py`)

각 에이전트 시스템 프롬프트의 고정 수치를 제거하고 투자성향관리자 지침을 단일 진실 출처로 통일.

- `stock_evaluator.py`: 손절 -5%, 익절 +15%, 종목 최대 비중 20% 제거 → "투자성향 지침 따름" 참조
- `stock_picker.py`: 하드코딩 임계값 제거, 유동성 기준 명시 (거래량 10만주 미만 또는 거래대금 10억 미만 제외)
- `asset_manager.py`: 잔고 70%/종목 20% 제거 → 투자성향 지침 참조, `max_position_size` 파라미터 추가
- 각 에이전트 기본값 fallback 주석 추가 (지침 없는 경우에도 동작)

**거래량 상위 종목 유동성 연동**

- `stock_picker.py`: `volume_leaders` 컨텍스트 수신, 프롬프트에 유동성 참고 섹션 추가
- `asset_manager.py`: `quotes_text`에 거래량 포함

---

## v1.5.0 — 2026-04-01

### 에이전트 점수 0~100 통일

모든 에이전트 평가 점수를 기존 0~10 (또는 구간별 2~9) 스케일에서 **0~100 통일**.

- `kitty/evaluator/performance.py`: 각 `_eval_*` 함수 점수 범위 0~100으로 재조정
  - 섹터분석가: 적중률 × 100
  - 종목발굴가: 수익률 구간별 20~90점
  - 종목평가가: 정확도 × 100
  - 자산운용가: 방향성 구간별 20~90점
  - 매수/매도실행가: 구간별 30~90점, 체결 0건 시 10점
- `kitty/feedback/store.py`: 점수 추이 비교 임계값 ±5점(0~100 기준)으로 조정
- `monitor/app.py`:
  - 점수 색상 임계값: 4/7 → 40/70
  - 점수 바 너비: `score * 10` → `score` (0~100% 직접 매핑)
  - 히트맵 셀 점수 표시: `/10` → `/100`
- `kitty/main.py`: `_format_eval_summary()` 이모지 구간 40/70 기준으로 변경
- `kitty/agents/tendency.py`: `update_strategy()` 판단 기준 0~100 기준으로 조정

---

## v1.4.0 — 2026-04-01

### 투자성향관리자 5차원 6단계 레벨 시스템

기존 3가지 고정 프로필(공격적/균형/보수적)을 **5개 차원 × 6단계 레벨** 독립 조정 방식으로 전면 교체.
각 차원을 독립적으로 조정해 다양한 투자 성향 조합을 표현할 수 있음 (예: 익절은 공격적이면서 손절은 보수적).

**`kitty/agents/tendency.py` 완전 재작성**

- 5개 차원: `take_profit`, `stop_loss`, `cash`, `max_weight`, `entry`
- 6단계 레벨: L1(최공격적) ~ L6(최보수적) — 차원별 독립 값 테이블
- 프리셋 3종: `aggressive`(L2 all), `balanced`(L4 위주), `conservative`(L5/L3 혼합)
- 초기 레벨: 모든 차원 L2 (공격적)
- `_build_directive()`: 현재 레벨 조합으로 지침 문자열 동적 생성
- `update_strategy(eval_results)`: 장 마감 후 AI가 각 차원 레벨 조정 (±2 한도)
- `_save_state()` / `_load_state()`: `logs/tendency_state.json` 영속 저장 (EC2 재시작에도 유지)
- `profile` 프로퍼티: 현재 레벨 + 계산된 파라미터 값 딕셔너리 반환

**모니터 성향 카드 개편** (`monitor/app.py`)

- 기존 3개 파라미터 카드 → 5개 차원 격자(grid) 표시
- 각 차원 셀에 레벨 배지(lv-1~lv-6 색상 코드) + 레벨명 + 현재 값 표시
- `setDimCell()` JS 헬퍼 추가, `loadTendency()` 5차원 levels dict 파싱

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
