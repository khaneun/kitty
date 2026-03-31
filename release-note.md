# Release Notes

## 2026-04-01

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

### 개선

**EC2 부팅 시 최신 코드 자동 반영** (`start.sh`)

기존 `start.sh`는 `git pull` 없이 로컬 파일로 Docker 빌드를 수행해,
코드 수정 후 `git push`만으로는 다음 날 자동 부팅에 반영되지 않는 문제가 있었음.
`start.sh` 첫 단계에 `git pull origin main`을 추가하고 파일을 repo에 포함.
이제 `git push` 후 다음 EC2 부팅 시 자동으로 최신 코드가 반영됨.
