from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class BarkSendResult:
    ok: bool
    status_code: int | None
    response_body: str
    error_message: str | None = None


def build_bark_push_url(server_url: str) -> str:
    base = server_url.strip().rstrip("/")
    if not base:
        base = "https://api.day.app"
    if base.endswith("/push"):
        return base
    return f"{base}/push"


async def send_bark_message(
    *,
    server_url: str,
    device_key: str,
    title: str,
    body: str,
    group: str = "autoBUAA",
    extra: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 10.0,
) -> BarkSendResult:
    url = build_bark_push_url(server_url)
    payload: dict[str, Any] = {
        "device_key": device_key,
        "title": title,
        "body": body,
        "group": group,
    }
    if extra:
        for k in ("sound", "icon", "level", "url"):
            if extra.get(k):
                payload[k] = extra[k]

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport
        ) as client:
            resp = await client.post(url, json=payload)
        resp_text = resp.text[:500]
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
                code = data.get("code")
                if code in (200, "200", None):
                    return BarkSendResult(
                        ok=True, status_code=resp.status_code, response_body=resp_text
                    )
                return BarkSendResult(
                    ok=False,
                    status_code=resp.status_code,
                    response_body=resp_text,
                    error_message=str(
                        data.get("message") or f"Bark returned code {code}"
                    ),
                )
            except Exception:
                return BarkSendResult(
                    ok=True, status_code=resp.status_code, response_body=resp_text
                )
        return BarkSendResult(
            ok=False,
            status_code=resp.status_code,
            response_body=resp_text,
            error_message=f"HTTP {resp.status_code}",
        )
    except Exception as exc:
        return BarkSendResult(
            ok=False,
            status_code=None,
            response_body="",
            error_message=str(exc),
        )
