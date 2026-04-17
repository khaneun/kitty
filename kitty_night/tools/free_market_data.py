"""무료 Yahoo Finance API를 활용한 미국 시장 데이터 수집

API 키 불필요. yfinance 라이브러리 사용.
- SPDR 섹터 ETF 11종: 각 섹터의 당일 등락률 직접 제공
- VIX 공포지수: 시장 리스크 수준 판단
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from kitty_night.utils import logger

# SPDR 섹터 ETF → 섹터명 매핑 (NightSectorAnalyst의 SECTOR MAP과 일치)
SECTOR_ETFS: dict[str, str] = {
    "XLK":  "Technology",
    "XLC":  "Communication",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLE":  "Energy",
    "XLY":  "Consumer Discretionary",
    "XLP":  "Consumer Staples",
    "XLI":  "Industrials",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
}

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yfinance")


def _fetch_sync() -> dict[str, Any]:
    """동기: yfinance로 섹터 ETF + VIX 시세 일괄 조회"""
    import yfinance as yf  # 로컬 임포트 — 미설치 시 graceful fallback

    all_symbols = list(SECTOR_ETFS.keys()) + ["^VIX"]
    tickers = yf.Tickers(" ".join(all_symbols))

    sector_etfs: list[dict] = []
    for sym, sector_name in SECTOR_ETFS.items():
        try:
            fi = tickers.tickers[sym].fast_info
            last_price = float(fi.last_price or 0)
            prev_close = float(fi.previous_close or 0)
            if last_price <= 0 or prev_close <= 0:
                continue
            change_rate = round((last_price - prev_close) / prev_close * 100, 2)
            sector_etfs.append({
                "symbol":      sym,
                "sector":      sector_name,
                "price":       round(last_price, 2),
                "prev_close":  round(prev_close, 2),
                "change_rate": change_rate,
            })
        except Exception as e:
            logger.debug(f"[FreeMarket] {sym} 조회 실패: {e}")

    vix: dict = {}
    try:
        fi_vix = tickers.tickers["^VIX"].fast_info
        vix_val = float(fi_vix.last_price or fi_vix.previous_close or 0)
        if vix_val > 0:
            vix = {
                "value": round(vix_val, 2),
                "level": "high" if vix_val > 25 else ("low" if vix_val < 15 else "medium"),
            }
    except Exception as e:
        logger.debug(f"[FreeMarket] VIX 조회 실패: {e}")

    return {"sector_etfs": sector_etfs, "vix": vix}


async def fetch_free_market_data() -> dict[str, Any]:
    """비동기 래퍼: 섹터 ETF + VIX 데이터 반환

    반환:
        {
            "sector_etfs": [
                {"symbol": "XLK", "sector": "Technology",
                 "price": 195.3, "prev_close": 193.1, "change_rate": +1.14},
                ...
            ],
            "vix": {"value": 18.5, "level": "medium"},
        }
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(_executor, _fetch_sync)
        logger.info(
            f"[FreeMarket] 섹터 ETF {len(result.get('sector_etfs', []))}개 | "
            f"VIX {result.get('vix', {}).get('value', 'N/A')}"
        )
        return result
    except Exception as e:
        logger.warning(f"[FreeMarket] 데이터 조회 실패 (무시): {e}")
        return {"sector_etfs": [], "vix": {}}
