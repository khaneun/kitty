"""DART OpenAPI 도구 - 전자공시시스템 공시 조회
API 키 발급: https://opendart.fss.or.kr → 인증키 신청/관리
"""
from datetime import datetime, timedelta
from typing import Any

import httpx

from kitty.utils import logger

from .base import BaseTool, ToolResult

_BASE = "https://opendart.fss.or.kr/api"


class DartTool(BaseTool):
    """DART 전자공시 조회 도구

    - fetch("005930")    → 삼성전자 최근 30일 공시 목록
    - fetch("반도체")    → 키워드 포함 최근 공시 목록 (KOSPI 전체)
    """

    @property
    def name(self) -> str:
        return "DART공시"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=10.0)
        self._corp_cache: dict[str, str] = {}   # stock_code → corp_code

    async def _corp_code(self, stock_code: str) -> str | None:
        if stock_code in self._corp_cache:
            return self._corp_cache[stock_code]
        try:
            resp = await self._client.get(
                f"{_BASE}/company.json",
                params={"crtfc_key": self._api_key, "stock_code": stock_code},
            )
            data: dict[str, Any] = resp.json()
            if data.get("status") == "000":
                code = data["corp_code"]
                self._corp_cache[stock_code] = code
                return code
        except Exception as e:
            logger.warning(f"[DART] corp_code 조회 실패 {stock_code}: {e}")
        return None

    async def fetch(self, query: str) -> ToolResult:
        """query가 6자리 숫자면 종목별 공시, 그 외엔 키워드 공시 검색"""
        if query.isdigit() and len(query) == 6:
            return await self._fetch_by_symbol(query)
        return await self._fetch_by_keyword(query)

    async def _fetch_by_symbol(self, stock_code: str) -> ToolResult:
        corp = await self._corp_code(stock_code)
        if not corp:
            return ToolResult(
                success=False, data="", source=self.name,
                error=f"corp_code 조회 실패: {stock_code}",
            )

        today = datetime.now()
        bgn = (today - timedelta(days=30)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")

        try:
            resp = await self._client.get(
                f"{_BASE}/list.json",
                params={
                    "crtfc_key": self._api_key,
                    "corp_code": corp,
                    "bgn_de": bgn,
                    "end_de": end,
                    "page_count": "10",
                },
            )
            data = resp.json()
            if data.get("status") != "000":
                return ToolResult(
                    success=False, data="", source=self.name,
                    error=data.get("message", "API 오류"),
                )

            items = data.get("list", [])[:5]
            if not items:
                return ToolResult(
                    success=True,
                    data=f"[{stock_code} 최근 30일 공시 없음]",
                    source=self.name,
                )

            lines = [f"[{stock_code} 최근 공시]"]
            for item in items:
                lines.append(
                    f"- {item['rcept_dt']} {item['report_nm']} (공시자: {item['flr_nm']})"
                )
            logger.debug(f"[DART] {stock_code} 공시 {len(items)}건 조회")
            return ToolResult(success=True, data="\n".join(lines), source=self.name)

        except Exception as e:
            logger.warning(f"[DART] 공시 조회 오류 {stock_code}: {e}")
            return ToolResult(success=False, data="", source=self.name, error=str(e))

    async def _fetch_by_keyword(self, keyword: str) -> ToolResult:
        today = datetime.now()
        bgn = (today - timedelta(days=7)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")

        try:
            resp = await self._client.get(
                f"{_BASE}/list.json",
                params={
                    "crtfc_key": self._api_key,
                    "bgn_de": bgn,
                    "end_de": end,
                    "page_count": "20",
                    "corp_cls": "Y",    # KOSPI
                },
            )
            data = resp.json()
            if data.get("status") != "000":
                return ToolResult(
                    success=False, data="", source=self.name,
                    error=data.get("message", "API 오류"),
                )

            all_items = data.get("list", [])
            # 키워드 필터링 (없으면 전체 최신 5건)
            filtered = [
                i for i in all_items
                if keyword in i.get("report_nm", "") or keyword in i.get("corp_name", "")
            ]
            items = (filtered or all_items)[:5]

            lines = [f"[DART 최근 공시 — 키워드: {keyword}]"]
            for item in items:
                lines.append(
                    f"- {item['rcept_dt']} {item['corp_name']} {item['report_nm']}"
                )
            return ToolResult(success=True, data="\n".join(lines), source=self.name)

        except Exception as e:
            logger.warning(f"[DART] 키워드 공시 오류 {keyword}: {e}")
            return ToolResult(success=False, data="", source=self.name, error=str(e))

    async def close(self) -> None:
        await self._client.aclose()
