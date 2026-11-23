# BUAA Boya (BYKC) course client and crypto.
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from app.core.time import SHANGHAI_TZ
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .exceptions import (
    BuaaError,
    BusinessTerminalError,
    OutcomeUnknownError,
    SessionExpiredError,
    parse_int,
)
from .sso import BuaaSessionManager, looks_like_login_page
from .webvpn import (
    from_webvpn_url,
    is_vpn_gateway_url,
    route_url,
)

BYKC_BASE = "https://bykc.buaa.edu.cn/sscv"
BYKC_CAS_LOGIN = "https://bykc.buaa.edu.cn/sscv/cas/login"
BYKC_CAS_EMPTY_TOKEN = "https://bykc.buaa.edu.cn/cas-login?token="
BYKC_REFERER = "https://bykc.buaa.edu.cn/system/course-select"
BYKC_ORIGIN = "https://bykc.buaa.edu.cn"

BYKC_RSA_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDlHMQ3B5GsWnCe7Nlo1YiG/YmH
dlOiKOST5aRm4iaqYSvhvWmwcigoyWTM+8bv2+sf6nQBRDWTY4KmNV7DBk1eDnTI
Qo6ENA31k5/tYCLEXgjPbEjCK9spiyB62fCT6cqOhbamJB0lcDJRO6Vo1m3dy+fD
0jbxfDVBBNtyltIsDQIDAQAB
-----END PUBLIC KEY-----
"""

KEY_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def normalize_sub_category(name: str | None) -> str:
    val = (name or "").strip()
    if "|" in val:
        val = val.split("|", 1)[1].strip()
    if val == "劳育":
        return "劳动教育"
    return val


@dataclass
class EncryptedRequest:
    body: bytes
    headers: dict[str, str]


class BykcCrypto:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = (
            key or "".join(secrets.choice(KEY_ALPHABET) for _ in range(16)).encode()
        )
        self.public_key = serialization.load_pem_public_key(BYKC_RSA_PEM)

    def encrypt_plaintext(self, plaintext: bytes) -> bytes:
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.key), modes.ECB()).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return json.dumps(base64.b64encode(encrypted).decode()).encode()

    def decrypt_response(self, body: bytes | str) -> Any:
        text = body.decode() if isinstance(body, bytes) else body
        encoded = json.loads(text)
        encrypted = base64.b64decode(encoded)
        decryptor = Cipher(algorithms.AES(self.key), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return json.loads(plaintext.decode())

    def encrypt_request(self, payload: dict[str, Any] | str) -> EncryptedRequest:
        if isinstance(payload, str):
            plaintext = payload.encode()
        else:
            plaintext = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
        body = self.encrypt_plaintext(plaintext)
        return EncryptedRequest(
            body=body,
            headers={
                "Ak": self._rsa_b64(self.key),
                "Sk": self._rsa_b64(hashlib.sha1(plaintext).hexdigest().encode()),
                "Ts": str(int(time.time() * 1000)),
            },
        )

    def _rsa_b64(self, data: bytes) -> str:
        encrypted = self.public_key.encrypt(data, asym_padding.PKCS1v15())  # type: ignore[union-attr]
        return base64.b64encode(encrypted).decode()


@dataclass
class BoyaCourseData:
    course_id: int
    name: str
    location: str
    category: str
    sub_category: str
    sign_method: str
    has_autonomous_sign: bool
    selected: bool
    chosen_id: int | None
    course_start: str | None
    course_end: str | None
    select_start: str | None
    select_end: str | None
    cancel_end: str | None
    current_count: int | None
    max_count: int | None
    campus: str
    resolved_campus: str = ""
    sign_config: dict[str, Any] = field(default_factory=dict)
    sign_type: int | None = None
    checkin: int | None = None
    pass_status: int | None = None
    score: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BoyaCategoryStat:
    category: str
    sub_category: str
    required_count: int
    passed_count: int


@dataclass
class BoyaStatsData:
    valid_count: int
    categories: list[BoyaCategoryStat]
    raw: dict[str, Any]


def parse_sign_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.replace('\\"', '"')
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def parse_boya_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(value[:19], fmt)
            return naive.replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def _parse_campus_str(raw_campus: Any) -> str:
    if isinstance(raw_campus, list):
        return ",".join(str(x) for x in raw_campus if x)
    if raw_campus is None:
        return ""
    return str(raw_campus)


def parse_course(raw: dict[str, Any]) -> BoyaCourseData:
    sign_config = parse_sign_config(raw.get("courseSignConfig"))
    points = sign_config.get("signPointList") if isinstance(sign_config, dict) else None
    sign_type = parse_int(raw.get("courseSignType"))
    has_auto = (isinstance(points, list) and len(points) > 0) or sign_type == 2
    sign_method = "自主签到" if has_auto else "常规签到"

    kind1 = raw.get("courseNewKind1")
    cat1 = (
        kind1.get("kindName")
        if isinstance(kind1, dict)
        else (raw.get("courseKind") or "")
    )
    kind2 = raw.get("courseNewKind2")
    cat2 = (
        kind2.get("kindName")
        if isinstance(kind2, dict)
        else (raw.get("kindName") or raw.get("courseKind") or "")
    )
    course = BoyaCourseData(
        course_id=int(raw.get("id") or 0),
        name=str(raw.get("courseName") or raw.get("name") or ""),
        location=str(raw.get("coursePosition") or ""),
        category=str(cat1 or ""),
        sub_category=normalize_sub_category(str(cat2 or "")),
        sign_method=sign_method,
        has_autonomous_sign=has_auto,
        selected=bool(raw.get("selected", raw.get("isSelected", False))),
        chosen_id=parse_int(raw.get("chosenCourseId")),
        course_start=raw.get("courseStartDate"),
        course_end=raw.get("courseEndDate"),
        select_start=raw.get("courseSelectStartDate"),
        select_end=raw.get("courseSelectEndDate"),
        cancel_end=raw.get("courseCancelEndDate"),
        current_count=parse_int(raw.get("courseCurrentCount")),
        max_count=parse_int(raw.get("courseMaxCount")),
        campus=_parse_campus_str(raw.get("courseCampusList")),
        sign_config=sign_config,
        sign_type=sign_type,
        checkin=parse_int(raw.get("checkin")),
        pass_status=parse_int(raw.get("pass")),
        score=parse_int(raw.get("score")),
        raw=raw,
    )
    course.resolved_campus = infer_course_campus(course)
    return course


def parse_chosen_course(raw: dict[str, Any]) -> BoyaCourseData:
    course_info = raw.get("courseInfo")
    if isinstance(course_info, dict):
        merged = dict(course_info)
        merged["selected"] = True
        merged["chosenCourseId"] = raw.get("id")
        for key in ("selectDate", "checkin", "score", "pass", "signInfo"):
            if key in raw:
                merged[key] = raw[key]
        return parse_course(merged)
    return parse_course(raw)


def parse_statistics(raw: dict[str, Any]) -> BoyaStatsData:
    valid_count = parse_int(raw.get("validCount")) or 0
    categories: list[BoyaCategoryStat] = []
    statistical = raw.get("statistical")
    if isinstance(statistical, dict):
        for cat_key, children in statistical.items():
            if not isinstance(children, dict):
                continue
            cat_name = cat_key.split("|", 1)[1] if "|" in cat_key else cat_key
            for sub_key, entry in children.items():
                if not isinstance(entry, dict):
                    continue
                sub_name = normalize_sub_category(sub_key)
                required = parse_int(entry.get("assessmentCount")) or 0
                passed = parse_int(entry.get("completeAssessmentCount")) or 0
                categories.append(
                    BoyaCategoryStat(
                        category=cat_name,
                        sub_category=sub_name,
                        required_count=required,
                        passed_count=passed,
                    )
                )
    return BoyaStatsData(valid_count=valid_count, categories=categories, raw=raw)


_SHAHE_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:J\d+|SH\d+|教\d+|实\d+)-\d+", re.IGNORECASE
)
_XUEYUANLU_MAIN_RE = re.compile(r"主(?:楼|南|北|中|东|西|[A-Za-z])?\d+", re.IGNORECASE)
_XUEYUANLU_WING_RE = re.compile(r"(?<![A-Za-z0-9])[A-H]\d{3}(?![-\dA-Za-z])")

_XUEYUANLU_LOCATION_KEYWORDS = (
    "晨兴音乐厅",
    "音乐厅",
    "新主楼",
    "学术交流厅",
    "如心",
    "知行",
    "校史馆",
    "校友之家",
    "逸夫馆",
    "绿园",
    "教学（三）",
    "（三）",
    "(三)",
)


def infer_course_campus(course: BoyaCourseData) -> str:
    """Infer actual campus: prefer explicit open campus, then fall back to location."""
    raw_campuses = [p.strip() for p in (course.campus or "").split(",") if p.strip()]
    if len(raw_campuses) == 1 and "全部" not in raw_campuses[0]:
        c0 = raw_campuses[0]
        if "沙河" in c0:
            return "沙河校区"
        if "学院路" in c0:
            return "学院路校区"
        if "杭州" in c0:
            return "杭州校区"
        return c0

    loc = (course.location or "").strip()
    if "沙河" in loc:
        return "沙河校区"
    if "学院路" in loc:
        return "学院路校区"
    if "杭州" in loc:
        return "杭州校区"

    if "咏曼剧场" in loc or "咏曼" in loc or _SHAHE_CODE_RE.search(loc):
        return "沙河校区"

    if (
        any(kw in loc for kw in _XUEYUANLU_LOCATION_KEYWORDS)
        or _XUEYUANLU_MAIN_RE.search(loc)
        or _XUEYUANLU_WING_RE.search(loc)
    ):
        return "学院路校区"

    if "线上" in loc or "网络" in loc:
        return "线上"

    name = (course.name or "").strip()
    if "杭州" in name:
        return "杭州校区"
    has_sh = "沙河" in name
    has_xyl = "学院路" in name
    if has_sh and not has_xyl:
        return "沙河校区"
    if has_xyl and not has_sh:
        return "学院路校区"

    if len(raw_campuses) > 1:
        return ",".join(raw_campuses)
    if "全部" in (course.campus or ""):
        return "全部校区"
    return course.campus or "全部校区"


def normalize_user_campus(user_campus: str | None) -> str:
    val = (user_campus or "").strip()
    if "杭州" in val:
        return "杭州"
    if "沙河" in val:
        return "沙河"
    if "学院路" in val:
        return "学院路"
    return "北京"


def course_matches_campus(course: BoyaCourseData, user_campus: str) -> bool:
    resolved = course.resolved_campus or infer_course_campus(course)
    target = normalize_user_campus(user_campus)

    if target == "杭州":
        if resolved == "杭州校区":
            return True
        if resolved in ("沙河校区", "学院路校区"):
            return False
        combined = f"{course.name} {course.location} {course.campus}"
        return "杭州" in combined

    if target == "沙河":
        if resolved == "沙河校区":
            return True
        if resolved in ("学院路校区", "杭州校区"):
            return False
        return "全部" in course.campus or not course.campus or "沙河" in course.campus

    if target == "学院路":
        if resolved == "学院路校区":
            return True
        if resolved in ("沙河校区", "杭州校区"):
            return False
        return "全部" in course.campus or not course.campus or "学院路" in course.campus

    # 北京（学院路 + 沙河）
    if resolved == "杭州校区":
        return False
    if resolved in ("沙河校区", "学院路校区"):
        return True
    if course.campus and "全部" not in course.campus:
        return any(k in course.campus for k in ("学院路", "沙河", "北京"))
    combined = f"{course.name} {course.location} {course.campus}"
    return "杭州" not in combined


def random_point_in_radius(
    lat: float, lng: float, radius_m: float, seed: int | None = None
) -> tuple[float, float]:
    import random

    rng = random.Random(seed)
    distance = radius_m * math.sqrt(rng.random()) * 0.65
    theta = rng.random() * 2 * math.pi
    dlat = (distance * math.sin(theta)) / 111_320
    dlng = (distance * math.cos(theta)) / (
        111_320 * max(0.2, math.cos(math.radians(lat)))
    )
    return lat + dlat, lng + dlng


def resolve_sign_coordinates(
    sign_config: dict[str, Any],
) -> tuple[float, float]:
    points = sign_config.get("signPointList") if isinstance(sign_config, dict) else None
    if isinstance(points, list):
        valid_points = [
            p
            for p in points
            if isinstance(p, dict)
            and isinstance(p.get("lat"), (int, float))
            and isinstance(p.get("lng"), (int, float))
        ]
        if valid_points:
            pt = secrets.choice(valid_points)
            radius = float(pt.get("radius") or 10.0)
            if radius > 0:
                return random_point_in_radius(
                    float(pt["lat"]), float(pt["lng"]), radius
                )
            return float(pt["lat"]), float(pt["lng"])
    raise BusinessTerminalError("课程平台缺少自主签到位置配置")


def _extract_token_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        direct = from_webvpn_url(url)
    except Exception:
        direct = url
    parsed = urlparse(direct)
    token = parse_qs(parsed.query).get("token", [None])[0]
    if token:
        return token
    return None


def _map_boya_error(errmsg: str, status: Any) -> BuaaError:
    text = errmsg or ""
    if str(status) == "98005399" or "会话已失效" in text or "重新登录" in text:
        return SessionExpiredError(text or "博雅会话已失效")
    for terminal_kw in (
        "容量已满",
        "人数已满",
        "名额已满",
        "选课上限",
        "达到上限",
        "未到选课时间",
        "不在选课时间",
        "已选",
        "重复选择",
        "选课已结束",
    ):
        if terminal_kw in text:
            return BusinessTerminalError(text)
    return BuaaError(f"博雅接口错误 (status={status}): {text}")


class BoyaClient:
    """Adapter for BUAA Boya (bykc.buaa.edu.cn)."""

    def __init__(
        self, session_mgr: BuaaSessionManager, network_mode: str = "webvpn"
    ) -> None:
        self.session_mgr = session_mgr
        self.network_mode = network_mode

    @property
    def _state_key(self) -> str:
        return f"bykc:{self.network_mode}"

    async def _ensure_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self.session_mgr.load_domain_state(self._state_key)
            if cached.get("token"):
                return str(cached["token"])

        async with self.session_mgr.build_http_client(
            self.network_mode, follow_redirects=False
        ) as client:
            for start_url in (BYKC_CAS_LOGIN, BYKC_CAS_EMPTY_TOKEN):
                token = await self._follow_for_token(
                    client, route_url(start_url, self.network_mode)
                )
                if token:
                    self.session_mgr.save_domain_state(
                        self._state_key, {"token": token}
                    )
                    return token
        raise SessionExpiredError("无法获取博雅业务 token，可能需要重新登录 SSO")

    async def _follow_for_token(
        self, client: httpx.AsyncClient, start_url: str, max_redirects: int = 10
    ) -> str | None:
        current = start_url
        for _ in range(max_redirects):
            resp = await client.get(current)
            final_url = str(resp.url)
            token = _extract_token_from_url(final_url) or _extract_token_from_url(
                resp.headers.get("location", "")
            )
            if token:
                return token
            if looks_like_login_page(resp.text) or is_vpn_gateway_url(final_url):
                return None
            loc = resp.headers.get("location")
            if not (300 <= resp.status_code <= 399 and loc):
                return None
            joined = urljoin(final_url, loc)
            token = _extract_token_from_url(joined)
            if token:
                return token
            if is_vpn_gateway_url(joined):
                return None
            current = route_url(from_webvpn_url(joined), self.network_mode)
        return None

    async def call(
        self,
        api: str,
        payload: dict[str, Any] | None = None,
        *,
        non_idempotent: bool = False,
    ) -> dict[str, Any]:
        for attempt in range(2):
            try:
                token = await self._ensure_token(force_refresh=(attempt > 0))
            except SessionExpiredError:
                if attempt > 0 or non_idempotent:
                    raise
                await self.session_mgr.login_sso(network_mode=self.network_mode)
                token = await self._ensure_token(force_refresh=True)

            crypto = BykcCrypto()
            enc = crypto.encrypt_request(payload or {})
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": route_url(BYKC_REFERER, self.network_mode),
                "Origin": route_url(BYKC_ORIGIN, self.network_mode),
                "Authtoken": token,
                "auth_token": token,
                "authtoken": token,
                **enc.headers,
            }
            url = route_url(f"{BYKC_BASE}/{api}", self.network_mode)
            async with self.session_mgr.build_http_client(
                self.network_mode, follow_redirects=True
            ) as client:
                try:
                    resp = await client.post(url, content=enc.body, headers=headers)
                except httpx.TimeoutException as exc:
                    if non_idempotent:
                        raise OutcomeUnknownError(
                            f"博雅操作 {api} 超时，结果待确认"
                        ) from exc
                    raise BuaaError(f"博雅请求 {api} 超时: {exc}") from exc

                if (
                    resp.status_code != 200
                    or looks_like_login_page(resp.text)
                    or is_vpn_gateway_url(str(resp.url))
                ):
                    self.session_mgr.clear_domain_state(self._state_key)
                    if non_idempotent:
                        raise OutcomeUnknownError(
                            f"博雅操作 {api} 响应异常，结果待确认"
                        )
                    if attempt == 0:
                        await self.session_mgr.login_sso(network_mode=self.network_mode)
                        continue
                    raise SessionExpiredError("博雅会话已失效")

                try:
                    decoded = crypto.decrypt_response(resp.content)
                except Exception:
                    try:
                        decoded = resp.json()
                    except Exception as exc:
                        if non_idempotent:
                            raise OutcomeUnknownError(
                                f"博雅操作 {api} 响应无法解密，结果待确认"
                            ) from exc
                        raise BuaaError(f"无法解密博雅响应 ({api})") from exc

                status = decoded.get("status") if isinstance(decoded, dict) else None
                errmsg = (
                    (decoded.get("errmsg") or decoded.get("msg") or "")
                    if isinstance(decoded, dict)
                    else ""
                )
                if status not in ("0", 0, "200", 200, None):
                    err = _map_boya_error(str(errmsg), status)
                    if isinstance(err, SessionExpiredError):
                        self.session_mgr.clear_domain_state(self._state_key)
                        if attempt == 0 and not non_idempotent:
                            continue
                    raise err
                return decoded
        raise SessionExpiredError("博雅会话刷新失败")

    async def get_all_config(self) -> dict[str, Any]:
        res = await self.call("getAllConfig", {})
        return res.get("data") or {}

    async def query_courses(self, page_size: int = 100) -> list[BoyaCourseData]:
        courses: list[BoyaCourseData] = []
        page = 1
        while True:
            resp = await self.call(
                "queryStudentSemesterCourseByPage",
                {"pageNumber": page, "pageSize": page_size},
            )
            page_data = resp.get("data") or {}
            content = page_data.get("content") or []
            courses.extend(parse_course(item) for item in content)
            total_pages = parse_int(page_data.get("totalPages"))
            total_elements = parse_int(page_data.get("totalElements"))
            if page_data.get("last") is True:
                break
            if total_elements is not None and len(courses) >= total_elements:
                break
            if total_pages is not None and page >= total_pages:
                break
            if not content:
                break
            page += 1
        return courses

    async def query_course_detail(self, course_id: int) -> BoyaCourseData:
        resp = await self.call("queryCourseById", {"id": course_id})
        return parse_course(resp.get("data") or {})

    async def query_chosen_courses(
        self, start_date: str, end_date: str
    ) -> list[BoyaCourseData]:
        resp = await self.call(
            "queryChosenCourse", {"startDate": start_date, "endDate": end_date}
        )
        items = (resp.get("data") or {}).get("courseList") or []
        return [parse_chosen_course(item) for item in items]

    async def query_statistics(self) -> BoyaStatsData:
        resp = await self.call("queryStatisticByUserId", {})
        return parse_statistics(resp.get("data") or {})

    async def select_course(self, course_id: int) -> dict[str, Any]:
        return await self.call(
            "choseCourse", {"courseId": course_id}, non_idempotent=True
        )

    async def drop_course(self, chosen_id: int) -> dict[str, Any]:
        return await self.call(
            "delChosenCourse", {"id": chosen_id}, non_idempotent=True
        )

    async def sign_course(
        self, course_id: int, lat: float, lng: float, sign_type: int
    ) -> dict[str, Any]:
        return await self.call(
            "signCourseByUser",
            {
                "courseId": course_id,
                "signLat": lat,
                "signLng": lng,
                "signType": sign_type,
            },
            non_idempotent=True,
        )


def resolve_semester_window(
    config: dict[str, Any], now_shanghai: datetime
) -> tuple[str, str]:
    semesters = config.get("semester") or []
    for sem in semesters:
        s = parse_boya_dt(sem.get("semesterStartDate"))
        e = parse_boya_dt(sem.get("semesterEndDate"))
        if s and e and s <= now_shanghai <= e:
            return sem["semesterStartDate"], sem["semesterEndDate"]
    if semesters:
        latest = max(
            semesters,
            key=lambda x: x.get("semesterEndDate") or "",
        )
        if latest.get("semesterStartDate") and latest.get("semesterEndDate"):
            return latest["semesterStartDate"], latest["semesterEndDate"]
    year = now_shanghai.year
    if now_shanghai.month >= 8:
        return f"{year}-08-01 00:00:00", f"{year + 1}-02-28 23:59:59"
    return f"{year}-01-01 00:00:00", f"{year}-08-31 23:59:59"
