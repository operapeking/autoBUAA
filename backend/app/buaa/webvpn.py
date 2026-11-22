# WebVPN URL translation and host AES-CFB encryption for BUAA WebVPN and
# Sangfor SSL WebVPN (vpn.buaa.edu.cn).
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except Exception:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB

WEBVPN_GATEWAY = "https://d.buaa.edu.cn"
WEBVPN_HOST = "d.buaa.edu.cn"
WEBVPN_KEY = b"wrdvpnisthebest!"
SANGFOR_VPN_GATEWAY = "https://vpn.buaa.edu.cn"
SANGFOR_VPN_HOST = "vpn.buaa.edu.cn"
SANGFOR_VPN_SUFFIX = ".vpn.buaa.edu.cn"
GATEWAY_HOST = "gw.buaa.edu.cn"

_SANGFOR_WEB_PATH_RE = re.compile(
    r"^/(?:safeurl/)?web/\d+/(https?)/\d+/([^/?#]+)(.*)$", re.IGNORECASE
)
_SANGFOR_PORT_SUFFIX_RE = re.compile(r"^(.*)-(\d+)-p$")


def encrypt_webvpn_host(host: str) -> str:
    plain = host.encode("utf-8")
    padded = plain + b"0" * ((16 - len(plain) % 16) % 16)
    cipher = Cipher(algorithms.AES(WEBVPN_KEY), CFB(WEBVPN_KEY)).encryptor()
    encrypted = cipher.update(padded) + cipher.finalize()
    return WEBVPN_KEY.hex() + encrypted.hex()[: len(plain) * 2]


def decrypt_webvpn_host(encoded: str) -> str:
    prefix = WEBVPN_KEY.hex()
    if not encoded.startswith(prefix):
        raise ValueError("Invalid WebVPN host prefix")
    body_hex = encoded[len(prefix) :]
    if len(body_hex) % 2 != 0:
        raise ValueError("Invalid WebVPN host hex length")
    raw = bytes.fromhex(body_hex)
    pad_len = (16 - len(raw) % 16) % 16
    cipher = Cipher(algorithms.AES(WEBVPN_KEY), CFB(WEBVPN_KEY)).decryptor()
    decrypted = cipher.update(raw + b"\x00" * pad_len) + cipher.finalize()
    return decrypted[: len(raw)].decode("utf-8")


def to_webvpn_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == WEBVPN_HOST:
        return url
    if parsed.port is None:
        protocol = parsed.scheme
    elif parsed.scheme == "http" and parsed.port == 80:
        protocol = "http"
    elif parsed.scheme == "https" and parsed.port == 443:
        protocol = "https"
    else:
        protocol = f"{parsed.scheme}-{parsed.port}"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    host = encrypt_webvpn_host(parsed.hostname or "")
    return f"{WEBVPN_GATEWAY}/{protocol}/{host}{parsed.path}{query}{fragment}"


def to_sangfor_webvpn_url(url: str) -> str:
    """Translate a direct URL to Sangfor WebVPN subdomain format (*.vpn.buaa.edu.cn)."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == SANGFOR_VPN_HOST or hostname.endswith(SANGFOR_VPN_SUFFIX):
        return url
    is_https = parsed.scheme.lower() == "https"
    port = parsed.port
    has_custom_port = port is not None and (
        (is_https and port != 443) or (not is_https and port != 80)
    )
    encoded = hostname.replace("-", "--").replace(".", "-")
    if has_custom_port:
        encoded = f"{encoded}-{port}-p"
    if is_https:
        encoded = f"{encoded}-s"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"https://{encoded}{SANGFOR_VPN_SUFFIX}{parsed.path}{query}{fragment}"


def from_sangfor_webvpn_url(url: str) -> str:
    """Unwrap Sangfor WebVPN subdomain (*.vpn.buaa.edu.cn) or /web/ proxy URLs."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    if hostname == SANGFOR_VPN_HOST:
        match = _SANGFOR_WEB_PATH_RE.match(parsed.path)
        if not match:
            return url
        scheme, host_port, rest_path = (
            match.group(1).lower(),
            match.group(2),
            match.group(3),
        )
        return f"{scheme}://{host_port}{rest_path}{query}{fragment}"
    if hostname.endswith(SANGFOR_VPN_SUFFIX):
        label = hostname[: -len(SANGFOR_VPN_SUFFIX)]
        if label.endswith("-s"):
            scheme = "https"
            label = label[:-2]
        else:
            scheme = "http"
        port_match = _SANGFOR_PORT_SUFFIX_RE.match(label)
        if port_match:
            label = port_match.group(1)
            netloc_port = f":{port_match.group(2)}"
        else:
            netloc_port = ""
        orig_host = re.sub(r"(--)|-", lambda m: "-" if m.group(1) else ".", label)
        return f"{scheme}://{orig_host}{netloc_port}{parsed.path}{query}{fragment}"
    return url


def from_webvpn_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == SANGFOR_VPN_HOST or hostname.endswith(SANGFOR_VPN_SUFFIX):
        return from_sangfor_webvpn_url(url)
    if hostname != WEBVPN_HOST:
        return url
    parts = parsed.path.lstrip("/").split("/", 2)
    if len(parts) < 2:
        raise ValueError(f"Malformed WebVPN URL: {url}")
    proto_part, encoded_host = parts[0], parts[1]
    rest_path = "/" + parts[2] if len(parts) == 3 else ""
    if "-" in proto_part:
        scheme, port = proto_part.split("-", 1)
        netloc_port = f":{port}"
    else:
        scheme = proto_part
        netloc_port = ""
    host = decrypt_webvpn_host(encoded_host)
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{scheme}://{host}{netloc_port}{rest_path}{query}{fragment}"


def is_vpn_gateway_url(url: str) -> bool:
    """Return True when URL points to a WebVPN login/portal gateway rather than a proxied upstream."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if hostname == SANGFOR_VPN_HOST:
        return not _SANGFOR_WEB_PATH_RE.match(path)
    if hostname == WEBVPN_HOST:
        return (
            path == "/" or path.startswith("/login") or path.startswith("/wengine-vpn")
        )
    return False


def route_url(url: str, network_mode: str) -> str:
    if network_mode == "webvpn":
        return to_webvpn_url(url)
    if network_mode == "vpn_buaa":
        return to_sangfor_webvpn_url(url)
    return url


@dataclass
class EndpointDiagnostic:
    target_host: str
    target_port: int
    network_mode: str
    dns_ok: bool
    resolved_ips: list[str]
    connect_ok: bool
    http_status: int | None
    failed_step: str | None
    message: str


def diagnose_host_connection(
    target_url: str,
    network_mode: str,
    *,
    timeout_seconds: float = 4.0,
    transport: httpx.BaseTransport | None = None,
) -> EndpointDiagnostic:
    effective_url = route_url(target_url, network_mode)
    parsed = urlparse(effective_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if transport is not None:
        try:
            with httpx.Client(
                timeout=timeout_seconds, transport=transport, follow_redirects=False
            ) as client:
                resp = client.get(effective_url)
            return EndpointDiagnostic(
                target_host=host,
                target_port=port,
                network_mode=network_mode,
                dns_ok=True,
                resolved_ips=["mock"],
                connect_ok=True,
                http_status=resp.status_code,
                failed_step=None,
                message="连接正常",
            )
        except Exception as exc:
            return EndpointDiagnostic(
                target_host=host,
                target_port=port,
                network_mode=network_mode,
                dns_ok=True,
                resolved_ips=["mock"],
                connect_ok=False,
                http_status=None,
                failed_step="connect",
                message=f"连接失败: {exc}",
            )

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos})
    except OSError as exc:
        return EndpointDiagnostic(
            target_host=host,
            target_port=port,
            network_mode=network_mode,
            dns_ok=False,
            resolved_ips=[],
            connect_ok=False,
            http_status=None,
            failed_step="dns",
            message=f"DNS 解析失败 ({host}): {exc}",
        )

    connected = False
    last_err: str = ""
    for ip in ips[:3]:
        try:
            with socket.create_connection((ip, port), timeout=timeout_seconds):
                connected = True
                break
        except OSError as exc:
            last_err = str(exc)

    if not connected:
        return EndpointDiagnostic(
            target_host=host,
            target_port=port,
            network_mode=network_mode,
            dns_ok=True,
            resolved_ips=ips,
            connect_ok=False,
            http_status=None,
            failed_step="connect",
            message=f"TCP 连接失败 ({host}:{port}): {last_err}",
        )

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            resp = client.get(effective_url)
        return EndpointDiagnostic(
            target_host=host,
            target_port=port,
            network_mode=network_mode,
            dns_ok=True,
            resolved_ips=ips,
            connect_ok=True,
            http_status=resp.status_code,
            failed_step=None,
            message=f"连接正常 (HTTP {resp.status_code})",
        )
    except Exception as exc:
        return EndpointDiagnostic(
            target_host=host,
            target_port=port,
            network_mode=network_mode,
            dns_ok=True,
            resolved_ips=ips,
            connect_ok=False,
            http_status=None,
            failed_step="http",
            message=f"HTTP/TLS 请求失败: {exc}",
        )


def detect_campus_gateway(timeout_seconds: float = 2.0) -> bool:
    """Detect whether gw.buaa.edu.cn resolves to 10.0.0.0/8 and accepts TCP:80."""
    try:
        infos = socket.getaddrinfo(
            GATEWAY_HOST, 80, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        if ip.packed[0] == 10:
            try:
                with socket.create_connection((ip_str, 80), timeout=timeout_seconds):
                    return True
            except OSError:
                continue
    return False
