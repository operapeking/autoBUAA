from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class BuaaError(Exception):
    """Base error for BUAA upstream adapters."""


class NetworkDiagnosticsError(BuaaError):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


class SessionExpiredError(BuaaError):
    pass


class OutcomeUnknownError(BuaaError):
    """Write request timed out or returned ambiguous outcome; must query to confirm."""


class BusinessTerminalError(BuaaError):
    """Non-retryable business-layer error (e.g. course full, selection limit)."""


@dataclass
class CaptchaChallenge:
    captcha_id: str
    captcha_type: str
    execution: str
    image_path: Path | None = None
    image_bytes: bytes = b""
    mime_type: str = "image/jpeg"


class CaptchaRequiredError(BuaaError):
    def __init__(
        self, challenge: CaptchaChallenge, message: str = "登录需要输入图形验证码"
    ) -> None:
        super().__init__(message)
        self.challenge = challenge


class LoginFailedError(BuaaError):
    pass
