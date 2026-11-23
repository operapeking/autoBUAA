from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Iterable

import httpx
from app.core.crypto import SecretCipher
from app.core.time import Clock, default_clock, ensure_utc
from app.db.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationRule,
)
from app.notifications.bark import send_bark_message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

RETRY_DELAYS_SECONDS = (5, 30, 120)

SENSITIVE_PAYLOAD_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "auth_token",
    "authtoken",
    "device_key",
    "bark_key",
    "session_id",
    "sessionid",
    "cookies",
    "cookie",
    "secret",
}

REMINDER_EVENTS = {
    "signup_reminder",
    "signin_reminder",
    "signout_reminder",
    "exercise_reminder",
}

EVENT_TITLES: dict[str, str] = {
    "signup_reminder": "博雅选课提醒",
    "signin_reminder": "签到提醒",
    "signout_reminder": "签退提醒",
    "task_succeeded": "任务执行成功",
    "task_failed": "任务执行失败",
    "outcome_unknown": "操作结果待确认",
    "auth_captcha_required": "统一认证需要验证码",
    "test": "测试通知",
    "exercise_reminder": "阳光打卡提醒",
}


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive keys (passwords, tokens, Bark keys, raw bytes) from payloads."""
    cleaned: dict[str, Any] = {}
    for k, v in (payload or {}).items():
        if k.lower() in SENSITIVE_PAYLOAD_KEYS:
            continue
        if isinstance(v, dict):
            cleaned[k] = sanitize_payload(v)
        elif isinstance(v, (bytes, bytearray)):
            continue
        else:
            cleaned[k] = v
    return cleaned


def render_notification_text(
    event_type: str,
    module: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    title = str(
        payload.get("title")
        or EVENT_TITLES.get(event_type, f"autoBUAA 通知 ({event_type})")
    )
    if payload.get("message"):
        body = str(payload["message"])
    else:
        parts = []
        course = payload.get("course_name")
        if course and str(course) not in title:
            parts.append(f"课程: {course}")
        if payload.get("window"):
            parts.append(f"时间: {payload['window']}")
        if payload.get("summary"):
            parts.append(str(payload["summary"]))
        if payload.get("reason"):
            parts.append(f"原因: {payload['reason']}")
        body = "\n".join(parts)
    return title[:250], body


def expire_events(events: Iterable[NotificationEvent]) -> None:
    for event in events:
        event.status = "expired"
        event.next_retry_at = None


def match_rule(
    session: Session, event_type: str, module: str
) -> NotificationRule | None:
    rule = (
        session.query(NotificationRule)
        .filter_by(event_type=event_type, module=module)
        .first()
    )
    if rule is None:
        rule = (
            session.query(NotificationRule)
            .filter_by(event_type=event_type, module="*")
            .first()
        )
    return rule


def publish_event(
    session: Session,
    *,
    event_type: str,
    event_key: str,
    module: str,
    payload: dict[str, Any],
    expires_at: datetime | None = None,
    send_at: datetime | None = None,
    execution_id: int | None = None,
    schedule_id: int | None = None,
    clock: Clock | None = None,
    bypass_rule_check: bool = False,
) -> NotificationEvent | None:
    """Persist a deduplicated notification event in the current DB transaction."""
    clk = clock or default_clock
    now_utc = clk.now_utc()

    existing = session.query(NotificationEvent).filter_by(event_key=event_key).first()
    if existing is not None:
        return None

    if not bypass_rule_check:
        rule = match_rule(session, event_type, module)
        if rule is not None and not rule.enabled:
            return None

    safe_payload = sanitize_payload(payload)
    title, body = render_notification_text(event_type, module, safe_payload)
    scheduled_send = max(now_utc, ensure_utc(send_at)) if send_at else now_utc
    exp = (
        ensure_utc(expires_at) if expires_at else (scheduled_send + timedelta(hours=24))
    )
    if exp <= now_utc or exp <= scheduled_send:
        return None

    event = NotificationEvent(
        event_key=event_key,
        event_type=event_type,
        module=module,
        execution_id=execution_id,
        schedule_id=schedule_id,
        title=title,
        body=body,
        payload_json=safe_payload,
        status="pending",
        attempts=0,
        next_retry_at=scheduled_send,
        expires_at=exp,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        return None
    return event


class NotificationDispatcher:
    """Processes pending notification events and sends via configured Bark channels."""

    def __init__(
        self,
        session_factory: Any,
        cipher: SecretCipher,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.clock = clock or default_clock
        self.transport = transport
        self._delivery_lock = asyncio.Lock()

    async def dispatch_due_events(self, limit: int = 20) -> int:
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            due_ids = [
                row.id
                for row in session.query(NotificationEvent)
                .filter(
                    NotificationEvent.status == "pending",
                    (NotificationEvent.next_retry_at.is_(None))
                    | (NotificationEvent.next_retry_at <= now_utc),
                )
                .order_by(NotificationEvent.id.asc())
                .limit(limit)
                .all()
            ]
        sent_or_updated = 0
        for event_id in due_ids:
            await self.deliver_event(event_id)
            sent_or_updated += 1
        return sent_or_updated

    async def deliver_event(self, event_id: int) -> NotificationEvent | None:
        async with self._delivery_lock:
            return await self._deliver_event(event_id)

    async def _deliver_event(self, event_id: int) -> NotificationEvent | None:
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            event = session.get(NotificationEvent, event_id)
            if event is None or event.status in {"sent", "expired"}:
                return event

            if event.next_retry_at and now_utc < event.next_retry_at:
                return event

            if event.expires_at and now_utc >= event.expires_at:
                event.status = "expired"
                event.next_retry_at = None
                session.commit()
                return event

            rule = match_rule(session, event.event_type, event.module)
            if rule and not rule.enabled:
                event.status = "expired"
                event.next_retry_at = None
                session.commit()
                return event
            channel = None
            if rule and rule.channel_id:
                channel = session.get(NotificationChannel, rule.channel_id)
            if channel is None:
                channel = (
                    session.query(NotificationChannel)
                    .filter_by(enabled=True)
                    .order_by(NotificationChannel.id.asc())
                    .first()
                )

            if channel is None or not channel.enabled:
                self._record_failure(
                    session,
                    event,
                    channel_id=channel.id if channel else None,
                    status_code=None,
                    response_body="",
                    error_message="没有已启用的通知渠道",
                    now_utc=now_utc,
                )
                session.commit()
                return event

            device_key = self.cipher.decrypt(channel.encrypted_device_key)
            if not device_key:
                self._record_failure(
                    session,
                    event,
                    channel_id=channel.id,
                    status_code=None,
                    response_body="",
                    error_message="Bark 设备密钥未配置",
                    now_utc=now_utc,
                )
                session.commit()
                return event

            server_url = channel.server_url
            extra = dict(channel.extra_json or {})
            group = str(extra.get("group") or "autoBUAA")
            title = event.title
            body = event.body
            channel_id = channel.id

        result = await send_bark_message(
            server_url=server_url,
            device_key=device_key,
            title=title,
            body=body,
            group=group,
            extra=extra,
            transport=self.transport,
        )

        with self.session_factory() as session:
            event = session.get(NotificationEvent, event_id)
            if event is None:
                return None
            attempt_no = event.attempts + 1
            event.attempts = attempt_no
            delivery = NotificationDelivery(
                event_id=event.id,
                channel_id=channel_id,
                attempt_no=attempt_no,
                status="succeeded" if result.ok else "failed",
                response_status=result.status_code,
                response_body=result.response_body,
                error_message=result.error_message,
                attempted_at=now_utc,
            )
            session.add(delivery)
            if result.ok:
                event.status = "sent"
                event.next_retry_at = None
            else:
                self._schedule_retry_or_fail(event, attempt_no, now_utc)
            session.commit()
            session.refresh(event)
            return event

    def _record_failure(
        self,
        session: Session,
        event: NotificationEvent,
        *,
        channel_id: int | None,
        status_code: int | None,
        response_body: str,
        error_message: str,
        now_utc: datetime,
    ) -> None:
        attempt_no = event.attempts + 1
        event.attempts = attempt_no
        session.add(
            NotificationDelivery(
                event_id=event.id,
                channel_id=channel_id,
                attempt_no=attempt_no,
                status="failed",
                response_status=status_code,
                response_body=response_body,
                error_message=error_message,
                attempted_at=now_utc,
            )
        )
        self._schedule_retry_or_fail(event, attempt_no, now_utc)

    @staticmethod
    def _schedule_retry_or_fail(
        event: NotificationEvent, attempt_no: int, now_utc: datetime
    ) -> None:
        if attempt_no <= len(RETRY_DELAYS_SECONDS):
            delay = RETRY_DELAYS_SECONDS[attempt_no - 1]
            candidate = now_utc + timedelta(seconds=delay)
            if event.expires_at and candidate >= event.expires_at:
                event.status = "expired"
                event.next_retry_at = None
            else:
                event.status = "pending"
                event.next_retry_at = candidate
        else:
            event.status = "failed"
            event.next_retry_at = None
