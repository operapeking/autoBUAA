from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class BuaaError(Exception):
    """Base error for BUAA upstream adapters."""


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
    image_path: Path


class CaptchaRequiredError(BuaaError):
    def __init__(
        self, challenge: CaptchaChallenge, message: str = "登录需要输入图形验证码"
    ) -> None:
        super().__init__(message)
        self.challenge = challenge


class LoginFailedError(BuaaError):
    pass


def parse_int(value: object) -> int | None:
    """Lenient integer parsing for upstream JSON fields (bools and blanks are None)."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
