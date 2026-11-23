from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

import httpx

from .exceptions import BuaaError, SessionExpiredError
from .sso import BuaaSessionManager, looks_like_login_page
from .webvpn import (
    is_vpn_gateway_url,
    route_url,
)

TD_BASE = "http://10.212.28.38"
TD_ENTRY_URL = f"{TD_BASE}/index.php"
TD_OVERVIEW_URL = f"{TD_BASE}/main.php?module=stu&title=stu_sun_score"


@dataclass
class TdOverviewData:
    academic_year: str
    semester: int
    term_name: str
    term_count: float | None
    attendance_count: float | None
    activity_count: float | None
    running_count: float | None


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def _count(cell: str) -> float:
    value = re.sub(r"[（(].*", "", cell).strip()
    if not value:
        return 0
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise BuaaError("体能锻炼次数格式无效") from exc
    if not number.is_finite() or number < 0 or number > 1_000_000:
        raise BuaaError("体能锻炼次数超出有效范围")
    return float(number)


def parse_td_overview(html: str) -> TdOverviewData:
    year_match = re.search(r"\bvar\s+xn\s*=\s*['\"](\d{4}-\d{4})['\"]", html)
    term_match = re.search(r"\bvar\s+xq\s*=\s*['\"]([12])['\"]", html)
    if not year_match or not term_match:
        raise BuaaError("健康云页面缺少当前学年学期")
    year, term = year_match[1], int(term_match[1])
    parser = _Tables()
    parser.feed(html)
    for table in parser.tables:
        if not table or table[0][:2] != ["学年", "学期"]:
            continue
        headers = table[0]
        columns = {}
        for code in ("888", "802", "803", "804"):
            columns[code] = next(
                (
                    i
                    for i, header in enumerate(headers)
                    if re.search(rf"[（(]{code}[）)]", header)
                ),
                None,
            )
        if columns["888"] is None:
            continue
        row = next((r for r in table[1:] if r[:2] == [year, str(term)]), None)
        counts: list[float | None] = []
        for code in ("888", "802", "803", "804"):
            column = columns[code]
            if row is not None and column is not None and column >= len(row):
                raise BuaaError("健康云次数表格结构无效")
            counts.append(
                _count(row[column]) if row is not None and column is not None else None
            )
        return TdOverviewData(year, term, f"{year} 学年第 {term} 学期", *counts)
    raise BuaaError("健康云页面缺少体能锻炼次数表格")


class TdClient:
    def __init__(
        self, session_mgr: BuaaSessionManager, network_mode: str = "webvpn"
    ) -> None:
        self.session_mgr = session_mgr
        self.network_mode = network_mode

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(route_url(url, self.network_mode))
        if looks_like_login_page(response.text) or is_vpn_gateway_url(
            str(response.url)
        ):
            raise SessionExpiredError("健康云统一认证会话已过期")
        if response.status_code != 200:
            raise BuaaError(f"健康云请求失败 (HTTP {response.status_code})")
        return response.text

    async def get_overview(self) -> TdOverviewData:
        for attempt in range(2):
            try:
                async with self.session_mgr.build_http_client(
                    self.network_mode, follow_redirects=True
                ) as client:
                    await self._get(client, TD_ENTRY_URL)
                    html = await self._get(client, TD_OVERVIEW_URL)
                    return parse_td_overview(html)
            except SessionExpiredError:
                if attempt:
                    raise
                await self.session_mgr.login_sso(network_mode=self.network_mode)
            except httpx.HTTPError as exc:
                raise BuaaError("健康云网络请求失败") from exc
        raise SessionExpiredError("健康云会话刷新失败")
