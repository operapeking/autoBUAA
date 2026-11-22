# BUAA iClass classroom sign-in adapter.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .exceptions import (
    BuaaError,
    BusinessTerminalError,
    OutcomeUnknownError,
    SessionExpiredError,
)
from .sso import BuaaSessionManager, looks_like_login_page
from .webvpn import (
    EndpointDiagnostic,
    diagnose_host_connection,
    from_webvpn_url,
    is_vpn_gateway_url,
    route_url,
)

SIGNIN_ENTRY_URL = "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter"
SIGNIN_LOGIN_URL = "https://iclass.buaa.edu.cn:8347/app/user/login.action"
SIGNIN_TODAY_URL = (
    "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action"
)
SIGNIN_SUBMIT_URL = "https://iclass.buaa.edu.cn:8181/app/course/stu_auto_sign.action"


@dataclass
class ClassroomCourse:
    course_sched_id: str
    course_name: str
    class_begin_time: str
    class_end_time: str
    sign_status: int
    raw: dict[str, Any]

    @property
    def signed(self) -> bool:
        return self.sign_status == 1


@dataclass
class ClassroomSignResult:
    success: bool
    already_signed: bool
    message: str


def _is_success_status(value: Any) -> bool:
    return value in (0, 200, "0", "200", "success")


def _is_empty_status(value: Any) -> bool:
    return value in (2, "2")


def _action_status(value: Any) -> int | None:
    if value == "success":
        return 0
    return _parse_int(value)


def _is_allowed_login_hop(url_str: str) -> bool:
    try:
        direct = from_webvpn_url(url_str)
        parsed = urlparse(direct)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            return False
        if host == "iclass.buaa.edu.cn":
            return parsed.port in (None, 443, 8346, 8347)
        if host == "sso.buaa.edu.cn":
            return parsed.port in (None, 443)
        return False
    except Exception:
        return False


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _final_url_is_auth(url_str: str) -> bool:
    if is_vpn_gateway_url(url_str):
        return True
    try:
        direct = from_webvpn_url(url_str)
        parsed = urlparse(direct)
        host = (parsed.hostname or "").lower()
        return host in {"sso.buaa.edu.cn", "d.buaa.edu.cn", "vpn.buaa.edu.cn"}
    except Exception:
        return True


def _extract_login_name(url_str: str) -> str | None:
    try:
        direct = from_webvpn_url(url_str)
        parsed = urlparse(direct)
        qs = parse_qs(parsed.query)
        for key, vals in qs.items():
            if key.lower() == "loginname" and vals and vals[0].strip():
                return vals[0].strip()
    except Exception:
        pass
    return None


def parse_today_courses(payload: dict[str, Any]) -> list[ClassroomCourse]:
    status = payload.get("STATUS")
    if _is_empty_status(status):
        return []
    if not _is_success_status(status):
        raise SessionExpiredError(f"课堂接口返回非成功状态: {status!r}")
    rows = payload.get("result") or []
    courses: list[ClassroomCourse] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        sign_status = _parse_int(row.get("signStatus"))
        courses.append(
            ClassroomCourse(
                course_sched_id=cid,
                course_name=str(row.get("courseName") or ""),
                class_begin_time=str(row.get("classBeginTime") or ""),
                class_end_time=str(row.get("classEndTime") or ""),
                sign_status=sign_status if sign_status is not None else 0,
                raw=row,
            )
        )
    return courses


class ClassroomClient:
    """Adapter for BUAA iClass classroom schedule query and sign-in."""

    def __init__(
        self, session_mgr: BuaaSessionManager, network_mode: str = "webvpn"
    ) -> None:
        self.session_mgr = session_mgr
        self.network_mode = network_mode

    @property
    def _state_key(self) -> str:
        return f"iclass:{self.network_mode}"

    def diagnose(self) -> EndpointDiagnostic:
        target = (
            "https://iclass.buaa.edu.cn:8347/app/common/get_timestamp.action"
            if self.network_mode != "direct"
            else "http://iclass.buaa.edu.cn:8081/app/common/get_timestamp.action"
        )
        return diagnose_host_connection(target, self.network_mode)

    async def _ensure_credential(self, force_refresh: bool = False) -> tuple[str, str]:
        if not force_refresh:
            cached = self.session_mgr.load_domain_state(self._state_key)
            if cached.get("user_id") and cached.get("session_id"):
                return str(cached["user_id"]), str(cached["session_id"])

        async with self.session_mgr.build_http_client(
            self.network_mode, follow_redirects=False
        ) as client:
            login_name = await self._resolve_login_name(client)
            login_url = route_url(SIGNIN_LOGIN_URL, self.network_mode)
            resp = await client.get(
                login_url,
                params={
                    "password": "",
                    "phone": login_name,
                    "userLevel": "1",
                    "verificationType": "2",
                    "verificationUrl": "",
                },
            )
            if resp.status_code != 200:
                raise BuaaError(f"课堂业务登录失败 (HTTP {resp.status_code})")
            data = resp.json()
            if not _is_success_status(data.get("STATUS")):
                raise BuaaError("课堂业务登录返回失败状态")
            result = data.get("result") or {}
            user_id = str(result.get("id") or "").strip()
            session_id = str(result.get("sessionId") or "").strip()
            if not user_id or not session_id:
                raise BuaaError("课堂登录响应缺少 user_id 或 sessionId")
            self.session_mgr.save_domain_state(
                self._state_key,
                {"user_id": user_id, "session_id": session_id},
            )
            return user_id, session_id

    async def _resolve_login_name(self, client: httpx.AsyncClient) -> str:
        current = route_url(SIGNIN_ENTRY_URL, self.network_mode)
        for _ in range(8):
            found = _extract_login_name(current)
            if found:
                return found
            resp = await client.get(current)
            final_url = str(resp.url)
            found = _extract_login_name(final_url)
            if found:
                return found
            if looks_like_login_page(resp.text) or is_vpn_gateway_url(final_url):
                raise SessionExpiredError("SSO 会话未建立或已过期")
            if not _is_allowed_login_hop(final_url):
                raise BuaaError("课堂签到登录跳转到未允许的主机")
            location = resp.headers.get("location")
            if not location:
                break
            next_url = urljoin(final_url, location)
            found = _extract_login_name(next_url)
            if found:
                return found
            if is_vpn_gateway_url(next_url):
                raise SessionExpiredError("WebVPN 会话未建立或已过期")
            if not _is_allowed_login_hop(next_url):
                raise BuaaError("课堂签到登录跳转到未允许的主机")
            current = route_url(from_webvpn_url(next_url), self.network_mode)
        raise SessionExpiredError(
            "无法从课堂跳转流程解析 loginName，可能需要重新登录 SSO"
        )

    async def get_today_courses(self, date_str: str) -> list[ClassroomCourse]:
        """Query courses for YYYYMMDD, automatically refreshing SSO/iClass session once if expired."""
        for attempt in range(2):
            try:
                user_id, session_id = await self._ensure_credential(
                    force_refresh=(attempt > 0)
                )
            except SessionExpiredError:
                await self.session_mgr.login_sso(network_mode=self.network_mode)
                user_id, session_id = await self._ensure_credential(force_refresh=True)

            async with self.session_mgr.build_http_client(
                self.network_mode, follow_redirects=True
            ) as client:
                url = route_url(SIGNIN_TODAY_URL, self.network_mode)
                resp = await client.get(
                    url,
                    params={"id": user_id, "dateStr": date_str},
                    headers={"sessionId": session_id},
                )
                if resp.status_code != 200 or _final_url_is_auth(str(resp.url)):
                    self.session_mgr.clear_domain_state(self._state_key)
                    if attempt == 0:
                        await self.session_mgr.login_sso(network_mode=self.network_mode)
                        continue
                    raise SessionExpiredError("课堂课程查询认证失效")
                try:
                    data = resp.json()
                    return parse_today_courses(data)
                except SessionExpiredError:
                    self.session_mgr.clear_domain_state(self._state_key)
                    if attempt == 0:
                        continue
                    raise
        return []

    async def sign_in(self, course_sched_id: str, date_str: str) -> ClassroomSignResult:
        """Preflight course status then submit classroom sign-in."""
        courses = await self.get_today_courses(date_str)
        target = next(
            (c for c in courses if c.course_sched_id == course_sched_id), None
        )
        if target is None:
            raise BusinessTerminalError("当日课表中未找到该课程安排")
        if target.signed:
            return ClassroomSignResult(
                success=True, already_signed=True, message="该课程已签到"
            )

        user_id, session_id = await self._ensure_credential()
        async with self.session_mgr.build_http_client(
            self.network_mode, follow_redirects=True
        ) as client:
            sign_url = route_url(SIGNIN_SUBMIT_URL, self.network_mode)
            try:
                resp = await client.post(
                    sign_url,
                    data={
                        "id": user_id,
                        "courseSchedId": course_sched_id,
                        "routerInfo": "02:00:00:00:00:00",
                        "longitude": "0",
                        "latitude": "0",
                        "machineInfo": "Android",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "sessionId": session_id,
                    },
                )
            except httpx.TimeoutException as exc:
                raise OutcomeUnknownError("课堂签到请求超时，结果待确认") from exc

            if resp.status_code != 200 or _final_url_is_auth(str(resp.url)):
                raise OutcomeUnknownError("课堂签到响应异常或会话跳转，结果待确认")

            try:
                body = resp.json()
            except Exception as exc:
                raise OutcomeUnknownError("课堂签到响应无法解析，结果待确认") from exc

            action = _action_status(body.get("STATUS"))
            if action is None:
                raise OutcomeUnknownError(
                    f"课堂签到返回未知状态: {body.get('STATUS')!r}"
                )
            if action not in (0, 200):
                return ClassroomSignResult(
                    success=False, already_signed=False, message="签到未完成"
                )
            stu_status = _parse_int((body.get("result") or {}).get("stuSignStatus"))
            if stu_status == 1:
                return ClassroomSignResult(
                    success=True, already_signed=False, message="课堂签到成功"
                )
            if stu_status == 0:
                return ClassroomSignResult(
                    success=False, already_signed=False, message="签到未完成"
                )
            raise OutcomeUnknownError("课堂签到响应缺少 stuSignStatus，结果待确认")
