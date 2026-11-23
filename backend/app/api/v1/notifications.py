from __future__ import annotations

import secrets
from typing import Any

from app.api.deps import require_admin
from app.container import AppContainer
from app.core.crypto import SecretCipher, mask_secret
from app.core.time import format_shanghai_iso
from app.db.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationRule,
)
from app.modules.automation import request_module_refresh
from app.notifications.service import publish_event
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ChannelUpdatePayload(BaseModel):
    name: str = Field(default="Bark 默认通道", min_length=1, max_length=128)
    enabled: bool = True
    server_url: str = Field(default="https://api.day.app", min_length=1, max_length=512)
    device_key: str | None = Field(default=None, max_length=256)
    group: str = Field(default="autoBUAA", max_length=64)
    sound: str | None = Field(default=None, max_length=64)


class RuleUpdatePayload(BaseModel):
    enabled: bool
    advance_minutes: int = Field(default=15, ge=0, le=180)
    channel_id: int | None = None


class TestPushPayload(BaseModel):
    title: str = Field(default="autoBUAA 测试推送", max_length=128)
    body: str = Field(default="收到这条消息说明 Bark 通知配置正常。", max_length=500)


def _serialize_channel(ch: NotificationChannel, cipher: SecretCipher) -> dict[str, Any]:
    raw_key = cipher.decrypt(ch.encrypted_device_key)
    return {
        "id": ch.id,
        "channel_type": ch.channel_type,
        "name": ch.name,
        "enabled": ch.enabled,
        "server_url": ch.server_url,
        "has_device_key": bool(raw_key),
        "device_key_masked": mask_secret(raw_key),
        "extra": ch.extra_json or {},
        "updated_at": format_shanghai_iso(ch.updated_at),
    }


@router.get("/channels")
def list_channels(
    container: AppContainer = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        rows = (
            session.query(NotificationChannel)
            .order_by(NotificationChannel.id.asc())
            .all()
        )
        return [_serialize_channel(r, container.cipher) for r in rows]


@router.put("/channels/{channel_id}")
def update_channel(
    channel_id: int,
    payload: ChannelUpdatePayload,
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        ch = session.get(NotificationChannel, channel_id)
        if ch is None:
            raise HTTPException(status_code=404, detail="通知渠道不存在")
        ch.name = payload.name
        ch.enabled = payload.enabled
        ch.server_url = payload.server_url.strip().rstrip("/")
        if payload.device_key is not None and payload.device_key.strip() != "":
            ch.encrypted_device_key = container.cipher.encrypt(
                payload.device_key.strip()
            )
        extra = dict(ch.extra_json or {})
        extra["group"] = payload.group
        if payload.sound is not None:
            extra["sound"] = payload.sound
        ch.extra_json = extra
        session.commit()
        session.refresh(ch)
        return _serialize_channel(ch, container.cipher)


@router.post("/channels/{channel_id}/test")
async def test_channel_push(
    channel_id: int,
    payload: TestPushPayload | None = None,
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    body_data = payload or TestPushPayload()
    with container.session_factory() as session:
        ch = session.get(NotificationChannel, channel_id)
        if ch is None:
            raise HTTPException(status_code=404, detail="通知渠道不存在")
        ev = publish_event(
            session,
            event_type="test",
            event_key=f"test:{channel_id}:{secrets.token_hex(8)}",
            module="system",
            payload={"title": body_data.title, "message": body_data.body},
            clock=container.clock,
            bypass_rule_check=True,
        )
        session.commit()
        event_id = ev.id if ev else None

    if event_id is None:
        raise HTTPException(status_code=400, detail="创建测试通知事件失败")
    delivered = await container.notification_dispatcher.deliver_event(event_id)
    return {
        "ok": bool(delivered and delivered.status == "sent"),
        "event_id": event_id,
        "status": delivered.status if delivered else "unknown",
    }


RULE_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("exercise_reminder", "ygdk"): "阳光打卡进度提醒",
    ("signup_reminder", "boya"): "博雅选课提醒",
    ("signin_reminder", "classroom"): "课堂签到开始前提醒",
    ("signin_reminder", "boya"): "博雅签到提醒",
    ("signout_reminder", "boya"): "博雅签退提醒",
    ("task_succeeded", "*"): "自动任务执行成功时通知",
    ("task_failed", "*"): "自动任务执行失败时通知",
    ("outcome_unknown", "*"): "操作超时结果待确认时通知",
    ("auth_captcha_required", "*"): "统一认证需要人工验证码时通知",
}


def _serialize_rule(rule: NotificationRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "event_type": rule.event_type,
        "module": rule.module,
        "enabled": rule.enabled,
        "advance_minutes": rule.advance_minutes,
        "channel_id": rule.channel_id,
        "description": RULE_DESCRIPTIONS.get((rule.event_type, rule.module), ""),
    }


@router.get("/rules")
def list_rules(
    container: AppContainer = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        rows = session.query(NotificationRule).order_by(NotificationRule.id.asc()).all()
        return [_serialize_rule(r) for r in rows]


@router.put("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    payload: RuleUpdatePayload,
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        rule = session.get(NotificationRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="通知规则不存在")
        rule.enabled = payload.enabled
        rule.advance_minutes = payload.advance_minutes
        if payload.channel_id is not None:
            rule.channel_id = payload.channel_id
        # Course reminders are scheduled during refreshes; rebuild them with the new timing.
        if rule.module in {"classroom", "boya"}:
            request_module_refresh(session, rule.module, container.clock)
        session.commit()
        return _serialize_rule(rule)


@router.get("/events")
def list_events(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    container: AppContainer = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        q = session.query(NotificationEvent)
        if status_filter:
            q = q.filter(NotificationEvent.status == status_filter)
        rows = q.order_by(NotificationEvent.id.desc()).limit(limit).all()
        result: list[dict[str, Any]] = []
        for ev in rows:
            deliveries = (
                session.query(NotificationDelivery)
                .filter_by(event_id=ev.id)
                .order_by(NotificationDelivery.attempt_no.asc())
                .all()
            )
            result.append(
                {
                    "id": ev.id,
                    "event_key": ev.event_key,
                    "event_type": ev.event_type,
                    "module": ev.module,
                    "execution_id": ev.execution_id,
                    "schedule_id": ev.schedule_id,
                    "title": ev.title,
                    "body": ev.body,
                    "status": ev.status,
                    "attempts": ev.attempts,
                    "next_retry_at": format_shanghai_iso(ev.next_retry_at),
                    "expires_at": format_shanghai_iso(ev.expires_at),
                    "created_at": format_shanghai_iso(ev.created_at),
                    "deliveries": [
                        {
                            "id": d.id,
                            "attempt_no": d.attempt_no,
                            "status": d.status,
                            "response_status": d.response_status,
                            "error_message": d.error_message,
                            "attempted_at": format_shanghai_iso(d.attempted_at),
                        }
                        for d in deliveries
                    ],
                }
            )
        return result


@router.post("/events/{event_id}/retry")
async def retry_notification_event(
    event_id: int,
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        ev = session.get(NotificationEvent, event_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="通知事件不存在")
        if ev.status == "sent":
            return {
                "ok": True,
                "event_id": ev.id,
                "status": "sent",
                "message": "该消息已发送成功",
            }
        if ev.expires_at and container.clock.now_utc() >= ev.expires_at:
            ev.status = "expired"
            session.commit()
            raise HTTPException(
                status_code=400, detail="通知事件已超过有效期，停止发送"
            )
        ev.status = "pending"
        ev.next_retry_at = container.clock.now_utc()
        session.commit()

    delivered = await container.notification_dispatcher.deliver_event(event_id)
    return {
        "ok": bool(delivered and delivered.status == "sent"),
        "event_id": event_id,
        "event_key": delivered.event_key if delivered else None,
        "status": delivered.status if delivered else "unknown",
    }
