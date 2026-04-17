"""KRX 공개 데이터 활용 한국 시장 데이터 수집 (pykrx)

API 키 불필요. pykrx 라이브러리가 KRX 공개 통계를 래핑.
- 데이터 기준: 직전 완료 거래일 (pykrx는 일별 데이터만 지원, 인트라데이 미지원)
  → 장 중(~15:30)이면 전일 데이터, 장 마감(15:35~) 후면 당일 데이터 사용
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import holidays

from kitty.utils import logger

_KST = ZoneInfo("Asia/Seoul")
_kr_holidays = holidays.KR()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pykrx")

# KRX KOSPI 업종 지수 코드 → 섹터명 매핑
# (sector_analyst.py의 섹터 분류와 일치)
_SECTOR_INDEX_MAP: dict[str, str] = {
    "1036": "반도체/전자",    # 전기전자 — 삼성전자, SK하이닉스
    "1038": "자동차/모빌리티", # 운수장비 — 현대차, 기아
    "1031": "2차전지/화학",   # 화학    — LG에너지솔루션, 삼성SDI, 에코프로비엠
    "1032": "바이오/의약품",  # 의약품  — 셀트리온, 유한양행
    "1037": "의료정밀",       # 의료정밀 — 삼성바이오로직스
    "1048": "인터넷/서비스",  # 서비스업 — NAVER, 카카오
    "1044": "금융",           # 금융업  — KB금융, 신한지주
    "1041": "건설/인프라",    # 건설업  — 현대건설, 삼성물산
}


def _get_last_trading_date() -> str:
    """직전 완료 거래일 반환 (장 마감 후면 오늘, 장 중이면 전일)"""
    from datetime import datetime
    now = datetime.now(_KST)
    is_after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 35)
    candidate = now.date() if is_after_close else now.date() - timedelta(days=1)
    for _ in range(7):
        if candidate.weekday() < 5 and candidate not in _kr_holidays:
            return candidate.strftime("%Y%m%d")
        candidate -= timedelta(days=1)
    return now.strftime("%Y%m%d")


def _get_prev_trading_date(base_date_str: str) -> str:
    """base_date 직전 거래일 반환 (전일 종가 계산용)"""
    base = date(int(base_date_str[:4]), int(base_date_str[4:6]), int(base_date_str[6:8]))
    candidate = base - timedelta(days=1)
    for _ in range(7):
        if candidate.weekday() < 5 and candidate not in _kr_holidays:
            return candidate.strftime("%Y%m%d")
        candidate -= timedelta(days=1)
    return base_date_str


def _fetch_sync() -> dict[str, Any]:
    """동기: pykrx로 업종 지수 + 외국인/기관 순매수 조회"""
    from pykrx import stock  # 로컬 임포트 — 미설치 시 graceful fallback

    target_date = _get_last_trading_date()
    prev_date = _get_prev_trading_date(target_date)

    # ── 1. KRX 업종 지수 등락률 ───────────────────────────────────────────────
    sector_indices: list[dict] = []
    for ticker, sector_name in _SECTOR_INDEX_MAP.items():
        try:
            df = stock.get_index_ohlcv(prev_date, target_date, ticker)
            if len(df) < 2:
                continue
            prev_close = float(df["종가"].iloc[-2])
            today_close = float(df["종가"].iloc[-1])
            if prev_close <= 0:
                continue
            change_rate = round((today_close - prev_close) / prev_close * 100, 2)
            sector_indices.append({
                "ticker":      ticker,
                "sector":      sector_name,
                "close":       int(today_close),
                "change_rate": change_rate,
                "date":        target_date,
            })
        except Exception as e:
            logger.debug(f"[KRMarket] 업종지수 {ticker}({sector_name}) 조회 실패: {e}")

    # ── 2. 외국인 순매수 상위 10종목 (KOSPI) ─────────────────────────────────
    foreign_net: list[dict] = []
    try:
        df = stock.get_market_net_purchases_of_investors(
            target_date, target_date, "KOSPI", "외국인합계"
        )
        if not df.empty:
            df = df.sort_values("순매수거래대금", ascending=False)
            for code, row in df.head(10).iterrows():
                net_val = int(row.get("순매수거래대금", 0))
                if net_val == 0:
                    continue
                foreign_net.append({
                    "symbol":       str(code),
                    "net_buy_krw":  net_val,          # 순매수거래대금 (원)
                })
    except Exception as e:
        logger.debug(f"[KRMarket] 외국인 순매수 조회 실패: {e}")

    # ── 3. 기관 순매수 상위 10종목 (KOSPI) ───────────────────────────────────
    inst_net: list[dict] = []
    try:
        df = stock.get_market_net_purchases_of_investors(
            target_date, target_date, "KOSPI", "기관합계"
        )
        if not df.empty:
            df = df.sort_values("순매수거래대금", ascending=False)
            for code, row in df.head(10).iterrows():
                net_val = int(row.get("순매수거래대금", 0))
                if net_val == 0:
                    continue
                inst_net.append({
                    "symbol":      str(code),
                    "net_buy_krw": net_val,
                })
    except Exception as e:
        logger.debug(f"[KRMarket] 기관 순매수 조회 실패: {e}")

    return {
        "date":           target_date,
        "sector_indices": sector_indices,
        "foreign_net":    foreign_net,
        "inst_net":       inst_net,
    }


async def fetch_kr_market_data() -> dict[str, Any]:
    """비동기 래퍼: KRX 업종 지수 + 외국인/기관 순매수 반환

    반환:
        {
            "date": "20260417",
            "sector_indices": [
                {"ticker": "1036", "sector": "반도체/전자",
                 "close": 1234567, "change_rate": +1.23, "date": "20260417"},
                ...
            ],
            "foreign_net": [
                {"symbol": "005930", "net_buy_krw": 50_000_000_000},  # 순매수
                ...
            ],
            "inst_net": [
                {"symbol": "000660", "net_buy_krw": 30_000_000_000},
                ...
            ],
        }
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(_executor, _fetch_sync)
        logger.info(
            f"[KRMarket] 업종지수 {len(result.get('sector_indices', []))}개 | "
            f"외국인순매수 {len(result.get('foreign_net', []))}종목 | "
            f"기관순매수 {len(result.get('inst_net', []))}종목 "
            f"(기준일: {result.get('date', '?')})"
        )
        return result
    except Exception as e:
        logger.warning(f"[KRMarket] 데이터 조회 실패 (무시): {e}")
        return {"date": "", "sector_indices": [], "foreign_net": [], "inst_net": []}
