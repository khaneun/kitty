"""
해외주식 주문가능금액(get_available_usd) 조회 테스트

단위 테스트: 모킹으로 응답 구조·엣지케이스 검증
통합 테스트: 실제 KIS API 호출 (자격증명 필요 시 자동 실행)

실행:
  # 단위 테스트만
  python -m pytest tests/test_night_available_cash.py -v -k "not live"

  # 통합 테스트 포함 (KIS API 자격증명 필요)
  NIGHT_KIS_PAPER_APP_KEY=... python -m pytest tests/test_night_available_cash.py -v -s
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 공통 픽스처 ─────────────────────────────────────────────────────────────────

def _mock_resp(rt_cd: str, output) -> MagicMock:
    """KIS API 모의 응답 생성 헬퍼"""
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"rt_cd": rt_cd, "msg1": "정상처리", "output": output}
    return m


# ── 단위 테스트 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_correct_amount():
    """정상 응답에서 ovrs_ord_psbl_amt 값을 float으로 반환"""
    mock_resp = _mock_resp("0", {"ovrs_ord_psbl_amt": "5432.10", "tr_crcy_cd": "USD"})

    with patch("kitty_night.broker.kis_overseas.KISOverseasBroker._get_token", new_callable=AsyncMock, return_value="tok"), \
         patch("kitty_night.broker.kis_overseas.KISOverseasBroker._call_with_retry", new_callable=AsyncMock, return_value=mock_resp):
        from kitty_night.broker.kis_overseas import KISOverseasBroker
        broker = KISOverseasBroker()
        result = await broker.get_available_usd()

    assert result == pytest.approx(5432.10), f"got {result}"


@pytest.mark.asyncio
async def test_returns_zero_on_api_error():
    """rt_cd != '0' (API 비즈니스 에러) 시 0.0 반환"""
    mock_resp = _mock_resp("1", {})
    mock_resp.json.return_value["msg1"] = "계좌 정보 오류"

    with patch("kitty_night.broker.kis_overseas.KISOverseasBroker._get_token", new_callable=AsyncMock, return_value="tok"), \
         patch("kitty_night.broker.kis_overseas.KISOverseasBroker._call_with_retry", new_callable=AsyncMock, return_value=mock_resp):
        from kitty_night.broker.kis_overseas import KISOverseasBroker
        broker = KISOverseasBroker()
        result = await broker.get_available_usd()

    assert result == 0.0


@pytest.mark.asyncio
async def test_returns_zero_on_missing_field():
    """output은 성공이지만 ovrs_ord_psbl_amt 필드 없을 때 0.0 반환"""
    mock_resp = _mock_resp("0", {"tr_crcy_cd": "USD"})  # 금액 필드 없음

    with patch("kitty_night.broker.kis_overseas.KISOverseasBroker._get_token", new_callable=AsyncMock, return_value="tok"), \
         patch("kitty_night.broker.kis_overseas.KISOverseasBroker._call_with_retry", new_callable=AsyncMock, return_value=mock_resp):
        from kitty_night.broker.kis_overseas import KISOverseasBroker
        broker = KISOverseasBroker()
        result = await broker.get_available_usd()

    assert result == 0.0


@pytest.mark.asyncio
async def test_returns_zero_on_empty_string_field():
    """ovrs_ord_psbl_amt가 빈 문자열("")일 때 0.0 반환"""
    mock_resp = _mock_resp("0", {"ovrs_ord_psbl_amt": "", "tr_crcy_cd": "USD"})

    with patch("kitty_night.broker.kis_overseas.KISOverseasBroker._get_token", new_callable=AsyncMock, return_value="tok"), \
         patch("kitty_night.broker.kis_overseas.KISOverseasBroker._call_with_retry", new_callable=AsyncMock, return_value=mock_resp):
        from kitty_night.broker.kis_overseas import KISOverseasBroker
        broker = KISOverseasBroker()
        result = await broker.get_available_usd()

    assert result == 0.0


@pytest.mark.asyncio
async def test_output_as_list_does_not_raise():
    """output이 list인 경우에도 0.0 반환 (AttributeError로 크래시 없어야 함)

    KIS API 일부 엔드포인트는 output을 list로 반환하는 경우가 있음.
    현재 get_available_usd() 코드: output.get(...) → list는 .get() 없음 → AttributeError 가능성.
    이 테스트가 실패하면 kis_overseas.py 방어 처리 필요.
    """
    mock_resp = _mock_resp("0", [{"ovrs_ord_psbl_amt": "3000.00"}])  # list로 반환

    with patch("kitty_night.broker.kis_overseas.KISOverseasBroker._get_token", new_callable=AsyncMock, return_value="tok"), \
         patch("kitty_night.broker.kis_overseas.KISOverseasBroker._call_with_retry", new_callable=AsyncMock, return_value=mock_resp):
        from kitty_night.broker.kis_overseas import KISOverseasBroker
        broker = KISOverseasBroker()
        try:
            result = await broker.get_available_usd()
            # list인 경우 .get()이 없으므로 0.0 이 아닌 오류 또는 0.0 반환
            print(f"\n  list output → result: {result}")
        except AttributeError as e:
            pytest.fail(
                f"output이 list일 때 AttributeError 발생: {e}\n"
                "kis_overseas.py get_available_usd()에 방어처리 필요:\n"
                "  output = data.get('output', {})\n"
                "  if isinstance(output, list): output = output[0] if output else {}"
            )


# ── 통합 테스트 (실제 KIS API 자격증명 필요) ────────────────────────────────────

_HAS_CREDS = bool(
    os.getenv("NIGHT_KIS_PAPER_APP_KEY")
    or os.getenv("NIGHT_KIS_APP_KEY")
    or os.getenv("KIS_APP_KEY")
)


@pytest.mark.skipif(not _HAS_CREDS, reason="KIS API 자격증명 없음 — NIGHT_KIS_PAPER_APP_KEY 설정 필요")
@pytest.mark.asyncio
async def test_get_available_usd_live():
    """실제 KIS API 호출 — 응답 구조 및 값 확인 (자격증명 있을 때만 실행)"""
    from kitty_night.broker.kis_overseas import KISOverseasBroker

    broker = KISOverseasBroker()
    try:
        result = await broker.get_available_usd()
        print(f"\n[live] get_available_usd() = ${result:,.2f}")
        assert isinstance(result, float), f"float이어야 함, 실제: {type(result)}"
        assert result >= 0.0, f"음수 불가: {result}"
    finally:
        await broker.close()


@pytest.mark.skipif(not _HAS_CREDS, reason="KIS API 자격증명 없음")
@pytest.mark.asyncio
async def test_raw_api_response_live():
    """실제 API 응답 필드 전체 출력 — 필드명 확인용 디버그 테스트

    Note: inquire-psamount는 유효한 ITEM_CD 필수 (특히 모의투자).
    ITEM_CD="" → rt_cd=1, 모의투자 종목코드정보를 확인하세요 에러.
    AAPL/NASD/200 기준으로 호출하면 ovrs_ord_psbl_amt = 계좌 전체 가용 USD 현금.
    """
    from kitty_night.broker.kis_overseas import KISOverseasBroker, _BUYABLE_TR

    broker = KISOverseasBroker()
    try:
        tr_id = _BUYABLE_TR[broker._mode]
        headers = await broker._headers(tr_id)

        resp = await broker._client.get(
            f"{broker._base_url}/uapi/overseas-stock/v1/trading/inquire-psamount",
            headers=headers,
            params={
                "CANO": broker._cano,
                "ACNT_PRDT_CD": broker._acnt_prdt_cd,
                "OVRS_EXCG_CD": "NASD",
                "OVRS_ORD_UNPR": "200",  # 기준가격 (실제 계산과 무관)
                "ITEM_CD": "AAPL",       # 유효 종목코드 필수
            },
        )
        data = resp.json()
        print(f"\n[raw API] mode={broker._mode}, tr_id={tr_id}")
        print(f"  rt_cd={data.get('rt_cd')}, msg1={data.get('msg1')}")
        print(f"  output type={type(data.get('output'))}")
        print(f"  output={data.get('output')}")

        assert data.get("rt_cd") == "0", (
            f"API 응답 실패: rt_cd={data.get('rt_cd')}, msg1={data.get('msg1')}\n"
            f"  → tr_id={tr_id}, mode={broker._mode}\n"
            f"  → ITEM_CD=AAPL, OVRS_ORD_UNPR=200으로 호출"
        )

        output = data.get("output", {})
        if isinstance(output, list):
            print(f"  output이 list임. output[0]={output[0] if output else 'empty'}")
        else:
            print(f"  output keys: {list(output.keys())}")
            amt = output.get("ovrs_ord_psbl_amt", "FIELD_NOT_FOUND")
            print(f"  ovrs_ord_psbl_amt={amt}")

    finally:
        await broker.close()
