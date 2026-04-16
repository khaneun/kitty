"""한국투자증권 Open API (KIS Developers) 클라이언트

Docs: https://apiportal.koreainvestment.com
실전: https://openapi.koreainvestment.com:9443
모의: https://openapivts.koreainvestment.com:9443
"""
import asyncio
import random
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

import httpx
from pydantic import BaseModel

from kitty.config import settings
from kitty.utils import logger


class StockQuote(BaseModel):
    symbol: str
    name: str
    current_price: int
    change_rate: float       # 등락률 (%)
    volume: int
    market_cap: int | None = None


class OrderResult(BaseModel):
    order_id: str
    symbol: str
    side: str                # BUY / SELL
    quantity: int
    price: int
    status: str
    timestamp: datetime


class KISBroker:
    """한국투자증권 Open API 래퍼"""

    # quote 간격: 0.4s → 초당 최대 2.5건 (연속 조회 오류 방지)
    _QUOTE_INTERVAL = 0.4
    # 주문 간격: 연속 주문 1.2s
    _ORDER_INTERVAL = 1.2

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: datetime = datetime.min
        # 실전: SSL 검증 활성화 / 모의: 인증서 호스트명 불일치 이슈로 verify=False
        self._client = httpx.AsyncClient(
            timeout=15.0,
            verify=settings.is_live,
        )
        self._last_quote_ts: float = 0.0
        self._last_order_ts: float = 0.0

    @property
    def _base_url(self) -> str:
        return settings.active_kis_base_url

    async def _get_token(self) -> str:
        if self._access_token and datetime.now() < self._token_expires_at:
            return self._access_token

        resp = await self._client.post(
            f"{self._base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": settings.active_kis_app_key,
                "appsecret": settings.active_kis_app_secret,
            },
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "KIS API 403 — 앱키가 해당 환경(모의/실전)에 미등록이거나 "
                "단시간 내 토큰 요청 초과입니다. 잠시 후 재시도하세요."
            )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        # 한국투자증권 토큰 유효기간 24시간, 여유 있게 23시간 후 갱신
        self._token_expires_at = datetime.now() + timedelta(hours=23)
        logger.info("한국투자증권 토큰 발급 완료")
        return self._access_token  # type: ignore[return-value]

    async def _headers(self, tr_id: str) -> dict[str, str]:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "appkey": settings.active_kis_app_key,
            "appsecret": settings.active_kis_app_secret,
            "tr_id": tr_id,
            "Content-Type": "application/json; charset=utf-8",
        }

    # ── Throttle ─────────────────────────────────────────────────────────────

    async def _throttle_quote(self) -> None:
        """시세 조회 최소 간격 보장"""
        elapsed = time.monotonic() - self._last_quote_ts
        wait = self._QUOTE_INTERVAL - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_quote_ts = time.monotonic()

    async def _throttle_order(self) -> None:
        """연속 주문 최소 간격 보장"""
        elapsed = time.monotonic() - self._last_order_ts
        wait = self._ORDER_INTERVAL - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_order_ts = time.monotonic()

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
        - 500/503 서버 오류: 지수 백오프 (10s → 20s → 30s) — KIS 새벽 점검 대응
        - 네트워크 오류: 지수 백오프
        - 다른 4xx/5xx: 즉시 raise
        """
        last_exc: Exception = RuntimeError(f"KIS API 재시도 초과: {label}")
        for attempt in range(retries):
            try:
                resp = await make_request()
                if resp.status_code == 429:
                    wait = 30.0 + random.uniform(0.0, 10.0)
                    logger.warning(
                        f"[KIS] {label} 레이트리밋(429) — {wait:.0f}s 대기 후 재시도 "
                        f"({attempt + 1}/{retries})"
                    )
                    await asyncio.sleep(wait)
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP 429", request=resp.request, response=resp,
                    )
                    continue
                if resp.status_code in (500, 503):
                    # 새벽 KIS 정기 점검(04:00~06:00 KST) 대응 — 대기 30s까지 늘림
                    wait = min((attempt + 1) * 10.0, 30.0) + random.uniform(0.0, 2.0)
                    logger.warning(
                        f"[KIS] {label} 서버오류({resp.status_code}) — "
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
                    f"[KIS] {label} 네트워크 오류 ({attempt + 1}/{retries}) "
                    f"{wait:.1f}s 대기: {e}"
                )
                await asyncio.sleep(wait)
        raise last_exc

    async def get_quote(self, symbol: str) -> StockQuote:
        """현재가 조회 (TR: FHKST01010100)"""
        await self._throttle_quote()

        async def _req():
            headers = await self._headers("FHKST01010100")
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=headers,
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
            )

        resp = await self._call_with_retry(_req, f"{symbol} 시세조회")
        output = resp.json()["output"]
        return StockQuote(
            symbol=symbol,
            name=output.get("hts_kor_isnm", ""),
            current_price=int(output.get("stck_prpr", 0)),
            change_rate=float(output.get("prdy_ctrt", 0.0)),
            volume=int(output.get("acml_vol", 0)),
        )

    async def get_balance(self) -> dict[str, Any]:
        """주식 잔고 조회
        실전 TR: TTTC8434R
        모의 TR: VTTC8434R
        """
        tr_id = "TTTC8434R" if settings.is_live else "VTTC8434R"

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
                headers=headers,
                params={
                    "CANO": settings.active_kis_account_number[:8],
                    "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "N",
                    "INQR_DVSN": "02",
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "01",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
            )

        resp = await self._call_with_retry(_req, "잔고조회")
        return resp.json()

    async def get_available_cash(self) -> int:
        """주문 가능 현금 조회
        실전 TR: TTTC8908R
        모의 TR: VTTC8908R
        """
        tr_id = "TTTC8908R" if settings.is_live else "VTTC8908R"

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                headers=headers,
                params={
                    "CANO": settings.active_kis_account_number[:8],
                    "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
                    "PDNO": "005930",   # 조회용 임의 종목 (삼성전자)
                    "ORD_UNPR": "0",
                    "ORD_DVSN": "01",
                    "CMA_EVLU_AMT_ICLD_YN": "Y",
                    "OVRS_ICLD_YN": "N",
                },
            )

        resp = await self._call_with_retry(_req, "주문가능현금조회")
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"가용현금 조회 실패: {data.get('msg1')}")
            return 0
        output = data.get("output", {})
        # nrcvb_buy_amt(미수없는매수금액): 가수도정산금액(당일매도 재투자분) 포함한 실제 매수 가능 금액
        # ord_psbl_cash(주문가능현금): 예수금 현금만 — 당일 매도 재투자분 미포함으로 과소 계상됨
        cash = int(output.get("nrcvb_buy_amt", 0))
        logger.info(f"매수가능금액(미수없음): {cash:,}원")
        return cash

    async def buy(self, symbol: str, quantity: int, price: int = 0, name: str = "") -> OrderResult:
        """매수 주문
        실전 TR: TTTC0802U
        모의 TR: VTTC0802U
        price=0 이면 시장가(ORD_DVSN=01), 그 외 지정가(ORD_DVSN=00)
        """
        await self._throttle_order()
        tr_id = "TTTC0802U" if settings.is_live else "VTTC0802U"
        ord_dvsn = "01" if price == 0 else "00"
        _label = f"{name}({symbol})" if name else symbol

        body = {
            "CANO": settings.active_kis_account_number[:8],
            "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/order-cash",
                headers=headers,
                json=body,
            )

        resp = await self._call_with_retry(_req, f"매수 {_label}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1") or data.get("msg", str(data)))
        logger.info(f"매수 주문: {_label} {quantity}주 @ {price}원")
        return OrderResult(
            order_id=data["output"]["ODNO"],
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=price,
            status="SUBMITTED",
            timestamp=datetime.now(),
        )

    async def sell(self, symbol: str, quantity: int, price: int = 0, name: str = "") -> OrderResult:
        """매도 주문
        실전 TR: TTTC0801U
        모의 TR: VTTC0801U
        price=0 이면 시장가(ORD_DVSN=01), 그 외 지정가(ORD_DVSN=00)
        """
        await self._throttle_order()
        tr_id = "TTTC0801U" if settings.is_live else "VTTC0801U"
        ord_dvsn = "01" if price == 0 else "00"
        _label = f"{name}({symbol})" if name else symbol

        body = {
            "CANO": settings.active_kis_account_number[:8],
            "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/order-cash",
                headers=headers,
                json=body,
            )

        resp = await self._call_with_retry(_req, f"매도 {_label}")
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1") or data.get("msg", str(data)))
        logger.info(f"매도 주문: {_label} {quantity}주 @ {price}원")
        return OrderResult(
            order_id=data["output"]["ODNO"],
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=price,
            status="SUBMITTED",
            timestamp=datetime.now(),
        )

    async def get_order_status(self, order_id: str) -> dict:
        """주문 체결 조회
        실전 TR: TTTC8001R
        모의 TR: VTTC8001R
        Returns: {"filled_qty": int, "remaining_qty": int, "status": str}
        """
        tr_id = "TTTC8001R" if settings.is_live else "VTTC8001R"
        today = datetime.now(_KST).strftime("%Y%m%d")

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                headers=headers,
                params={
                    "CANO": settings.active_kis_account_number[:8],
                    "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
                    "INQR_STRT_DT": today,
                    "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "00",
                    "INQR_DVSN": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": order_id,
                    "INQR_DVSN_3": "00",
                    "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
            )

        resp = await self._call_with_retry(_req, f"체결조회 {order_id}", retries=3)
        data = resp.json()
        items = data.get("output1", [])
        if not items:
            return {"filled_qty": 0, "remaining_qty": 0, "status": "UNKNOWN"}
        item = items[0]
        return {
            "filled_qty": int(item.get("tot_ccld_qty", 0)),
            "remaining_qty": int(item.get("rmn_qty", 0)),
            "status": item.get("ord_stts", ""),
        }

    async def cancel_order(self, order_id: str, symbol: str, quantity: int) -> bool:
        """주문 취소
        실전 TR: TTTC0803U
        모의 TR: VTTC0803U
        """
        tr_id = "TTTC0803U" if settings.is_live else "VTTC0803U"
        body = {
            "CANO": settings.active_kis_account_number[:8],
            "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }

        async def _req():
            headers = await self._headers(tr_id)
            return await self._client.post(
                f"{self._base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
                headers=headers,
                json=body,
            )

        resp = await self._call_with_retry(_req, f"주문취소 {order_id}", retries=2)
        data = resp.json()
        success = data.get("rt_cd") == "0"
        if success:
            logger.info(f"주문 취소 완료: {order_id}")
        else:
            logger.warning(f"주문 취소 실패: {data.get('msg1')}")
        return success

    async def get_volume_rank(self, market: str = "J", count: int = 50) -> list[dict[str, Any]]:
        """거래량 상위 종목 조회 (TR: FHPST01710000)

        Args:
            market: "J" = KOSPI, "Q" = KOSDAQ
        Returns:
            [{"symbol", "name", "industry", "market", "current_price", "change_rate", "volume", "turnover"}, ...]
        """
        await self._throttle_quote()

        async def _req():
            headers = await self._headers("FHPST01710000")
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers=headers,
                params={
                    "FID_COND_MRKT_DIV_CODE": market,
                    "FID_COND_SCR_DIV_CODE": "20171" if market == "Q" else "20101",
                    "FID_INPUT_ISCD": "0000",
                    "FID_DIV_CLS_CODE": "0",
                    "FID_BLNG_CLS_CODE": "0",
                    "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "000000",
                    "FID_INPUT_PRICE_1": "",
                    "FID_INPUT_PRICE_2": "",
                    "FID_VOL_CNT": "",
                    "FID_INPUT_DATE_1": "",
                },
            )

        label = "KOSDAQ거래량순위" if market == "Q" else "KOSPI거래량순위"
        resp = await self._call_with_retry(_req, label)
        data = resp.json()
        result: list[dict[str, Any]] = []
        for item in data.get("output", [])[:count]:
            sym = item.get("mksc_shrn_iscd", "")
            if not sym:
                continue
            result.append({
                "symbol":        sym,
                "name":          item.get("hts_kor_isnm", ""),
                "industry":      item.get("bstp_kor_isnm", ""),   # 업종명 (섹터 매칭용)
                "market":        "KOSDAQ" if market == "Q" else "KOSPI",
                "current_price": int(item.get("stck_prpr", 0)),
                "change_rate":   float(item.get("prdy_ctrt", 0.0)),
                "volume":        int(item.get("acml_vol", 0)),
                "turnover":      int(item.get("acml_tr_pbmn", 0)),
            })
        return result

    async def get_change_rate_rank(self, market: str = "J", count: int = 50) -> list[dict[str, Any]]:
        """등락률 상위 종목 조회 (TR: FHPST01720000)

        Args:
            market: "J" = KOSPI, "Q" = KOSDAQ
        Returns:
            [{"symbol", "name", "industry", "market", "current_price", "change_rate", "volume", "turnover"}, ...]
        """
        await self._throttle_quote()

        async def _req():
            headers = await self._headers("FHPST01720000")
            return await self._client.get(
                f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                headers=headers,
                params={
                    "FID_COND_MRKT_DIV_CODE": market,
                    "FID_COND_SCR_DIV_CODE": "20172" if market == "Q" else "20170",
                    "FID_INPUT_ISCD": "0000",
                    "FID_RANK_SORT_CLS_CODE": "0",       # 0 = 상승률 순
                    "FID_DIFF_CLS_CODE":       "2",       # 2 = 전일 대비
                    "FID_TRGT_CLS_CODE":       "0",
                    "FID_TRGT_EXLS_CLS_CODE":  "0000000000",
                    "FID_INPUT_PRICE_1":        "",
                    "FID_INPUT_PRICE_2":        "",
                    "FID_RST_DVS_CODE":         "0",
                    "FID_INPUT_ISCD2":          "",
                    "FID_INPUT_DATE_1":         "",
                },
            )

        label = "KOSDAQ등락률순위" if market == "Q" else "KOSPI등락률순위"
        resp = await self._call_with_retry(_req, label)
        data = resp.json()
        result: list[dict[str, Any]] = []
        for item in data.get("output", [])[:count]:
            sym = item.get("mksc_shrn_iscd", "")
            if not sym:
                continue
            result.append({
                "symbol":        sym,
                "name":          item.get("hts_kor_isnm", ""),
                "industry":      item.get("bstp_kor_isnm", ""),
                "market":        "KOSDAQ" if market == "Q" else "KOSPI",
                "current_price": int(item.get("stck_prpr", 0)),
                "change_rate":   float(item.get("prdy_ctrt", 0.0)),
                "volume":        int(item.get("acml_vol", 0)),
                "turnover":      int(item.get("acml_tr_pbmn", 0)),
            })
        return result

    def reset_token(self) -> None:
        """모드 전환 시 캐시된 토큰 무효화 (다음 요청 시 재발급)"""
        self._access_token = None
        self._token_expires_at = datetime.min

    async def close(self) -> None:
        await self._client.aclose()
