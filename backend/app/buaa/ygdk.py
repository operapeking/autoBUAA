# BUAA Sunshine Clock-in (YGDK) adapter.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .exceptions import BuaaError, SessionExpiredError, parse_int
from .sso import BuaaSessionManager, looks_like_login_page
from .webvpn import (
    from_webvpn_url,
    is_vpn_gateway_url,
    route_url,
)

YGDK_OAUTH_URL = (
    "https://app.buaa.edu.cn/uc/api/oauth/index"
    "?redirect=https%3A%2F%2Fygdk.buaa.edu.cn%2F%23%2Fhome"
    "&appid=200230221144501510&state=STATE&qrcode=1"
)
YGDK_LOGIN_URL = "https://ygdk.buaa.edu.cn/api/Front/Clockin/User/campusAppLogin"
YGDK_FRONT_BASE = "https://ygdk.buaa.edu.cn"


@dataclass
class YgdkOverviewData:
    classify_id: int
    classify_name: str
    term_id: int | None
    term_name: str | None
    term_count: int | None
    term_target: int | None
    week_count: int | None
    week_target: int | None


def _extract_code_from_url(raw_url: str) -> str | None:
    try:
        direct = from_webvpn_url(raw_url)
        parsed = urlparse(direct)
        if (parsed.hostname or "").lower() != "ygdk.buaa.edu.cn":
            return None
        for source in [
            parsed.query,
            parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else "",
        ]:
            for pair in source.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k == "code" and v.strip():
                        return unquote(v.strip())
    except Exception:
        pass
    return None


def _parse_envelope(payload: dict[str, Any]) -> Any:
    code = parse_int(payload.get("code"))
    if code == 1:
        return payload.get("result")
    if code == -98:
        raise SessionExpiredError("阳光打卡业务会话已失效")
    raise BuaaError(
        str(payload.get("msg") or payload.get("errmsg") or "阳光打卡请求失败")
    )


class YgdkClient:
    """Adapter for BUAA Sunshine Clock-in (ygdk.buaa.edu.cn)."""

    def __init__(
        self, session_mgr: BuaaSessionManager, network_mode: str = "webvpn"
    ) -> None:
        self.session_mgr = session_mgr
        self.network_mode = network_mode

    @property
    def _state_key(self) -> str:
        return f"ygdk:{self.network_mode}"

    async def _ensure_credential(self, force_refresh: bool = False) -> tuple[int, str]:
        if not force_refresh:
            cached = self.session_mgr.load_domain_state(self._state_key)
            if cached.get("uid") and cached.get("token"):
                return int(cached["uid"]), str(cached["token"])

        async with self.session_mgr.build_http_client(
            self.network_mode, follow_redirects=False
        ) as client:
            code = await self._oauth_code(client)
            resp = await client.get(
                route_url(YGDK_LOGIN_URL, self.network_mode),
                params={"code": code},
            )
            if (
                resp.status_code != 200
                or looks_like_login_page(resp.text)
                or is_vpn_gateway_url(str(resp.url))
            ):
                if (
                    resp.status_code in (301, 302, 303, 307, 308)
                    or looks_like_login_page(resp.text)
                    or is_vpn_gateway_url(str(resp.url))
                ):
                    raise SessionExpiredError("阳光打卡登录重定向或会话过期")
                raise BuaaError(f"阳光打卡登录失败 (HTTP {resp.status_code})")
            try:
                payload = resp.json()
            except Exception as exc:
                raise BuaaError(f"阳光打卡登录响应无法解析: {exc}") from exc
            result = _parse_envelope(payload)
            data = (
                result.get("data")
                if isinstance(result, dict) and isinstance(result.get("data"), dict)
                else result
            )
            if not isinstance(data, dict):
                raise BuaaError("阳光打卡登录响应结构无效")
            uid = parse_int(data.get("uid"))
            token = str(data.get("token") or "").strip()
            if not uid or not token:
                raise BuaaError("阳光打卡登录响应缺少 uid 或 token")
            token = unquote(token)
            self.session_mgr.save_domain_state(
                self._state_key, {"uid": uid, "token": token}
            )
            return uid, token

    async def _oauth_code(self, client: httpx.AsyncClient) -> str:
        current = route_url(YGDK_OAUTH_URL, self.network_mode)
        last_status: int | None = None
        for _ in range(10):
            try:
                resp = await client.get(current)
            except httpx.TimeoutException as exc:
                raise BuaaError(f"阳光打卡 OAuth 请求超时: {exc}") from exc
            last_status = resp.status_code
            final_url = str(resp.url)
            code = _extract_code_from_url(final_url)
            if code:
                return code
            if is_vpn_gateway_url(final_url):
                raise SessionExpiredError("WebVPN 会话已过期")
            if resp.status_code >= 400:
                raise BuaaError(f"阳光打卡 OAuth 跳转失败 (HTTP {resp.status_code})")
            if looks_like_login_page(resp.text):
                raise SessionExpiredError("SSO 会话已过期")
            location = resp.headers.get("location")
            if not location:
                meta_match = re.search(
                    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'>\s]+)',
                    resp.text,
                    flags=re.IGNORECASE,
                )
                if meta_match:
                    location = meta_match.group(1).strip()
            if not location:
                break
            joined = urljoin(final_url, location)
            code = _extract_code_from_url(joined)
            if code:
                return code
            if is_vpn_gateway_url(joined):
                raise SessionExpiredError("WebVPN 会话已过期")
            current = route_url(from_webvpn_url(joined), self.network_mode)
        detail = f" (HTTP {last_status})" if last_status is not None else ""
        raise SessionExpiredError(f"阳光打卡 OAuth 跳转未获取到 code{detail}")

    async def _post_form(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        include_user_id: bool = False,
    ) -> Any:
        sso_refreshed = False
        for attempt in range(2):
            try:
                uid, token = await self._ensure_credential(force_refresh=(attempt > 0))
            except SessionExpiredError:
                if sso_refreshed:
                    raise
                await self.session_mgr.login_sso(network_mode=self.network_mode)
                sso_refreshed = True
                uid, token = await self._ensure_credential(force_refresh=True)

            form = {k: str(v) for k, v in (params or {}).items()}
            if include_user_id:
                form["user_id"] = str(uid)
            form["uid"] = str(uid)
            form["token"] = token
            url = route_url(f"{YGDK_FRONT_BASE}{path}", self.network_mode)

            async with self.session_mgr.build_http_client(
                self.network_mode, follow_redirects=True
            ) as client:
                try:
                    resp = await client.post(
                        url,
                        data=form,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                except httpx.TimeoutException as exc:
                    raise BuaaError(f"阳光打卡请求超时: {exc}") from exc

                if (
                    resp.status_code != 200
                    or looks_like_login_page(resp.text)
                    or is_vpn_gateway_url(str(resp.url))
                ):
                    self.session_mgr.clear_domain_state(self._state_key)
                    if attempt == 0:
                        if not sso_refreshed:
                            await self.session_mgr.login_sso(
                                network_mode=self.network_mode
                            )
                            sso_refreshed = True
                        continue
                    raise SessionExpiredError("阳光打卡会话已失效")

                try:
                    return _parse_envelope(resp.json())
                except SessionExpiredError:
                    self.session_mgr.clear_domain_state(self._state_key)
                    if attempt == 0:
                        continue
                    raise
        raise SessionExpiredError("阳光打卡会话刷新失败")

    async def get_overview(self) -> YgdkOverviewData:
        classifies = await self._post_form("/api/Front/Clockin/Classify/getList")
        classify_rows = (classifies or {}).get("list") or []
        selected = next(
            (r for r in classify_rows if "体育" in str(r.get("name") or "")),
            classify_rows[0] if classify_rows else None,
        )
        if not selected or not parse_int(selected.get("classify_id")):
            raise BuaaError("未获取到阳光打卡分类")
        classify_id = int(selected["classify_id"])
        classify_name = str(selected.get("name") or "")

        count = (
            await self._post_form(
                "/api/Front/Clockin/Clockin/getCount",
                {"classify_id": classify_id},
                include_user_id=True,
            )
            or {}
        )
        term = await self._post_form("/api/Front/Clockin/Term/get") or {}
        term_count = next(
            (
                value
                for key in (
                    "term_good_count_show",
                    "term_good_count",
                    "term_count_show",
                    "term_count",
                )
                if (value := parse_int(count.get(key))) is not None
            ),
            None,
        )
        return YgdkOverviewData(
            classify_id=classify_id,
            classify_name=classify_name,
            term_id=parse_int(term.get("term_id")) or parse_int(term.get("id")),
            term_name=term.get("name"),
            term_count=term_count,
            term_target=parse_int(count.get("term_num"))
            or parse_int(selected.get("term_num")),
            week_count=parse_int(count.get("week_count")),
            week_target=parse_int(count.get("week_num"))
            or parse_int(selected.get("week_num")),
        )
