# WebVPN URL translation and host AES-CFB encryption for BUAA WebVPN and
# Sangfor SSL WebVPN (vpn.buaa.edu.cn).
from __future__ import annotations

import re
from urllib.parse import urlparse

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
