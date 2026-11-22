# Unified BUAA SSO + WebVPN session manager.
from __future__ import annotations

import html.parser
import json
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from app.core.crypto import SecretCipher
from app.core.time import Clock, default_clock
from app.db.models import CampusAccount, NotificationEvent, SessionState
from app.notifications.service import publish_event
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa

from .exceptions import CaptchaChallenge, CaptchaRequiredError, LoginFailedError
from .webvpn import SANGFOR_VPN_GATEWAY, is_vpn_gateway_url, route_url

SSO_LOGIN_URL = "https://sso.buaa.edu.cn/login"
SSO_CAPTCHA_URL = "https://sso.buaa.edu.cn/captcha"
UC_ACTIVATE_URL = "https://uc.buaa.edu.cn/api/login?target=https%3A%2F%2Fuc.buaa.edu.cn%2F%23%2Fuser%2Flogin"
UC_USERINFO_URL = "https://uc.buaa.edu.cn/api/uc/userinfo"

SANGFOR_LOGIN_AUTH_URL = f"{SANGFOR_VPN_GATEWAY}/por/login_auth.csp?apiversion=1"
SANGFOR_PSW_CONFIG_URL = f"{SANGFOR_VPN_GATEWAY}/public/psw_config?apiversion=1"
SANGFOR_LOGIN_PSW_URL = (
    f"{SANGFOR_VPN_GATEWAY}/por/login_psw.csp?anti_replay=1&encrypt=1&apiversion=1"
)
SANGFOR_RAND_CODE_URL = f"{SANGFOR_VPN_GATEWAY}/por/rand_code.csp"
SANGFOR_TIMEQUERY_URL = f"{SANGFOR_VPN_GATEWAY}/por/timequery.csp?apiversion=1"
SANGFOR_CAPTCHA_ERROR_CODES = {"20023", "20041", "20043", "20053"}


def parse_sangfor_auth_xml(xml_text: str) -> dict[str, str]:
    """Parse Sangfor SSL VPN XML <Auth> response into a flat tag->text dict."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return {}
    result: dict[str, str] = {}
    for elem in root:
        result[elem.tag] = (elem.text or "").strip()
    return result


def encrypt_sangfor_password(
    password: str,
    csrf_rand_code: str,
    rsa_key_hex: str,
    rsa_exp: int | str = 65537,
) -> str:
    """Encrypt '<password>_<csrf_rand_code>' with Sangfor RSA public key (PKCS#1 v1.5)."""
    modulus = int(rsa_key_hex, 16)
    exponent = int(rsa_exp or 65537)
    pub_key = rsa.RSAPublicNumbers(e=exponent, n=modulus).public_key()
    plaintext = f"{password}_{csrf_rand_code}".encode("utf-8")
    encrypted = pub_key.encrypt(plaintext, asym_padding.PKCS1v15())
    return encrypted.hex()


def _sync_sangfor_twfid_cookie(client: httpx.AsyncClient, twfid: str) -> None:
    """Ensure TWFID cookie is scoped to .vpn.buaa.edu.cn so all WebVPN subdomains receive it."""
    if not twfid:
        return
    to_delete = [
        (c.domain, c.path)
        for c in client.cookies.jar
        if c.name == "TWFID" and c.domain != ".vpn.buaa.edu.cn"
    ]
    for dom, path in to_delete:
        try:
            client.cookies.jar.clear(domain=dom, path=path, name="TWFID")
        except KeyError:
            pass
    client.cookies.set("TWFID", twfid, domain=".vpn.buaa.edu.cn", path="/")


class _FormParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: (value or "") for key, value in attrs}
        if tag.lower() == "form":
            self.current = {"attrs": attrs_dict, "inputs": []}
            self.forms.append(self.current)
        elif tag.lower() == "input" and self.current is not None:
            self.current["inputs"].append(attrs_dict)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self.current = None


def detect_captcha(html: str) -> tuple[str, str] | None:
    match = re.search(
        r"config\.captcha\s*=\s*\{\s*type:\s*['\"]([^'\"]+)['\"],\s*id:\s*['\"]([^'\"]+)['\"]",
        html,
    )
    return (match.group(1), match.group(2)) if match else None


def extract_execution(html: str) -> str:
    match = re.search(r'name=["\']execution["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not match:
        match = re.search(
            r'value=["\']([^"\']+)["\'][^>]*name=["\']execution["\']', html
        )
    return match.group(1) if match else ""


def build_login_params(
    html: str, username: str, password: str, captcha: str | None = None
) -> dict[str, str]:
    parser = _FormParser()
    parser.feed(html)
    form = parser.forms[0] if parser.forms else {"inputs": []}
    params: dict[str, str] = {}
    for attrs in form.get("inputs", []):
        name = (attrs.get("name") or "").strip()
        if not name or name in {"username", "password"}:
            continue
        input_type = (attrs.get("type") or "").strip().lower()
        value = attrs.get("value") or ""
        if input_type in {"submit", "button", "image"}:
            continue
        if value:
            params[name] = value
    params["username"] = username
    params["password"] = password
    params["submit"] = "登录"
    params.setdefault("type", "username_password")
    params.setdefault("execution", extract_execution(html))
    params.setdefault("_eventId", "submit")
    if captcha:
        params["captcha"] = captcha
        params["captchaResponse"] = captcha
    return params


def translate_sso_error(msg: str | None) -> str | None:
    if not msg:
        return None
    if re.search(r"invalid credentials", msg, re.IGNORECASE):
        return "统一认证账号或密码错误"
    if re.search(r"access denied", msg, re.IGNORECASE):
        return "访问被拒绝"
    return msg


def find_login_error(html: str) -> str | None:
    for pattern in [
        r"Invalid credentials\.",
        r"Access Denied[^<\"]*",
        r"<div class=\"tip-text\">([^<]+)</div>",
        r"<p[^>]*>([^<]*(?:错误|密码|验证码|失败)[^<]*)</p>",
    ]:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return re.sub(
                r"\s+", " ", match.group(1) if match.groups() else match.group(0)
            ).strip()
    return None


def serialize_cookies(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in client.cookies.jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
        )
    return cookies


def restore_cookies(client: httpx.AsyncClient, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        client.cookies.set(
            name,
            value,
            domain=cookie.get("domain")
            if isinstance(cookie.get("domain"), str)
            else None,
            path=cookie.get("path") if isinstance(cookie.get("path"), str) else "/",
        )


def looks_like_login_page(text: str) -> bool:
    head = text[:1200].lower()
    return "<html" in head and (
        "cas login" in head
        or "loginform" in head
        or "sso.buaa.edu.cn" in head
        or 'id="fm1"' in head
    )


class BuaaSessionManager:
    """Manages persisted SSO/WebVPN/business sessions and interactive captcha recovery."""

    def __init__(
        self,
        session_factory: Any,
        cipher: SecretCipher,
        captcha_dir: Path,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.captcha_dir = captcha_dir
        self.clock = clock or default_clock
        self.transport = transport

    def load_domain_state(self, domain: str) -> dict[str, Any]:
        with self.session_factory() as session:
            row = session.get(SessionState, domain)
            if row is None:
                return {}
            raw = self.cipher.decrypt(row.encrypted_payload)
            return json.loads(raw) if raw else {}

    def save_domain_state(self, domain: str, data: dict[str, Any]) -> None:
        payload = self.cipher.encrypt(json.dumps(data, ensure_ascii=False))
        assert payload is not None
        with self.session_factory() as session:
            row = session.get(SessionState, domain)
            if row is None:
                row = SessionState(domain=domain, encrypted_payload=payload)
                session.add(row)
            else:
                row.encrypted_payload = payload
            session.commit()

    def clear_domain_state(self, domain: str) -> None:
        with self.session_factory() as session:
            row = session.get(SessionState, domain)
            if row is not None:
                session.delete(row)
                session.commit()

    def build_http_client(
        self, network_mode: str, follow_redirects: bool = False
    ) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=follow_redirects,
            transport=self.transport,
        )
        sso_state = self.load_domain_state(f"sso:{network_mode}")
        cookies = sso_state.get("cookies") or []
        if cookies:
            restore_cookies(client, cookies)
            if network_mode == "vpn_buaa":
                twfid = client.cookies.get("TWFID")
                if twfid:
                    _sync_sangfor_twfid_cookie(client, twfid)
        return client

    def save_http_cookies(self, network_mode: str, client: httpx.AsyncClient) -> None:
        self.save_domain_state(
            f"sso:{network_mode}",
            {"cookies": serialize_cookies(client)},
        )

    async def _raise_sangfor_captcha(
        self,
        client: httpx.AsyncClient,
        network_mode: str,
        *,
        csrf_rand_code: str,
        rsa_key: str,
        rsa_exp: str,
        error_msg: str | None = None,
    ) -> None:
        img_resp = await client.get(
            f"{SANGFOR_RAND_CODE_URL}?rnd={secrets.token_hex(4)}"
        )
        img_resp.raise_for_status()
        ct = img_resp.headers.get("content-type", "image/gif")
        if "png" in ct.lower():
            ext = ".png"
        elif "gif" in ct.lower():
            ext = ".gif"
        else:
            ext = ".jpg"
        captcha_id = f"vpn-{secrets.token_hex(6)}"
        self.captcha_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.captcha_dir / f"{captcha_id}{ext}"
        img_path.write_bytes(img_resp.content)

        twfid = client.cookies.get("TWFID") or ""
        if twfid:
            _sync_sangfor_twfid_cookie(client, twfid)
        self.save_http_cookies(network_mode, client)
        self.save_domain_state(
            f"vpn_pending:{network_mode}",
            {
                "csrf_rand_code": csrf_rand_code,
                "rsa_key": rsa_key,
                "rsa_exp": rsa_exp,
            },
        )
        challenge = CaptchaChallenge(
            captcha_id=captcha_id,
            captcha_type="sangfor_vpn",
            execution=csrf_rand_code,
            image_path=img_path,
            image_bytes=img_resp.content,
            mime_type=ct,
        )
        self._mark_captcha_required(
            challenge, error_msg=error_msg or "vpn.buaa.edu.cn 需要输入验证码"
        )
        raise CaptchaRequiredError(
            challenge, error_msg or "vpn.buaa.edu.cn 需要输入验证码"
        )

    async def _ensure_sangfor_vpn_login(
        self,
        client: httpx.AsyncClient,
        network_mode: str,
        student_id: str,
        password: str,
        *,
        captcha_code: str | None = None,
        force_refresh: bool = False,
    ) -> bool:
        """Ensure Sangfor SSL VPN session is authenticated; return True if captcha_code was consumed."""
        pending = self.load_domain_state(f"vpn_pending:{network_mode}")
        if pending and captcha_code and not force_refresh:
            csrf = str(pending.get("csrf_rand_code") or "")
            rsa_key = str(pending.get("rsa_key") or "")
            rsa_exp = str(pending.get("rsa_exp") or "65537")
            used_captcha = True
        else:
            if not force_refresh and client.cookies.get("TWFID"):
                tq_resp = await client.get(SANGFOR_TIMEQUERY_URL)
                tq_data = parse_sangfor_auth_xml(tq_resp.text)
                ttl_raw = (tq_data.get("data") or "").strip()
                if (
                    tq_data.get("ErrorCode") == "1"
                    and ttl_raw.isdigit()
                    and int(ttl_raw) > 0
                ):
                    twfid = tq_data.get("twfID") or client.cookies.get("TWFID") or ""
                    _sync_sangfor_twfid_cookie(client, twfid)
                    return False

            auth_resp = await client.get(SANGFOR_LOGIN_AUTH_URL)
            if auth_resp.status_code >= 400:
                raise LoginFailedError(
                    f"vpn.buaa.edu.cn 登录初始化失败 (HTTP {auth_resp.status_code})"
                )
            auth_data = parse_sangfor_auth_xml(auth_resp.text)

            cfg_resp = await client.get(SANGFOR_PSW_CONFIG_URL)
            if cfg_resp.status_code >= 400:
                raise LoginFailedError(
                    f"vpn.buaa.edu.cn 获取密码配置失败 (HTTP {cfg_resp.status_code})"
                )
            cfg_data = parse_sangfor_auth_xml(cfg_resp.text)

            rsa_key = (
                cfg_data.get("RSA_ENCRYPT_KEY")
                or auth_data.get("RSA_ENCRYPT_KEY")
                or ""
            )
            rsa_exp = (
                cfg_data.get("RSA_ENCRYPT_EXP")
                or auth_data.get("RSA_ENCRYPT_EXP")
                or "65537"
            )
            csrf = (
                cfg_data.get("CSRF_RAND_CODE") or auth_data.get("CSRF_RAND_CODE") or ""
            )
            if not rsa_key or not csrf:
                raise LoginFailedError("vpn.buaa.edu.cn 登录配置缺少 RSA 公钥或随机码")

            need_captcha = (
                cfg_data.get("USE_RAND_CODE") == "1" or auth_data.get("RndImg") == "1"
            )
            if need_captcha and not captcha_code:
                await self._raise_sangfor_captcha(
                    client,
                    network_mode,
                    csrf_rand_code=csrf,
                    rsa_key=rsa_key,
                    rsa_exp=rsa_exp,
                )
            used_captcha = bool(captcha_code)

        enc_password = encrypt_sangfor_password(password, csrf, rsa_key, rsa_exp)
        post_resp = await client.post(
            SANGFOR_LOGIN_PSW_URL,
            data={
                "mitm_result": "",
                "svpn_req_randcode": csrf,
                "svpn_name": student_id,
                "svpn_password": enc_password,
                "svpn_rand_code": captcha_code or "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        login_data = parse_sangfor_auth_xml(post_resp.text)
        err_code = login_data.get("ErrorCode") or ""
        if err_code in {"1", "20021"}:
            twfid = login_data.get("TwfID") or client.cookies.get("TWFID") or ""
            _sync_sangfor_twfid_cookie(client, twfid)
            self.clear_domain_state(f"vpn_pending:{network_mode}")
            self.save_http_cookies(network_mode, client)
            return used_captcha

        if err_code in SANGFOR_CAPTCHA_ERROR_CODES:
            next_csrf = login_data.get("CSRF_RAND_CODE") or csrf
            err_msg = (
                login_data.get("Note")
                or login_data.get("Message")
                or "vpn.buaa.edu.cn 需要输入验证码"
            )
            await self._raise_sangfor_captcha(
                client,
                network_mode,
                csrf_rand_code=next_csrf,
                rsa_key=rsa_key,
                rsa_exp=rsa_exp,
                error_msg=err_msg if used_captcha else None,
            )

        self.clear_domain_state(f"vpn_pending:{network_mode}")
        err_msg = (
            login_data.get("Message")
            or login_data.get("Note")
            or f"vpn.buaa.edu.cn 登录失败 (ErrorCode={err_code or post_resp.status_code})"
        )
        self._mark_login_failed(err_msg)
        raise LoginFailedError(translate_sso_error(err_msg) or err_msg)

    async def login_sso(
        self,
        *,
        network_mode: str,
        captcha_code: str | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            account = session.query(CampusAccount).first()
            if (
                account is None
                or not account.student_id
                or not account.encrypted_password
            ):
                raise LoginFailedError("尚未配置北航学号与密码")
            student_id = account.student_id
            password = self.cipher.decrypt(account.encrypted_password) or ""

        async with self.build_http_client(
            network_mode, follow_redirects=False
        ) as client:
            sso_captcha_code = captcha_code
            if network_mode == "vpn_buaa":
                vpn_consumed = await self._ensure_sangfor_vpn_login(
                    client,
                    network_mode,
                    student_id,
                    password,
                    captcha_code=captcha_code,
                )
                if vpn_consumed:
                    sso_captcha_code = None

            login_url = route_url(SSO_LOGIN_URL, network_mode)
            pending_key = f"sso_pending:{network_mode}"
            html = ""
            if sso_captcha_code:
                # Resume the challenge-issuing login page: a fresh GET /login
                # would issue a new captchaId and invalidate the typed code.
                html = str(
                    self.load_domain_state(pending_key).get("html") or ""
                )
            if not html:
                get_resp = await client.get(login_url)
                if (
                    network_mode == "vpn_buaa"
                    and 300 <= get_resp.status_code <= 399
                    and is_vpn_gateway_url(get_resp.headers.get("location", ""))
                ):
                    # Cached TWFID was rejected by WebVPN reverse proxy; re-auth Sangfor once.
                    await self._ensure_sangfor_vpn_login(
                        client,
                        network_mode,
                        student_id,
                        password,
                        captcha_code=sso_captcha_code,
                        force_refresh=True,
                    )
                    sso_captcha_code = None
                    get_resp = await client.get(login_url)

                if get_resp.status_code >= 400:
                    raise LoginFailedError(
                        f"统一认证登录页返回 HTTP {get_resp.status_code}"
                    )

                html = get_resp.text
                # If already logged in via cookies, CAS may redirect directly
                if 300 <= get_resp.status_code <= 399 and get_resp.headers.get(
                    "location"
                ):
                    followed = await self._follow_redirects(client, get_resp)
                    if not looks_like_login_page(
                        followed.text
                    ) and not is_vpn_gateway_url(str(followed.url)):
                        self.clear_domain_state(pending_key)
                        self.save_http_cookies(network_mode, client)
                        info = await self._activate_and_userinfo(
                            client, network_mode
                        )
                        self._mark_login_success(info.get("name"))
                        return info

                captcha = detect_captcha(html)
                # A code reaching this branch has no stored challenge page to
                # resume, so it cannot match this fresh captchaId; re-issue.
                if captcha:
                    captcha_type, captcha_id = captcha
                    execution = extract_execution(html)
                    img_resp = await client.get(
                        route_url(
                            f"{SSO_CAPTCHA_URL}?captchaId={captcha_id}", network_mode
                        )
                    )
                    img_resp.raise_for_status()
                    ct = img_resp.headers.get("content-type", "image/jpeg")
                    ext = ".png" if "png" in ct.lower() else ".jpg"
                    self.captcha_dir.mkdir(parents=True, exist_ok=True)
                    img_path = self.captcha_dir / f"{captcha_id}{ext}"
                    img_path.write_bytes(img_resp.content)
                    self.save_http_cookies(network_mode, client)
                    self.save_domain_state(
                        pending_key, {"html": html, "captcha_id": captcha_id}
                    )

                    challenge = CaptchaChallenge(
                        captcha_id=captcha_id,
                        captcha_type=captcha_type,
                        execution=execution,
                        image_path=img_path,
                        image_bytes=img_resp.content,
                        mime_type=ct,
                    )
                    self._mark_captcha_required(challenge)
                    raise CaptchaRequiredError(challenge)

            params = build_login_params(html, student_id, password, sso_captcha_code)
            post_resp = await client.post(
                login_url,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            final = await self._follow_redirects_and_password_expiry(client, post_resp)
            err = find_login_error(final.text)
            if final.status_code >= 400 or err:
                # Check if server returned a new captcha challenge
                next_captcha = detect_captcha(final.text)
                if next_captcha:
                    captcha_type, captcha_id = next_captcha
                    img_resp = await client.get(
                        route_url(
                            f"{SSO_CAPTCHA_URL}?captchaId={captcha_id}", network_mode
                        )
                    )
                    img_path = self.captcha_dir / f"{captcha_id}.jpg"
                    if img_resp.status_code == 200:
                        self.captcha_dir.mkdir(parents=True, exist_ok=True)
                        img_path.write_bytes(img_resp.content)
                    self.save_http_cookies(network_mode, client)
                    self.save_domain_state(
                        pending_key,
                        {"html": final.text, "captcha_id": captcha_id},
                    )
                    challenge = CaptchaChallenge(
                        captcha_id=captcha_id,
                        captcha_type=captcha_type,
                        execution=extract_execution(final.text),
                        image_path=img_path,
                    )
                    self._mark_captcha_required(challenge, error_msg=err)
                    raise CaptchaRequiredError(
                        challenge, err or "验证码校验未通过，请重新输入"
                    )
                self.clear_domain_state(pending_key)
                reason = (
                    translate_sso_error(err)
                    or f"统一认证失败 (HTTP {final.status_code})"
                )
                self._mark_login_failed(reason)
                raise LoginFailedError(reason)

            self.clear_domain_state(pending_key)
            self.save_http_cookies(network_mode, client)
            info = await self._activate_and_userinfo(client, network_mode)
            self._mark_login_success(info.get("name"))
            return info

    async def _follow_redirects(
        self, client: httpx.AsyncClient, response: httpx.Response, limit: int = 10
    ) -> httpx.Response:
        current = response
        for _ in range(limit):
            if 300 <= current.status_code <= 399 and current.headers.get("location"):
                target = urljoin(str(current.url), current.headers["location"])
                current = await client.get(target)
                continue
            break
        return current

    async def _follow_redirects_and_password_expiry(
        self, client: httpx.AsyncClient, response: httpx.Response
    ) -> httpx.Response:
        current = response
        ignored = False
        while True:
            current = await self._follow_redirects(client, current)
            if (
                "continueForm" in current.text or "ignoreAndContinue" in current.text
            ) and not ignored:
                execution = extract_execution(current.text)
                current = await client.post(
                    str(current.url).split("?", 1)[0],
                    data={"execution": execution, "_eventId": "ignoreAndContinue"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                ignored = True
                continue
            return current

    async def _activate_and_userinfo(
        self, client: httpx.AsyncClient, network_mode: str
    ) -> dict[str, Any]:
        try:
            act_resp = await client.get(route_url(UC_ACTIVATE_URL, network_mode))
            await self._follow_redirects(client, act_resp)
            info_resp = await client.get(route_url(UC_USERINFO_URL, network_mode))
            self.save_http_cookies(network_mode, client)
            if info_resp.status_code == 200:
                data = info_resp.json()
                inner = data.get("data") or data
                if isinstance(inner, dict):
                    return {"name": inner.get("name") or inner.get("realname") or ""}
        except Exception:
            pass
        return {}

    def _mark_captcha_required(
        self, challenge: CaptchaChallenge, error_msg: str | None = None
    ) -> None:
        reason = translate_sso_error(error_msg)
        with self.session_factory() as session:
            account = session.query(CampusAccount).first()
            if account:
                account.auth_status = "captcha_required"
                account.captcha_id = challenge.captcha_id
                account.captcha_type = challenge.captcha_type
                account.captcha_execution = challenge.execution
                account.captcha_image_path = (
                    str(challenge.image_path) if challenge.image_path else None
                )
                account.auth_error = reason or "统一认证需要输入验证码"
            latest = (
                session.query(NotificationEvent)
                .filter_by(event_type="auth_captcha_required")
                .order_by(NotificationEvent.id.desc())
                .first()
            )
            now_utc = self.clock.now_utc()
            already_notified = latest is not None and (
                latest.status == "pending"
                or (
                    latest.status == "sent"
                    and latest.created_at is not None
                    and latest.created_at > now_utc - timedelta(minutes=30)
                )
            )
            if not already_notified:
                publish_event(
                    session,
                    event_type="auth_captcha_required",
                    event_key=f"auth:captcha:{challenge.captcha_id}",
                    module="auth",
                    payload={
                        "title": "统一认证需要验证码",
                        "message": "请打开 autoBUAA 输入统一认证验证码"
                        + (f"\n原因: {reason}" if reason else ""),
                        "captcha_id": challenge.captcha_id,
                    },
                    clock=self.clock,
                )
            session.commit()

    def _mark_login_failed(self, error_msg: str) -> None:
        with self.session_factory() as session:
            account = session.query(CampusAccount).first()
            if account:
                account.auth_status = "failed"
                account.auth_error = translate_sso_error(error_msg) or error_msg
                session.commit()

    def _mark_login_success(self, display_name: str | None) -> None:
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            account = session.query(CampusAccount).first()
            if account:
                account.auth_status = "authenticated"
                account.auth_error = None
                account.captcha_id = None
                account.captcha_type = None
                account.captcha_execution = None
                account.captcha_image_path = None
                account.last_login_at = now_utc
                if display_name:
                    account.display_name = display_name
            for ev in (
                session.query(NotificationEvent)
                .filter_by(event_type="auth_captcha_required", status="pending")
                .all()
            ):
                ev.status = "expired"
                ev.next_retry_at = None
            session.commit()
