"""KIS 해외주식 API 래퍼 — 미국 시장 중심, 확장 가능 구조

KIS Developers 해외주식 API 문서:
https://apiportal.koreainvestment.com/apiservice/overseas-stock

연속 주문 제한: 동일 종목 최소 1초 간격, 초당 최대 2건
"""
import asyncio
import random
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from kitty_night.config import night_settings
from kitty_night.utils import logger

_KST = ZoneInfo("Asia/Seoul")


def _sf(value: Any, default: float = 0.0) -> float:
    """KIS API 숫자 필드를 안전하게 float 변환 (빈 문자열/None 허용)"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# ── 모델 ──────────────────────────────────────────────────────────────────────

class OverseasQuote(BaseModel):
    symbol: str
    name: str
    excd: str              # NAS / NYS / AMS
    current_price: float   # USD
    change_rate: float     # 등락률 (%)
    volume: int
    currency: str = "USD"


class OverseasOrderResult(BaseModel):
    order_id: str
    symbol: str
    excd: str
    side: str              # BUY / SELL
    quantity: int
    price: float           # USD
    status: str
    timestamp: datetime


# ── 거래소 코드 변환 (3-char quote API → 4-char order/balance API) ─────────────

_EXCD_MAP: dict[str, str] = {
    "NAS": "NASD",
    "NYS": "NYSE",
    "AMS": "AMEX",
    "HKS": "SEHK",
    "TSE": "TKSE",
    "SHS": "SHAA",
    "SZS": "SZAA",
    "HSX": "VNSE",
}


def _to_order_excd(excd: str) -> str:
    """시세 조회용 3-char excd → 주문/잔고 API용 4-char excd 변환.
    이미 4-char이거나 알 수 없는 코드는 그대로 반환.
    """
    return _EXCD_MAP.get(excd.upper(), excd)


# ── TR 코드 ───────────────────────────────────────────────────────────────────

# 시세 조회 (실전/모의 공통)
_QUOTE_TR = "HHDFS76200200"

# 잔고 조회
_BALANCE_TR = {"live": "TTTS3012R", "paper": "VTTS3012R"}

# 주문 가능 금액
_BUYABLE_TR = {"live": "TTTS3007R", "paper": "VTTS3007R"}

# 매수
_BUY_TR = {"live": "TTTT1002U", "paper": "VTTT1002U"}

# 매도
_SELL_TR = {"live": "TTTT1006U", "paper": "VTTT1006U"}

# 체결 조회
_CCLD_TR = {"live": "TTTS3035R", "paper": "VTTS3035R"}

# 주문 취소/정정
_CANCEL_TR = {"live": "TTTT1004U", "paper": "VTTT1004U"}

# 거래량 순위 (실전/모의 공통)
_VOLUME_RANK_TR = "HHDFS76410000"


class KISOverseasBroker:
    """KIS 해외주식 API"""

    # 연속 주문 제한: 1.2초 간격 (규정 1초 + 0.2초 여유)
    _ORDER_INTERVAL = 1.2
    # 시세 조회 간격: 1.0초 (KIS 해외 API — 분당 ~55건으로 보수적 유지)
    _QUOTE_INTERVAL = 1.0

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: datetime = datetime.min
        self._client = httpx.AsyncClient(timeout=20.0, verify=False)
        self._last_order_ts: float = 0.0
        self._last_quote_ts: float = 0.0

    @property
    def _mode(self) -> str:
        return "live" if night_settings.is_live else "paper"

    @property
    def _base_url(self) -> str:
        return night_settings.active_kis_base_url

    @property
    def _cano(self) -> str:
        return night_settings.active_kis_account_number[:8]

    @property
    def _acnt_prdt_cd(self) -> str:
        return night_settings.active_kis_account_number[8:]

    # ── 인증 ─────────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._access_token and datetime.now() < self._token_expires_at:
            return self._access_token

        resp = await self._client.post(
            f"{self._base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": night_settings.active_kis_app_key,
                "appsecret": night_settings.active_kis_app_secret,
            },
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "KIS API 403 — 앱키 미등록이거나 토큰 요청 초과. 잠시 후 재시도."
            )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = datetime.now() + timedelta(hours=23)
        logger.info("[Night:KIS] 해외주식 토큰 발급 완료")
        return self._access_token  # type: ignore[return-value]

    async def _headers(self, tr_id: str) -> dict[str, str]:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "appkey": night_settings.active_kis_app_key,
            "appsecret": night_settings.active_kis_app_secret,
            "tr_id": tr_id,
            "Content-Type": "application/json; charset=utf-8",
        }

    # ── 중앙 재시도 헬퍼 ─────────────────────────────────────────────────────

    async def _call_with_retry(
        self,
        make_request,
        label: str,
        retries: int = 5,
    ) -> httpx.Response:
        """HTTP 재시도 래퍼.

        make_request: async callable() → httpx.Response
        - 429 레이트리밋: 30~40s 고정 대기 후 재시도
        - 500/503 서버 오류: 지수 백오프 (2s → 4s → 8s)
        - 네트워크 오류: 지수 백오프
        - 다른 4xx/5xx: 즉시 raise
        """
        last_exc: Exception = RuntimeError(f"[Night:KIS] API 재시도 초과: {label}")
        for attempt in range(retries):
            try:
                resp = await make_request()
                if resp.status_code == 429:
                    wait = 30.0 + random.uniform(0.0, 10.0)
                    logger.warning(
                        f"[Night:KIS] {label} 레이트리밋(429) — {wait:.0f}s 대기 후 재시도 "
                        f"({attempt + 1}/{retries})"
                    )
                    await asyncio.sleep(wait)
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP 429", request=resp.request, response=resp,
                    )
                    continue
                if resp.status_code in (500, 503):
                    wait = min((attempt + 1) * 2.0, 8.0) + random.uniform(0.0, 1.0)
                    logger.warning(
                        f"[Night:KIS] {label} 서버오류({resp.status_code}) — "
                        f"{wait:.1f}s 대기 ({attempt + 1}/{retries})"
                    )
                    await asyncio.sleep(wait)
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp,
                    )
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                wait = min((attempt + 1) * 2.0, 8.0) + random.uniform(0.0, 1.0)
                last_exc = e
                logger.warning(
                    f"[Night:KIS] {label} 네트워크 오류 ({attempt + 1}/{retries}) "
                    f"{wait:.1f}s 대기: {e}"
                )
                await asyncio.sleep(wait)
        raise last_exc

    # ── Throttle ─────────────────────────────────────────────────────────────

    async def _throttle_order(self) -> None:
        """연속 주문 간격 보장"""
        elapsed = time.monotonic() - self._last_order_ts
        wait = self._ORDER_INTERVAL - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_order_ts = time.monotonic()

    async def _throttle_quote(self) -> None:
        """시세 조회 간격 보장"""
        elapsed = time.monotonic() - self._last_quote_ts
        wait = self._QUOTE_INTERVAL - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_quote_ts = time.monotonic()

    # ── 시세 조회 ────────────────────────────────────────────────────────────

    async def get_quote(self, symbol: str, excd: str = "NAS") -> OverseasQuote:
        """해외주식 현재가 조회 (TR: HHDFS76200200)"""
        await self._throttle_quote()

        async def _req():
            headers = await self._headers(_QUOTE_TR)
            return await self._client.get(
                f"{self._base_url}/uapi/overseas-price/v1/quotations/price",
                headers=headers,
                params={"AUTH": "", "EXCD": excd, "SYMB": symbol},
            )

        resp = await self._call_with_retry(_req, f"{symbol} 시세조회")
        output = resp.json().get("output", {})
        return OverseasQuote(
            symbol=symbol,
            name=output.get("rsym", symbol),
            excd=excd,
            current_price=_sf(output.get("last")),
            change_rate=_sf(output.get("rate")),
            volume=int(_sf(output.get("tvol"))),
        )

    # ── 잔고 조회 ────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """해외주식 잔고 조회 — 정규화된 holdings 포함하여 반환

        반환 형식:
          {
            "holdings": [
              {
                "symbol": str, "name": str, "excd": str,
                "quantity": int, "avg_price": float, "current_price": float,
                "eval_amount": float, "pnl_amount": float, "pnl_rate": float,
              }, ...
            ],
            "output2": <원본 output2>,  # 계좌 요약 (필요시 참조)
          }
        """
        tr_id = _BALANCE_TR[self._mode]

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/inquire-balance",
                headers=headers,
                params={
                    "CANO": self._cano,
                    "ACNT_PRDT_CD": self._acnt_prdt_cd,
                    "OVRS_EXCG_CD": "",   # 전 거래소 조회 (빈 문자열 = all exchanges)
                    "TR_CRCY_CD": "USD",
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": "",
                },
            )

        resp = await self._call_with_retry(_req, "해외잔고조회")
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.error(
                f"[Night:KIS] 잔고조회 실패 rt_cd={data.get('rt_cd')} "
                f"msg={data.get('msg1')} msg_cd={data.get('msg_cd')}"
            )
            return {"holdings": [], "output2": []}

        # output1 → 정규화된 holdings 변환
        holdings: list[dict[str, Any]] = []
        for item in data.get("output1", []):
            qty = int(_sf(item.get("ovrs_cblc_qty")))
            if qty <= 0:
                continue
            avg_price     = _sf(item.get("pchs_avg_pric"))
            current_price = _sf(item.get("now_pric2")) or avg_price
            eval_amount   = _sf(item.get("ovrs_stck_evlu_amt"))
            pnl_amount    = _sf(item.get("frcr_evlu_pfls_amt"))
            pnl_rate      = _sf(item.get("evlu_pfls_rt"))
            # eval_amount가 0이면 현재가 × 수량으로 추정
            if eval_amount == 0 and current_price > 0:
                eval_amount = current_price * qty
            # pnl_amount가 0이면 (현재가 - 평균단가) × 수량으로 추정
            if pnl_amount == 0 and avg_price > 0:
                pnl_amount = (current_price - avg_price) * qty
            holdings.append({
                "symbol":        item.get("ovrs_pdno", ""),
                "name":          item.get("ovrs_item_name", ""),
                "excd":          item.get("ovrs_excg_cd", "NAS"),
                "quantity":      qty,
                "avg_price":     avg_price,
                "current_price": current_price,
                "eval_amount":   eval_amount,
                "pnl_amount":    pnl_amount,
                "pnl_rate":      pnl_rate,
            })

        logger.info(f"[Night:KIS] balance: {len(holdings)} holdings")
        return {
            "holdings": holdings,
            "output2":  data.get("output2", []),
        }

    async def get_available_usd(self) -> float:
        """해외주식 주문 가능 USD 금액 조회"""
        tr_id = _BUYABLE_TR[self._mode]

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/inquire-psamount",
                headers=headers,
                params={
                    "CANO": self._cano,
                    "ACNT_PRDT_CD": self._acnt_prdt_cd,
                    "OVRS_EXCG_CD": "NASD",
                    "OVRS_ORD_UNPR": "0",
                    "ITEM_CD": "",
                },
            )

        resp = await self._call_with_retry(_req, "해외주문가능금액")
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"[Night:KIS] available USD query failed: rt_cd={data.get('rt_cd')} msg={data.get('msg1')}")
            return 0.0
        output = data.get("output", {})
        # KIS API 일부 응답이 list로 오는 경우 방어 처리
        if isinstance(output, list):
            output = output[0] if output else {}
        usd = _sf(output.get("ovrs_ord_psbl_amt"))
        logger.info(f"[Night:KIS] available USD: ${usd:,.2f} (output keys: {list(output.keys()) if isinstance(output, dict) else 'list'})")
        return usd

    # ── 매수 ─────────────────────────────────────────────────────────────────

    async def _paper_aggressive_price(self, symbol: str, excd: str, side: str) -> float:
        """모의투자용 공격적 지정가 산출 — 현재가 기준 BUY +0.5%, SELL -0.5%
        시세 조회 실패 시 최대 2회 재시도 (지수 백오프).
        """
        for attempt in range(3):
            try:
                quote = await self.get_quote(symbol, excd)
                if side == "BUY":
                    return round(quote.current_price * 1.005, 2)
                else:
                    return round(quote.current_price * 0.995, 2)
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 3.0
                    logger.warning(
                        f"[Night:KIS] 모의투자 지정가 산출 실패 ({symbol}) "
                        f"— {wait:.0f}s 후 재시도 ({attempt + 1}/3): {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        f"[Night:KIS] 모의투자 지정가 산출 최종 실패 ({symbol}): {e}"
                    )
        return 0.0

    async def buy(
        self, symbol: str, excd: str, quantity: int,
        price: float = 0.0, name: str = "",
    ) -> OverseasOrderResult:
        """해외주식 매수 주문. price=0이면 시장가(01), 아니면 지정가(00).
        모의투자는 시장가 미지원 → 현재가 +0.5% 공격적 지정가로 자동 변환.
        연속 주문 제한 1.2초 간격 보장.
        """
        # 모의투자는 시장가 주문 미지원 → 공격적 지정가로 대체
        if price == 0.0 and self._mode == "paper":
            price = await self._paper_aggressive_price(symbol, excd, "BUY")
            logger.info(f"[Night:KIS] 모의투자 매수 시장가→지정가 대체: {symbol} @ ${price:.2f}")

        await self._throttle_order()
        tr_id = _BUY_TR[self._mode]
        ord_dvsn = "00" if price > 0 else "01"
        _label = f"{name}({symbol})" if name else symbol
        order_excd = _to_order_excd(excd)   # NAS → NASD 등 변환

        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": order_excd,
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/order",
                headers=headers, json=body,
            )

        resp = await self._call_with_retry(_req, f"매수 {_label}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1", str(data)))
        logger.info(f"[Night] BUY: {_label} {quantity}shares @ ${price:.2f} excd={order_excd}")
        return OverseasOrderResult(
            order_id=data["output"]["ODNO"],
            symbol=symbol, excd=excd,
            side="BUY", quantity=quantity, price=price,
            status="SUBMITTED",
            timestamp=datetime.now(_KST),
        )

    # ── 매도 ─────────────────────────────────────────────────────────────────

    async def sell(
        self, symbol: str, excd: str, quantity: int,
        price: float = 0.0, name: str = "",
    ) -> OverseasOrderResult:
        """해외주식 매도 주문.
        모의투자는 시장가 미지원 → 현재가 -0.5% 공격적 지정가로 자동 변환.
        """
        # 모의투자는 시장가 주문 미지원 → 공격적 지정가로 대체
        if price == 0.0 and self._mode == "paper":
            price = await self._paper_aggressive_price(symbol, excd, "SELL")
            if price <= 0.0:
                raise RuntimeError(
                    f"모의투자 매도 불가 ({symbol}): 시세 조회 실패로 지정가 산출 불가"
                )
            logger.info(f"[Night:KIS] 모의투자 매도 시장가→지정가 대체: {symbol} @ ${price:.2f}")

        await self._throttle_order()
        tr_id = _SELL_TR[self._mode]
        ord_dvsn = "00" if price > 0 else "01"
        _label = f"{name}({symbol})" if name else symbol
        order_excd = _to_order_excd(excd)   # NAS → NASD 등 변환

        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": order_excd,
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/order",
                headers=headers, json=body,
            )

        resp = await self._call_with_retry(_req, f"매도 {_label}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1", str(data)))
        logger.info(f"[Night] SELL: {_label} {quantity}shares @ ${price:.2f} excd={order_excd}")
        return OverseasOrderResult(
            order_id=data["output"]["ODNO"],
            symbol=symbol, excd=excd,
            side="SELL", quantity=quantity, price=price,
            status="SUBMITTED",
            timestamp=datetime.now(_KST),
        )

    # ── 체결 조회 ────────────────────────────────────────────────────────────

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """해외주식 체결 조회"""
        tr_id = _CCLD_TR[self._mode]
        today = datetime.now(_KST).strftime("%Y%m%d")

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl",
                headers=headers,
                params={
                    "CANO": self._cano,
                    "ACNT_PRDT_CD": self._acnt_prdt_cd,
                    "PDNO": "",
                    "ORD_STRT_DT": today,
                    "ORD_END_DT": today,
                    "SLL_BUY_DVSN": "00",
                    "CCLD_NCCS_DVSN": "00",
                    "OVRS_EXCG_CD": "",
                    "SORT_SQN": "DS",
                    "ORD_DT": "",
                    "ORD_GNO_BRNO": "",
                    "ODNO": order_id,
                    "CTX_AREA_NK200": "",
                    "CTX_AREA_FK200": "",
                },
            )

        resp = await self._call_with_retry(_req, f"체결조회 {order_id}", retries=3)
        data = resp.json()
        items = data.get("output", [])
        if not items:
            return {"filled_qty": 0, "remaining_qty": 0, "status": "UNKNOWN"}
        item = items[0]
        return {
            "filled_qty": int(_sf(item.get("ft_ccld_qty"))),
            "remaining_qty": int(_sf(item.get("nccs_qty"))),
            "avg_price": _sf(item.get("ft_ccld_unpr3")),
            "status": item.get("ord_stts", ""),
        }

    # ── 주문 취소 ────────────────────────────────────────────────────────────

    async def cancel_order(
        self, order_id: str, excd: str, symbol: str, quantity: int,
    ) -> bool:
        """해외주식 주문 취소"""
        tr_id = _CANCEL_TR[self._mode]
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "OVRS_EXCG_CD": _to_order_excd(excd),
            "PDNO": symbol,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "ORD_SVR_DVSN_CD": "0",
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl",
                headers=headers, json=body,
            )

        resp = await self._call_with_retry(_req, f"주문취소 {order_id}", retries=2)
        data = resp.json()
        success = data.get("rt_cd") == "0"
        if success:
            logger.info(f"[Night:KIS] order cancelled: {order_id}")
        else:
            logger.warning(f"[Night:KIS] cancel failed: {data.get('msg1')}")
        return success

    # ── 거래량 순위 ──────────────────────────────────────────────────────────

    async def get_volume_rank(self, excd: str = "NAS", count: int = 20) -> list[dict[str, Any]]:
        """해외주식 거래량 상위 종목 조회"""
        await self._throttle_quote()

        async def _req():
            headers = await self._headers(_VOLUME_RANK_TR)
            return await self._client.get(
                f"{self._base_url}/uapi/overseas-price/v1/quotations/inquire-search",
                headers=headers,
                params={
                    "AUTH": "",
                    "EXCD": excd,
                    "CO_YN_PRICECUR": "",
                    "CO_ST_PRICECUR": "",
                    "CO_EN_PRICECUR": "",
                    "CO_YN_RATE": "",
                    "CO_ST_RATE": "",
                    "CO_EN_RATE": "",
                    "CO_YN_VALX": "",
                    "CO_ST_VALX": "",
                    "CO_EN_VALX": "",
                    "CO_YN_SHAR": "",
                    "CO_ST_SHAR": "",
                    "CO_EN_SHAR": "",
                    "CO_YN_VOLUME": "1",
                    "CO_ST_VOLUME": "100000",
                    "CO_EN_VOLUME": "",
                    "CO_YN_AMT": "",
                    "CO_ST_AMT": "",
                    "CO_EN_AMT": "",
                    "CO_YN_EPS": "",
                    "CO_ST_EPS": "",
                    "CO_EN_EPS": "",
                    "CO_YN_PER": "",
                    "CO_ST_PER": "",
                    "CO_EN_PER": "",
                },
            )

        resp = await self._call_with_retry(_req, f"해외거래량순위({excd})")
        data = resp.json()
        items = data.get("output2", [])[:count]
        result = []
        for item in items:
            result.append({
                "symbol": item.get("symb", ""),
                "name": item.get("name", ""),
                "excd": excd,
                "current_price": _sf(item.get("last")),
                "change_rate": _sf(item.get("rate")),
                "volume": int(_sf(item.get("tvol"))),
            })
        return result

    # ── 리소스 정리 ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.aclose()
