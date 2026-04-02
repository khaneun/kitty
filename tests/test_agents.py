"""에이전트 기본 동작 테스트"""
import pytest
import httpx
from unittest.mock import AsyncMock, Mock, patch


@pytest.mark.asyncio
async def test_market_analyst_returns_json():
    """시장분석가가 JSON 구조를 반환하는지 확인"""
    mock_response = '{"market_sentiment": "bullish", "target_stocks": [], "risk_level": "low", "summary": "테스트"}'

    with patch("anthropic.AsyncAnthropic") as mock_client:
        mock_msg = AsyncMock()
        mock_msg.content = [AsyncMock(text=mock_response)]
        mock_client.return_value.messages.create = AsyncMock(return_value=mock_msg)

        from kitty.agents.market_analyst import MarketAnalystAgent
        agent = MarketAnalystAgent()
        result = await agent.run({"quotes": [], "portfolio": {}})

    assert result["market_sentiment"] == "bullish"
    assert result["risk_level"] == "low"


@pytest.mark.asyncio
async def test_strategist_no_decision_on_high_risk():
    """HIGH 리스크 시장에서 매수 결정이 없는지 확인"""
    mock_response = '{"decisions": [], "strategy_summary": "리스크 높아 관망"}'

    with patch("anthropic.AsyncAnthropic") as mock_client:
        mock_msg = AsyncMock()
        mock_msg.content = [AsyncMock(text=mock_response)]
        mock_client.return_value.messages.create = AsyncMock(return_value=mock_msg)

        from kitty.agents.strategist import StrategistAgent
        agent = StrategistAgent()
        result = await agent.run({
            "analysis": {"market_sentiment": "bearish", "risk_level": "high", "target_stocks": []},
            "portfolio": {},
            "available_cash": 1_000_000,
            "max_buy_amount": 500_000,
        })

    assert result["decisions"] == []


# ── 주가 조회 테스트 ──────────────────────────────────────────────────

def _mock_token_resp() -> Mock:
    """토큰 발급 mock 응답"""
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": "test-token"}
    resp.raise_for_status = Mock()
    return resp


@pytest.mark.asyncio
async def test_get_quote_success():
    """주가 조회 성공 — 200 응답 시 StockQuote 반환"""
    quote_resp = Mock()
    quote_resp.status_code = 200
    quote_resp.raise_for_status = Mock()
    quote_resp.json.return_value = {
        "output": {
            "hts_kor_isnm": "삼성전자",
            "stck_prpr": "75000",
            "prdy_ctrt": "1.35",
            "acml_vol": "15000000",
        }
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_token_resp()
    mock_client.get.return_value = quote_resp

    with patch("kitty.broker.kis.settings") as mock_settings, \
         patch("kitty.broker.kis.httpx.AsyncClient", return_value=mock_client):
        mock_settings.active_kis_base_url = "https://mock-api"
        mock_settings.active_kis_app_key = "test-key"
        mock_settings.active_kis_app_secret = "test-secret"

        from kitty.broker.kis import KISBroker
        broker = KISBroker()
        result = await broker.get_quote("005930")

    assert result.symbol == "005930"
    assert result.name == "삼성전자"
    assert result.current_price == 75000
    assert result.change_rate == 1.35
    assert result.volume == 15_000_000


@pytest.mark.asyncio
async def test_get_quote_500_error():
    """주가 조회 시 500 에러 — HTTPStatusError 발생"""
    error_resp = Mock()
    error_resp.status_code = 500
    error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=Mock(),
        response=error_resp,
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = _mock_token_resp()
    mock_client.get.return_value = error_resp

    with patch("kitty.broker.kis.settings") as mock_settings, \
         patch("kitty.broker.kis.httpx.AsyncClient", return_value=mock_client):
        mock_settings.active_kis_base_url = "https://mock-api"
        mock_settings.active_kis_app_key = "test-key"
        mock_settings.active_kis_app_secret = "test-secret"

        from kitty.broker.kis import KISBroker
        broker = KISBroker()
        with pytest.raises(httpx.HTTPStatusError):
            await broker.get_quote("005930")
