"""한국투자증권 Open API (KIS Developers) 클라이언트

Docs: https://apiportal.koreainvestment.com
실전: https://openapi.koreainvestment.com:9443
모의: https://openapivts.koreainvestment.com:9443
"""
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

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: datetime = datetime.min
        # 한국투자증권 모의투자 서버는 SSL 인증서 호스트명 불일치 이슈가 있어 verify=False 처리
        self._client = httpx.AsyncClient(timeout=10.0, verify=False)

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

    async def get_quote(self, symbol: str) -> StockQuote:
        """현재가 조회 (TR: FHKST01010100)"""
        headers = await self._headers("FHKST01010100")
        resp = await self._client.get(
            f"{self._base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=headers,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        resp.raise_for_status()
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
        headers = await self._headers(tr_id)
        resp = await self._client.get(
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
        resp.raise_for_status()
        return resp.json()

    async def get_available_cash(self) -> int:
        """주문 가능 현금 조회
        실전 TR: TTTC8908R
        모의 TR: VTTC8908R
        """
        tr_id = "TTTC8908R" if settings.is_live else "VTTC8908R"
        headers = await self._headers(tr_id)
        resp = await self._client.get(
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
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            logger.warning(f"가용현금 조회 실패: {data.get('msg1')}")
            return 0
        output = data.get("output", {})
        cash = int(output.get("ord_psbl_cash", 0))
        logger.info(f"주문가능현금: {cash:,}원")
        return cash

    async def buy(self, symbol: str, quantity: int, price: int = 0) -> OrderResult:
        """매수 주문
        실전 TR: TTTC0802U
        모의 TR: VTTC0802U
        price=0 이면 시장가(ORD_DVSN=01), 그 외 지정가(ORD_DVSN=00)
        """
        tr_id = "TTTC0802U" if settings.is_live else "VTTC0802U"
        ord_dvsn = "01" if price == 0 else "00"
        headers = await self._headers(tr_id)

        body = {
            "CANO": settings.active_kis_account_number[:8],
            "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }
        resp = await self._client.post(
            f"{self._base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1", str(data)))
        logger.info(f"매수 주문: {symbol} {quantity}주 @ {price}원")
        return OrderResult(
            order_id=data["output"]["ODNO"],
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=price,
            status="SUBMITTED",
            timestamp=datetime.now(),
        )

    async def sell(self, symbol: str, quantity: int, price: int = 0) -> OrderResult:
        """매도 주문
        실전 TR: TTTC0801U
        모의 TR: VTTC0801U
        price=0 이면 시장가(ORD_DVSN=01), 그 외 지정가(ORD_DVSN=00)
        """
        tr_id = "TTTC0801U" if settings.is_live else "VTTC0801U"
        ord_dvsn = "01" if price == 0 else "00"
        headers = await self._headers(tr_id)

        body = {
            "CANO": settings.active_kis_account_number[:8],
            "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
        }
        resp = await self._client.post(
            f"{self._base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(data.get("msg1", str(data)))
        logger.info(f"매도 주문: {symbol} {quantity}주 @ {price}원")
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
        headers = await self._headers(tr_id)
        resp = await self._client.get(
            f"{self._base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers=headers,
            params={
                "CANO": settings.active_kis_account_number[:8],
                "ACNT_PRDT_CD": settings.active_kis_account_number[8:],
                "INQR_STRT_DT": datetime.now(_KST).strftime("%Y%m%d"),
                "INQR_END_DT": datetime.now(_KST).strftime("%Y%m%d"),
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
        resp.raise_for_status()
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
        headers = await self._headers(tr_id)
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
        resp = await self._client.post(
            f"{self._base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        success = data.get("rt_cd") == "0"
        if success:
            logger.info(f"주문 취소 완료: {order_id}")
        else:
            logger.warning(f"주문 취소 실패: {data.get('msg1')}")
        return success

    def reset_token(self) -> None:
        """모드 전환 시 캐시된 토큰 무효화 (다음 요청 시 재발급)"""
        self._access_token = None
        self._token_expires_at = datetime.min

    async def close(self) -> None:
        await self._client.aclose()
