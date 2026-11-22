from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import httpx
from app.container import AppContainer
from app.core.config import Settings
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock
from app.db.models import NotificationChannel, NotificationDelivery, NotificationEvent
from app.notifications.service import publish_event


async def test_notification_dedup_sanitization_retry_and_expiry(tmp_path: Path) -> None:
    calls: list[dict] = []
    fail_mode = {"fail": True}

    def bark_handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content.decode()))
        if fail_mode["fail"]:
            return httpx.Response(500, json={"code": 500, "message": "server error"})
        return httpx.Response(200, json={"code": 200, "message": "success"})

    start_sh = datetime(2026, 11, 15, 12, 0, 0, tzinfo=SHANGHAI_TZ)
    clock = Clock(start_sh.astimezone(UTC_TZ))
    settings = Settings(
        data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
    )
    container = AppContainer(
        settings=settings,
        clock=clock,
        bark_transport=httpx.MockTransport(bark_handler),
        run_migrations=True,
    )

    with container.session_factory() as session:
        ch = session.query(NotificationChannel).first()
        assert ch is not None
        ch.enabled = True
        ch.encrypted_device_key = container.cipher.encrypt("secret-bark-device-key-999")
        session.commit()

    # Publish two events with the same event_key -> deduped to 1; also verify sensitive payload stripped
    with container.session_factory() as session:
        ev1 = publish_event(
            session,
            event_type="task_succeeded",
            event_key="dedup:key:1",
            module="classroom",
            payload={
                "title": "签到提醒",
                "message": "课程即将开始",
                "password": "my-secret-password",
                "token": "jwt-token-xyz",
                "device_key": "secret-bark-device-key-999",
                "photo_bytes": b"raw-bytes",
            },
            expires_at=clock.now_utc() + timedelta(minutes=2),
            clock=clock,
        )
        ev2 = publish_event(
            session,
            event_type="task_succeeded",
            event_key="dedup:key:1",
            module="classroom",
            payload={"title": "Duplicate", "message": "Should be ignored"},
            clock=clock,
        )
        session.commit()
        assert ev1 is not None
        assert ev2 is None
        assert "password" not in ev1.payload_json
        assert "token" not in ev1.payload_json
        assert "device_key" not in ev1.payload_json
        assert "photo_bytes" not in ev1.payload_json
        ev_id = ev1.id

    # Attempt 1 fails -> schedules +5s
    await container.notification_dispatcher.dispatch_due_events()
    with container.session_factory() as session:
        ev = session.get(NotificationEvent, ev_id)
        assert ev is not None
        assert ev.status == "pending"
        assert ev.attempts == 1
        assert ev.next_retry_at == clock.now_utc() + timedelta(seconds=5)

    # Attempt 2 (+5s) succeeds when Bark recovers
    clock.advance(5)
    fail_mode["fail"] = False
    await container.notification_dispatcher.dispatch_due_events()
    with container.session_factory() as session:
        ev = session.get(NotificationEvent, ev_id)
        deliveries = session.query(NotificationDelivery).filter_by(event_id=ev_id).all()
        assert ev is not None and ev.status == "sent"
        assert ev.attempts == 2
        assert len(deliveries) == 2
        assert calls[-1]["device_key"] == "secret-bark-device-key-999"

    # Expired event stops sending
    fail_mode["fail"] = True
    with container.session_factory() as session:
        ev_exp = publish_event(
            session,
            event_type="signin_reminder",
            event_key="exp:key:1",
            module="classroom",
            payload={"title": "Expiring", "message": "soon"},
            expires_at=clock.now_utc() + timedelta(seconds=3),
            clock=clock,
        )
        session.commit()
        assert ev_exp is not None
        exp_id = ev_exp.id

    # Attempt 1 fails, +5s exceeds expires_at (+3s) -> immediately marked expired!
    await container.notification_dispatcher.dispatch_due_events()
    with container.session_factory() as session:
        ev_exp = session.get(NotificationEvent, exp_id)
        assert ev_exp is not None and ev_exp.status == "expired"
