"""에이전트 기본 동작 테스트"""
import pytest
from unittest.mock import AsyncMock, patch


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
