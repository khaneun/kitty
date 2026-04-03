"""미국 시장 캘린더 — 거래시간, 서머타임, 휴장일 처리

NYSE/NASDAQ 정규장: 09:30~16:00 ET (America/New_York)
서머타임(EDT): 3월 둘째 일요일 ~ 11월 첫째 일요일 → KST 기준 1시간 차이

KST 기준:
  - EDT(여름): 정규장 22:30~05:00 KST
  - EST(겨울): 정규장 23:30~06:00 KST
"""
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

_ET = ZoneInfo("America/New_York")
_KST = ZoneInfo("Asia/Seoul")

# NYSE 캘린더 (NASDAQ과 동일한 휴장일)
_NYSE = xcals.get_calendar("XNYS")


class MarketPhase(str, Enum):
    """Night mode 시장 구간"""
    CLOSED = "closed"             # 비활성 (KR장 시간대)
    WAITING = "waiting"           # 대기 (KR장 종료 ~ 야간 분석 시작 전)
    PRE_MARKET = "pre_market"     # 장전 분석 (주문 없음)
    MARKET = "market"             # 정규장 (분석 + 주문)
    POST_MARKET = "post_market"   # 장후 평가


def now_kst() -> datetime:
    return datetime.now(_KST)


def now_et() -> datetime:
    return datetime.now(_ET)


def is_dst() -> bool:
    """현재 미국 서머타임(EDT) 여부"""
    return bool(datetime.now(_ET).dst())


def us_market_date(kst_dt: datetime | None = None) -> date:
    """KST 시각 기준으로 해당하는 미국 거래일 반환.
    KST 새벽(06:00 이전)이면 전날 미국 거래일에 해당.
    """
    if kst_dt is None:
        kst_dt = now_kst()
    et_dt = kst_dt.astimezone(_ET)
    return et_dt.date()


def is_us_holiday(d: date | None = None) -> bool:
    """NYSE 휴장일 여부"""
    if d is None:
        d = us_market_date()
    return not _NYSE.is_session(d.isoformat())


def get_market_hours_kst(d: date | None = None) -> tuple[datetime, datetime]:
    """미국 정규장 개장/종료 시각을 KST로 반환.
    Returns:
        (open_kst, close_kst) — 둘 다 KST aware datetime
    """
    if d is None:
        d = us_market_date()
    open_et = datetime.combine(d, time(9, 30), tzinfo=_ET)
    close_et = datetime.combine(d, time(16, 0), tzinfo=_ET)
    return open_et.astimezone(_KST), close_et.astimezone(_KST)


def get_market_phase(kst_dt: datetime | None = None) -> MarketPhase:
    """현재 KST 시각 기준 Night mode 시장 구간 판별.

    시간대 흐름 (KST 기준, EDT 여름):
      08:40~15:35  CLOSED      (한국장 시간, kitty 동작 구간)
      15:35~21:00  WAITING     (한국장 종료 ~ 야간 분석 대기)
      21:00~22:30  PRE_MARKET  (장전 분석, 주문 없음)
      22:30~05:00  MARKET      (정규장, 분석 + 주문)
      05:00~06:00  POST_MARKET (장후 평가)
      06:00~08:40  CLOSED      (야간 종료 ~ 한국장 시작 전)
    """
    if kst_dt is None:
        kst_dt = now_kst()

    # 오늘/내일 미국 거래일 확인
    trading_date = us_market_date(kst_dt)
    if is_us_holiday(trading_date):
        return MarketPhase.CLOSED

    open_kst, close_kst = get_market_hours_kst(trading_date)

    # 장전 분석 시작: 개장 90분 전
    pre_market_start = open_kst - timedelta(minutes=90)
    # 장후 평가 종료: 종료 60분 후
    post_market_end = close_kst + timedelta(minutes=60)

    # 한국장 시간대 (08:40~15:35 KST)
    kr_open = kst_dt.replace(hour=8, minute=40, second=0, microsecond=0)
    kr_close = kst_dt.replace(hour=15, minute=35, second=0, microsecond=0)

    if kr_open <= kst_dt < kr_close:
        return MarketPhase.CLOSED

    if kst_dt < pre_market_start:
        # 아직 장전 분석 시작 전
        if kst_dt.hour < 7:
            # 새벽 — 어제 POST_MARKET 이후일 수 있음
            yesterday = trading_date - timedelta(days=1)
            if not is_us_holiday(yesterday):
                _, yclose = get_market_hours_kst(yesterday)
                ypost_end = yclose + timedelta(minutes=60)
                if kst_dt < ypost_end:
                    return MarketPhase.POST_MARKET
        return MarketPhase.WAITING

    if pre_market_start <= kst_dt < open_kst:
        return MarketPhase.PRE_MARKET

    if open_kst <= kst_dt < close_kst:
        return MarketPhase.MARKET

    if close_kst <= kst_dt < post_market_end:
        return MarketPhase.POST_MARKET

    return MarketPhase.WAITING


def next_market_open_kst() -> datetime:
    """다음 미국 정규장 개장 시각 (KST)"""
    d = us_market_date()
    for _ in range(10):
        if not is_us_holiday(d):
            open_kst, _ = get_market_hours_kst(d)
            if open_kst > now_kst():
                return open_kst
        d += timedelta(days=1)
    # fallback
    return now_kst() + timedelta(hours=12)


def seconds_until(target_kst: datetime) -> float:
    """target_kst까지 남은 초 (음수면 0 반환)"""
    return max(0, (target_kst - now_kst()).total_seconds())
