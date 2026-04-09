# Kitty — AI 자동매매 시스템 개발 가이드

## 프로젝트 개요

Kitty는 KIS Open API + AI 멀티 에이전트로 동작하는 한국/미국 주식 자동매매 시스템.
EC2에서 3개 Docker 컨테이너가 상시 실행된다.

| 컨테이너 | 모듈 | 역할 | 운영 시간 (KST) |
|---|---|---|---|
| `kitty-trader` | `kitty/` | KR 주식 자동매매 | 08:50~15:30 |
| `kitty-night-trader` | `kitty_night/` | US 주식 자동매매 | 21:00~06:00 |
| `kitty-monitor` | `monitor/` | 대시보드 (포트 8080) | 24/7 |

## 인프라

- **EC2**: ap-northeast-2 (서울), 태그명 `kitty-trader`
- **SSH**: `ssh -i ~/kitty-key.pem ec2-user@<IP>`
- **IP 조회**: `aws ec2 describe-instances --filters "Name=tag:Name,Values=kitty-trader" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text`
- **Secrets**: AWS Secrets Manager `kitty/prod`
- **배포**: `bash start.sh` (git pull → Secrets → .env → docker build × 3 → run)

## 핵심 파일 구조

```
kitty/
  main.py              # KR 트레이딩 루프 (매 사이클 에이전트 파이프라인)
  config.py            # 환경변수 및 설정 (TradingMode, AIProvider)
  agents/
    base.py            # BaseAgent — AI 호출, 피드백 로드, 토큰 기록
    tendency.py        # 투자성향관리자 — 5차원 × 6레벨 전략 결정
    sector_analyst.py  # 섹터분석가
    stock_picker.py    # 종목발굴가
    stock_evaluator.py # 종목평가가 (보유종목 HOLD/SELL 결정)
    asset_manager.py   # 자산운용가 (최종 주문 결정)
    buy_executor.py    # 매수실행가
    sell_executor.py   # 매도실행가
  broker/kis.py        # KIS API 래퍼 (KR 주식)
  feedback/store.py    # 에이전트 피드백 영속 저장소
  evaluator/           # 사이클/일일 성과 평가
  utils/portfolio.py   # 포트폴리오 스냅샷 저장

kitty_night/           # 위와 동일 구조, 해외주식 전용
  broker/kis_overseas.py  # KIS 해외주식 API 래퍼

monitor/app.py         # FastAPI 대시보드 (HTML/CSS/JS 단일 파일)
start.sh               # EC2 전체 배포 스크립트
```

## 에이전트 파이프라인

### KR (kitty/main.py)
```
섹터분석가 → 종목발굴가 → 종목평가가 → 자산운용가 → 매도실행가 → 매수실행가
→ 투자성향관리자.update_strategy()  ← 장 마감 후 레벨 조정
```

### Night (kitty_night/main.py)
```
NightSectorAnalyst → NightStockPicker → NightStockEvaluator
→ NightAssetManager → NightSellExecutor → NightBuyExecutor
→ TendencyAgent.update_strategy()
```

## BaseAgent 패턴

모든 에이전트는 `BaseAgent`를 상속한다.

```python
class BaseAgent(ABC):
    def __init__(self, name, system_prompt):
        # _build_system_prompt(): base + feedback/store.py 의 피드백 자동 주입
    async def think(self, user_message) -> str:  # 대화 누적형 AI 호출
    async def run(self, context) -> dict:         # 에이전트 핵심 로직 (추상)
    def _record_tokens(self, in_t, out_t)         # token_usage/YYYY-MM-DD.json 기록
```

**피드백 자동 주입 흐름**:
`feedback/{에이전트명}.json` → `get_feedback_prompt()` → `system_prompt` 끝에 append
→ 저장된 개선사항이 다음 사이클부터 에이전트 판단에 자동 반영됨

## KIS API 핵심 규칙

### 거래소 코드 — 3자리 vs 4자리 혼재 주의
| 구분 | 형식 | 사용처 |
|---|---|---|
| 시세 조회 | 3자리 (`NAS`, `NYS`, `AMS`) | `get_quote()` |
| 주문/잔고 | 4자리 (`NASD`, `NYSE`, `AMEX`) | `buy()`, `sell()`, `get_balance()` |

`kitty_night/broker/kis_overseas.py`에 `_to_order_excd()` 변환 헬퍼가 있음.

### `rt_cd` 묵시적 실패
KIS API는 비즈니스 오류도 HTTP 200으로 반환. **반드시 `rt_cd == "0"` 체크**.
```python
if data.get("rt_cd") != "0":
    logger.error(f"API 실패: {data.get('msg1')}")
    return 빈_결과
```

### 해외 잔고 조회
`OVRS_EXCG_CD: ""` (빈 문자열) = 전 거래소 조회. 특정 코드 지정 시 해당 거래소만 반환.

## 투자성향관리자 (TendencyAgent)

5개 차원 × 6단계 레벨로 투자 전략을 결정:

| 차원 | L1 (공격적) | L6 (보수적) |
|---|---|---|
| take_profit | +5% | +35% |
| stop_loss | -1.5% | -10% |
| cash | 10% | 60% |
| max_weight | 35% | 10% |
| entry | +6% | +0.5% |

- 상태: `logs/tendency_state.json`
- 지침 생성: `_build_directive()` → 에이전트들에게 주입
- 업데이트: 장 마감 후 `update_strategy(eval_results)` AI 호출

## 피드백 시스템

```
성과 평가 → feedback/{에이전트}.json에 append
         → BaseAgent 초기화 시 system_prompt에 자동 주입
```

**파일 위치**:
- KR: `feedback/` (Docker: `/app/feedback/`, Monitor: `/feedback/`)
- Night: `night-feedback/` (Docker: `/app/night-feedback/`, Monitor: `/night-feedback/`)

**엔트리 구조**:
```json
{"date": "2026-04-09", "score": 75, "summary": "...", "improvement": "...", "good_pattern": "..."}
```

최근 14일 보관, 최근 5일이 system_prompt에 주입됨.

## 포트폴리오 스냅샷

`utils/portfolio.py` → `logs/portfolio_snapshot.json` (KR)
`kitty_night/utils/portfolio.py` → `night-logs/night_portfolio_snapshot.json`

Monitor가 읽어 대시보드에 표시. 필드: `ts`, `trading_mode`, `available_cash`, `total_eval`, `total_pnl`, `holdings`

## 텔레그램 알람

- KR: `kitty/telegram/bot.py` → `report_trade(action, symbol, qty, price, reason, name="")`
- Night: `kitty_night/telegram/bot.py` → 동일 시그니처 (price는 float USD)
- 시장가 주문: `price=0` → quote_map 참조가격 fallback 또는 "시장가" 표시

## 모니터 (monitor/app.py)

**단일 파일** (Python FastAPI + HTML/CSS/JS 임베드 `_HTML = r"""..."""`)

주요 API:
- `GET /api/agent-scores` — 에이전트 일별 점수 (feedback/*.json)
- `GET /api/agent-feedback` — 에이전트별 개선 피드백 리스트
- `POST /api/agent-feedback/add` — 개선 피드백 추가 (자동 프롬프트 강화)
- `POST /api/tendency-advisor` — 성향관리자 AI 채팅
- `POST /api/chat` — 에이전트 채팅 (commands/ 파일 경유, trader 컨테이너가 처리)
- `POST /api/set-mode` — paper ↔ live 전환

**탭 구조**: 🤖 성적표 / 📒 매매일지 / ⚙️ 관리 (📋 에러 / 🔢 토큰 / 🧠 성향관리)

## Docker 볼륨 맵

| 호스트 | kitty-trader | kitty-night-trader | kitty-monitor |
|---|---|---|---|
| `feedback/` | `/app/feedback` (rw) | — | `/feedback` (rw) |
| `night-feedback/` | — | `/app/night-feedback` (rw) | `/night-feedback` (rw) |
| `logs/` | `/app/logs` (rw) | — | `/logs` (ro) |
| `night-logs/` | — | `/app/night-logs` (rw) | `/night-logs` (ro) |
| `commands/` | `/app/commands` (rw) | — | `/commands` (rw) |

## 개발 패턴 & 주의사항

### 에이전트 추가/수정 시
1. `BaseAgent` 상속, `run(context) -> dict` 구현
2. `main.py` 파이프라인에 추가
3. `monitor/app.py`의 `AGENTS` 목록 및 `_AGENT_PROMPTS` 딕셔너리에 추가
4. 텔레그램 알람이 필요하면 `reporter.report_trade()` 호출

### KIS API 응답 처리
- `output1`: 개별 항목 리스트
- `output2`: 요약 (잔고 합계 등)
- 항상 `rt_cd` 체크, 빈 리스트 방어 처리 필수

### 시간대
- 모든 KST 시간은 `ZoneInfo("Asia/Seoul")` 사용
- Night 트레이더는 미국 시장 시간 기준 (MarketPhase로 자동 판별)

### 모니터 HTML 편집
`_HTML = r"""..."""` raw string 안에 있음.
CSS → JS → HTML 순으로 작성됨. `switchAdmin()`, `switchMain()` 함수가 탭 전환 담당.

### 배포 전 체크리스트
- `python3 -c "import ast; ast.parse(open('monitor/app.py').read().split('_HTML')[0])"` — 문법 검증
- `git push origin main` 후 EC2에서 `bash start.sh`
- feedback 마운트는 rw (모니터에서 성향관리 탭으로 피드백 저장 가능)

## 자주 하는 작업

```bash
# EC2 배포
EC2_IP=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=kitty-trader" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text --region ap-northeast-2)
ssh -i ~/kitty-key.pem ec2-user@$EC2_IP "cd ~/kitty && git pull && bash start.sh"

# 컨테이너 로그 확인
ssh -i ~/kitty-key.pem ec2-user@$EC2_IP "docker logs kitty-trader --tail 50"
ssh -i ~/kitty-key.pem ec2-user@$EC2_IP "docker logs kitty-night-trader --tail 50"
ssh -i ~/kitty-key.pem ec2-user@$EC2_IP "docker logs kitty-monitor --tail 20"

# 모니터 접속
# http://<EC2_IP>:8080  (Basic Auth: MONITOR_PASSWORD in Secrets Manager)
```
