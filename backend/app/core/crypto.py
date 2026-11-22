from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretCipher:
    """Encrypts sensitive database values using a persistent Fernet key file."""

    def __init__(self, key_file: Path) -> None:
        self.key_file = key_file
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_file.exists():
            raw = self.key_file.read_bytes().strip()
            if raw:
                return raw
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self.key_file.write_bytes(key)
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass
        return key

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None or plaintext == "":
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str | None) -> str | None:
        if ciphertext is None or ciphertext == "":
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt secret payload with current key"
            ) from exc


def hash_password(password: str) -> str:
    if len(password) < 6:
        raise ValueError("管理员密码长度不能少于 6 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_secret(value: str | None, keep_tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep_tail:
        return "*" * len(value)
    return "*" * max(6, len(value) - keep_tail) + value[-keep_tail:]
