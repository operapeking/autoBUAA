from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from app.buaa.classroom import ClassroomClient, ClassroomCourse
from app.buaa.ygdk import YgdkClient, YgdkOverviewData
from app.container import AppContainer
from app.core.config import Settings
from app.core.time import SHANGHAI_TZ, Clock
from app.db.models import (
    CampusAccount,
    ModuleConfig,
    NotificationChannel,
    NotificationEvent,
    TaskExecution,
    TaskSchedule,
)
from app.framework.tasks import TaskResult, create_schedule
from app.main import create_app
from app.modules.automation import reconcile_automation
from app.modules.services import sync_classroom_today, sync_ygdk_progress
from app.notifications.service import publish_event
from fastapi.testclient import TestClient
from pydantic import BaseModel


@pytest.fixture
def container(tmp_path: Path):
    app = AppContainer(
        settings=Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "dist"),
        clock=Clock(datetime(2026, 11, 16, 18, tzinfo=SHANGHAI_TZ)),
        run_migrations=False,
    )
    yield app
    app.engine.dispose()


@pytest.mark.parametrize(
    "term,term_target,week,week_target,hour,expected",
    [
        (0, 32, 0, 4, 18, True),
        (31, 32, 3, 4, 18, True),
        (32, 32, 0, 4, 18, False),
        (33, 32, 1, 4, 18, False),
        (0, 32, 4, 4, 18, False),
        (0, 32, 5, 4, 18, False),
        (0, 32, 0, 4, 17, False),
        (None, 32, 0, 4, 18, False),
        (0, None, 0, 4, 18, False),
        (0, 32, None, 4, 18, False),
        (0, 32, 0, None, 18, False),
        (0, 0, 0, 4, 18, False),
        (-1, 32, 0, 4, 18, False),
        (0, 32, -1, 4, 18, False),
    ],
)
async def test_sunshine_reminder_conditions(
    container, monkeypatch, term, term_target, week, week_target, hour, expected
):
    container.clock.set(datetime(2026, 11, 16, hour, tzinfo=SHANGHAI_TZ))

    async def overview(_self):
        return YgdkOverviewData(
            1, "阳光体育", 20261, "秋学期", term, term_target, week, week_target
        )

    monkeypatch.setattr(YgdkClient, "get_overview", overview)
    with container.session_factory() as session:
        session.get(ModuleConfig, "ygdk").enabled = True
        session.commit()
    await sync_ygdk_progress(
        container.session_factory, container.session_mgr, container.clock
    )
    await sync_ygdk_progress(
        container.session_factory, container.session_mgr, container.clock
    )
    with container.session_factory() as session:
        events = (
            session.query(NotificationEvent)
            .filter_by(event_type="exercise_reminder")
            .all()
        )
        assert len(events) == int(expected)
        if expected:
            assert events[0].expires_at.astimezone(SHANGHAI_TZ).day == 17


async def test_sunshine_completion_expires_queued_reminder(container, monkeypatch):
    state = {"week": 1}

    async def overview(_self):
        return YgdkOverviewData(
            1, "阳光体育", 20261, "秋学期", 10, 32, state["week"], 4
        )

    monkeypatch.setattr(YgdkClient, "get_overview", overview)
    with container.session_factory() as session:
        session.get(ModuleConfig, "ygdk").enabled = True
        session.commit()
    await sync_ygdk_progress(
        container.session_factory, container.session_mgr, container.clock
    )
    state["week"] = 4
    await sync_ygdk_progress(
        container.session_factory, container.session_mgr, container.clock
    )
    with container.session_factory() as session:
        assert session.query(NotificationEvent).one().status == "expired"


def test_settings_manage_schedules_and_validate_input(container):
    client = TestClient(create_app(container, start_scheduler=False))
    assert client.get("/api/v1/network").status_code == 401
    assert (
        client.post("/api/v1/auth/setup", json={"password": "admin-secret"}).status_code
        == 200
    )
    state = client.get("/api/v1/network").json()
    for path in (
        "/classroom/courses",
        "/boya/courses",
        "/ygdk/overview",
        "/td/overview",
        "/tasks/schedules",
        "/tasks/executions",
        "/notifications/channels",
        "/notifications/rules",
        "/notifications/events",
    ):
        assert client.get("/api/v1" + path).status_code == 200
    assert {mod["module"] for mod in state["modules"]} == {"classroom", "boya", "ygdk", "td"}
    enabled = client.put(
        "/api/v1/network/modules/classroom",
        json={"enabled": True, "config_json": {"auto_sign": False}},
    )
    assert enabled.status_code == 200
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
        )
        assert (
            schedule.status == "enabled"
            and schedule.next_run_at == container.clock.now_utc()
        )
        version = schedule.version
    # Repeated requests preserve the existing plan.
    client.put(
        "/api/v1/network/modules/classroom",
        json={"enabled": True, "config_json": {"auto_sign": False}},
    )
    with container.session_factory() as session:
        assert (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
            .version
            == version
        )
    for config in (
        {"poll_interval_seconds": 1},
        {"signin_offset_minutes": -20},
        {"auto_sign": "invalid"},
    ):
        assert (
            client.put(
                "/api/v1/network/modules/classroom",
                json={"enabled": False, "config_json": config},
            ).status_code
            == 422
        )
    assert (
        client.put(
            "/api/v1/network/modules/ygdk",
            json={"enabled": True, "config_json": {"reminder_time": "24:01"}},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/v1/network/modules/classroom",
            json={"enabled": False, "config_json": {}},
        ).status_code
        == 200
    )
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
        )
        assert schedule.status == "paused" and schedule.next_run_at is None


async def test_course_refresh_is_idempotent_and_separates_dates(container, monkeypatch):
    container.clock.set(datetime(2026, 11, 16, 8, tzinfo=SHANGHAI_TZ))
    signed = {"value": False}

    async def courses(_self, _date):
        return [
            ClassroomCourse(
                "same-id", "高等数学", "10:00", "11:40", int(signed["value"]), {}
            )
        ]

    monkeypatch.setattr(ClassroomClient, "get_today_courses", courses)
    with container.session_factory() as session:
        session.get(ModuleConfig, "classroom").enabled = True
        session.commit()
    await sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        reminder = session.query(NotificationEvent).one()
        assert (
            reminder.next_retry_at.astimezone(SHANGHAI_TZ).strftime("%H:%M") == "09:45"
        )
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261116:same-id")
            .one()
        )
        schedule.status = "completed"
        schedule.next_run_at = None
        version = schedule.version
        session.commit()
    await sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261116:same-id")
            .one()
        )
        assert schedule.status == "completed" and schedule.version == version
        assert session.query(NotificationEvent).count() == 1
    signed["value"] = True
    await sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        assert session.query(NotificationEvent).one().status == "expired"
    signed["value"] = False
    container.clock.advance(86400)
    await sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        assert (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261117:same-id")
            .one()
            .status
            == "enabled"
        )


async def test_reminder_only_classroom_does_not_schedule_signin(container, monkeypatch):
    async def courses(_self, _date):
        return [ClassroomCourse("1", "课程", "20:00", "21:40", 0, {})]

    monkeypatch.setattr(ClassroomClient, "get_today_courses", courses)
    with container.session_factory() as session:
        mod = session.get(ModuleConfig, "classroom")
        mod.enabled = True
        mod.config_json = {"auto_sign": False}
        session.commit()
    await sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        assert (
            session.query(TaskSchedule)
            .filter_by(task_type="classroom.auto_signin")
            .count()
            == 0
        )
        assert (
            session.query(NotificationEvent)
            .filter_by(event_type="signin_reminder", status="pending")
            .count()
            == 1
        )


async def test_waiting_window_uses_one_execution(container):
    class Empty(BaseModel):
        pass

    async def wait(ctx, params):
        return TaskResult(
            status="waiting",
            summary="waiting",
            resume_at=ctx.clock.now_utc() + timedelta(minutes=5),
        )

    container.registry.register_task(
        "test.wait", Empty, wait, module="system", display_name="wait"
    )
    with container.session_factory() as session:
        schedule = create_schedule(
            session,
            container.registry,
            name="window",
            task_type="test.wait",
            parameters={},
            trigger_type="window",
            trigger_config={
                "start_at": "2026-11-16 18:00",
                "end_at": "2026-11-16 19:00",
                "poll_interval_seconds": 30,
            },
            clock=container.clock,
        )
        schedule_id = schedule.id
        session.commit()
    await container.scheduler.tick(wait_for_completion=True)
    container.clock.advance(30)
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        assert (
            session.query(TaskExecution).filter_by(schedule_id=schedule_id).count() == 1
        )


async def test_disable_cancels_waiting_work(container):
    with container.session_factory() as session:
        mod = session.get(ModuleConfig, "classroom")
        mod.enabled = True
        reconcile_automation(
            session, container.registry, container.clock, changed="classroom"
        )
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
        )
        session.add(
            TaskExecution(
                schedule_id=schedule.id,
                task_type=schedule.task_type,
                module="classroom",
                scheduled_for=container.clock.now_utc(),
                status="waiting",
                resume_at=container.clock.now_utc(),
            )
        )
        session.commit()
        mod.enabled = False
        reconcile_automation(
            session, container.registry, container.clock, changed="classroom"
        )
        session.commit()
        execution = session.query(TaskExecution).one()
        assert execution.status == "cancelled" and execution.resume_at is None
    assert await container.scheduler.tick(wait_for_completion=True) == 0


async def test_notification_concurrent_delivery_sends_once(container):
    calls = []

    async def bark(request):
        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"code": 200})

    container.notification_dispatcher.transport = httpx.MockTransport(bark)
    with container.session_factory() as session:
        channel = session.query(NotificationChannel).first()
        channel.enabled = True
        channel.encrypted_device_key = container.cipher.encrypt("private-device-key")
        event = publish_event(
            session,
            event_type="task_succeeded",
            event_key="concurrent",
            module="classroom",
            payload={"title": "success"},
            clock=container.clock,
        )
        event_id = event.id
        session.commit()
    await asyncio.gather(
        *(container.notification_dispatcher.deliver_event(event_id) for _ in range(3))
    )
    assert len(calls) == 1


def test_debug_file_logging_redacts_exception(container):
    logger = logging.getLogger("app.test")
    logger.debug("debug-file-marker password=private-password")
    try:
        raise ValueError("token=private-token")
    except ValueError:
        logger.exception("Bearer private-bearer")
    contents = (container.settings.data_dir / "logs" / "autobuaa.log").read_text(
        encoding="utf-8"
    )
    assert "debug-file-marker" in contents and "DEBUG" in contents
    assert (
        "private-password" not in contents
        and "private-token" not in contents
        and "private-bearer" not in contents
    )


async def test_boya_regular_reminders_and_confirmed_window_completion(
    container, monkeypatch
):
    from app.buaa.boya import BoyaClient, BoyaStatsData, parse_course
    from app.modules.services import sync_boya_all

    course = parse_course(
        {
            "id": 101,
            "courseName": "艺术讲座",
            "courseStartDate": "2026-11-16 20:00:00",
            "courseEndDate": "2026-11-16 22:00:00",
        }
    )
    course.selected = True
    course.checkin = course.pass_status = 0

    async def config(_self):
        return {
            "semester": [
                {
                    "semesterStartDate": "2026-09-01 00:00:00",
                    "semesterEndDate": "2027-01-31 23:59:59",
                }
            ]
        }

    async def courses(_self):
        return [course]

    async def chosen(_self, _start, _end):
        return [course]

    async def stats(_self):
        return BoyaStatsData(0, [], {})

    monkeypatch.setattr(BoyaClient, "get_all_config", config)
    monkeypatch.setattr(BoyaClient, "query_courses", courses)
    monkeypatch.setattr(BoyaClient, "query_chosen_courses", chosen)
    monkeypatch.setattr(BoyaClient, "query_statistics", stats)
    with container.session_factory() as session:
        session.get(ModuleConfig, "boya").enabled = True
        session.commit()
    await sync_boya_all(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        events = (
            session.query(NotificationEvent)
            .filter_by(module="boya", status="pending")
            .all()
        )
        assert {event.event_type for event in events} == {
            "signin_reminder",
            "signout_reminder",
        }
        assert {
            event.next_retry_at.astimezone(SHANGHAI_TZ).strftime("%H:%M")
            for event in events
        } == {"19:45", "21:45"}
        assert (
            session.query(TaskSchedule)
            .filter_by(module="boya", trigger_type="window")
            .count()
            == 0
        )

    course.has_autonomous_sign = True
    course.sign_config = {
        "signStartDate": "2026-11-16 20:00:00",
        "signEndDate": "2026-11-16 20:30:00",
        "signOutStartDate": "2026-11-16 21:30:00",
        "signOutEndDate": "2026-11-16 22:00:00",
        "signPointList": [{"lat": 40, "lng": 116}],
    }
    await sync_boya_all(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        schedules = (
            session.query(TaskSchedule)
            .filter_by(module="boya", trigger_type="window")
            .all()
        )
        assert len(schedules) == 2
        for schedule in schedules:
            schedule.status = "completed"
            schedule.last_result_status = "succeeded"
            schedule.next_run_at = None
        session.commit()
    await sync_boya_all(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )
    with container.session_factory() as session:
        assert (
            session.query(NotificationEvent)
            .filter_by(module="boya", status="pending")
            .count()
            == 0
        )
        assert (
            session.query(TaskSchedule)
            .filter_by(module="boya", status="completed")
            .count()
            == 2
        )
    client = TestClient(create_app(container, start_scheduler=False))
    client.post("/api/v1/auth/setup", json={"password": "admin-secret"})
    response = client.get("/api/v1/boya/courses")
    assert response.status_code == 200
    assert response.json()["courses"][0]["signin_done"] is True
    assert response.json()["courses"][0]["signout_done"] is True
    assert response.json()["courses"][0]["course_start"].endswith("+08:00")


async def test_overdue_daily_plan_runs_once_on_recovery(container, monkeypatch):
    calls = []

    async def overview(_self):
        calls.append(True)
        return YgdkOverviewData(1, "阳光体育", 20261, "秋学期", 1, 32, 0, 4)

    monkeypatch.setattr(YgdkClient, "get_overview", overview)
    with container.session_factory() as session:
        account = session.query(CampusAccount).first()
        account.student_id = "23000001"
        account.encrypted_password = container.cipher.encrypt("test-password")
        session.get(ModuleConfig, "ygdk").enabled = True
        reconcile_automation(
            session, container.registry, container.clock, changed="ygdk"
        )
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="ygdk:sync_overview")
            .one()
        )
        schedule.next_run_at = container.clock.now_utc() - timedelta(days=1)
        session.commit()
    container.scheduler.recover_on_startup()
    await container.scheduler.tick(wait_for_completion=True)
    await container.scheduler.tick(wait_for_completion=True)
    assert len(calls) == 1
    with container.session_factory() as session:
        assert (
            session.query(NotificationEvent)
            .filter_by(event_type="exercise_reminder")
            .count()
            == 1
        )
        schedule = (
            session.query(TaskSchedule)
            .filter_by(business_key="ygdk:sync_overview")
            .one()
        )
        assert schedule.next_run_at.astimezone(SHANGHAI_TZ) == datetime(
            2026, 11, 17, 18, tzinfo=SHANGHAI_TZ
        )


async def test_scheduler_background_lifecycle(container):
    container.scheduler.start()
    assert container.scheduler.running
    await asyncio.sleep(0)
    assert container.scheduler.last_heartbeat_at == container.clock.now_utc()
    await container.scheduler.stop()
    assert not container.scheduler.running


def test_notification_timing_change_preserves_waiting_signin(container):
    from app.db.models import NotificationRule

    with container.session_factory() as session:
        session.get(ModuleConfig, "classroom").enabled = True
        reconcile_automation(
            session, container.registry, container.clock, changed="classroom"
        )
        schedule = create_schedule(
            session,
            container.registry,
            name="课程签到",
            task_type="classroom.auto_signin",
            parameters={"course_sched_id": "1", "course_date": "20261116"},
            trigger_type="window",
            trigger_config={
                "start_at": "2026-11-16 18:00:00",
                "end_at": "2026-11-16 19:00:00",
            },
            business_key="classroom:signin:20261116:1",
            clock=container.clock,
        )
        execution = TaskExecution(
            schedule_id=schedule.id,
            task_type=schedule.task_type,
            module="classroom",
            scheduled_for=container.clock.now_utc(),
            status="waiting",
            resume_at=container.clock.now_utc() + timedelta(seconds=30),
        )
        session.add(execution)
        rule = (
            session.query(NotificationRule)
            .filter_by(event_type="signin_reminder", module="classroom")
            .one()
        )
        rule_id = rule.id
        session.commit()
        execution_id = execution.id
    client = TestClient(create_app(container, start_scheduler=False))
    client.post("/api/v1/auth/setup", json={"password": "admin-secret"})
    assert (
        client.put(
            f"/api/v1/notifications/rules/{rule_id}",
            json={"enabled": True, "advance_minutes": 10},
        ).status_code
        == 200
    )
    with container.session_factory() as session:
        assert session.get(TaskExecution, execution_id).status == "waiting"
        assert (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
            .next_run_at
            == container.clock.now_utc()
        )


@pytest.fixture
def boya_reservation(container, monkeypatch):
    from unittest.mock import AsyncMock

    from app.buaa.boya import BoyaClient, parse_course
    from app.db.models import BoyaCourseCache

    raw = {
        "id": 901,
        "courseName": "艺术讲座",
        "courseCampusList": ["沙河校区"],
        "courseSelectStartDate": "2026-11-16 18:01:00",
        "courseSelectEndDate": "2026-11-16 19:00:00",
        "courseCurrentCount": 10,
        "courseMaxCount": 10,
    }
    state = {"chosen": False}

    async def chosen(*args):
        return [parse_course(raw)] if state["chosen"] else []

    async def detail(*args):
        return parse_course(raw)

    select = AsyncMock(return_value={"status": 0})
    monkeypatch.setattr(BoyaClient, "get_all_config", AsyncMock(return_value={}))
    monkeypatch.setattr(BoyaClient, "query_chosen_courses", chosen)
    monkeypatch.setattr(BoyaClient, "query_course_detail", detail)
    monkeypatch.setattr(BoyaClient, "select_course", select)
    with container.session_factory() as session:
        account = session.query(CampusAccount).first()
        account.student_id = "23000001"
        account.encrypted_password = container.cipher.encrypt("test-password")
        account.campus = "沙河"
        session.get(ModuleConfig, "boya").enabled = True
        session.add(
            BoyaCourseCache(
                course_id=901,
                name="艺术讲座",
                campus="沙河校区",
                select_start=raw["courseSelectStartDate"],
                select_end=raw["courseSelectEndDate"],
                raw_json=raw,
            )
        )
        session.commit()
    client = TestClient(create_app(container=container, start_scheduler=False))
    client.post("/api/v1/auth/setup", json={"password": "admin-secret"})
    return client, raw, state, select


async def test_boya_reservation_wait_full_retry_success(container, boya_reservation):
    client, raw, _state, select = boya_reservation
    calls = []

    def bark(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200})

    container.notification_dispatcher.transport = httpx.MockTransport(bark)
    with container.session_factory() as session:
        channel = session.query(NotificationChannel).first()
        channel.enabled = True
        channel.encrypted_device_key = container.cipher.encrypt("test-device-key")
        session.commit()
    assert client.post("/api/v1/boya/reserve/901").status_code == 200
    assert client.post("/api/v1/boya/reserve/901").status_code == 200
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_not_called()
    container.clock.advance(60)
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_not_called()
    with container.session_factory() as session:
        assert (
            session.query(TaskSchedule).filter_by(task_type="boya.auto_select").count()
            == 1
        )
        assert (
            session.query(TaskExecution)
            .filter_by(task_type="boya.auto_select")
            .one()
            .status
            == "waiting"
        )
    container.scheduler.recover_on_startup()
    raw["courseCurrentCount"] = 9
    container.clock.advance(29)
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_not_called()
    container.clock.advance(1)
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_awaited_once_with(901)
    container.clock.advance(60)
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_awaited_once()
    await container.notification_dispatcher.dispatch_due_events()
    assert len(calls) == 1
    assert calls[0]["title"] == "博雅报名成功: 艺术讲座"
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule).filter_by(task_type="boya.auto_select").one()
        )
        assert schedule.status == "completed"
        events = (
            session.query(NotificationEvent)
            .filter_by(event_key=f"boya:select_ok:{schedule.id}")
            .all()
        )
        assert len(events) == 1
        assert "艺术讲座" in events[0].body


async def test_boya_reservation_full_response_then_confirm(container, boya_reservation):
    from app.buaa.exceptions import BusinessTerminalError, OutcomeUnknownError

    client, raw, state, select = boya_reservation
    raw["courseCurrentCount"] = 9
    select.side_effect = [
        BusinessTerminalError("名额已满"),
        OutcomeUnknownError("请求超时"),
    ]
    client.post("/api/v1/boya/reserve/901")
    container.clock.advance(60)
    await container.scheduler.tick(wait_for_completion=True)
    container.clock.advance(30)
    await container.scheduler.tick(wait_for_completion=True)
    container.clock.advance(30)
    await container.scheduler.tick(wait_for_completion=True)
    assert select.await_count == 2
    state["chosen"] = True
    container.clock.advance(30)
    await container.scheduler.tick(wait_for_completion=True)
    assert select.await_count == 2
    with container.session_factory() as session:
        assert (
            session.query(TaskSchedule)
            .filter_by(task_type="boya.auto_select")
            .one()
            .status
            == "completed"
        )


async def test_boya_reservation_cancel_pause_and_deadline(container, boya_reservation):
    client, _raw, _state, select = boya_reservation
    client.post("/api/v1/boya/reserve/901")
    container.clock.advance(60)
    await container.scheduler.tick(wait_for_completion=True)
    assert client.post("/api/v1/boya/reserve/901/cancel").status_code == 200
    container.clock.advance(30)
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        assert (
            session.query(TaskExecution)
            .filter_by(task_type="boya.auto_select")
            .one()
            .status
            == "cancelled"
        )
    client.post("/api/v1/boya/reserve/901")
    for enabled in (False, True):
        assert (
            client.put(
                "/api/v1/network/modules/boya",
                json={"enabled": enabled, "config_json": {"auto_sign": False}},
            ).status_code
            == 200
        )
        with container.session_factory() as session:
            schedule = (
                session.query(TaskSchedule)
                .filter_by(task_type="boya.auto_select")
                .one()
            )
            assert schedule.status == ("enabled" if enabled else "paused")
            # Keep this scenario focused on reservation tasks.
            session.query(TaskSchedule).filter_by(
                task_type="boya.sync_courses"
            ).delete()
            session.commit()
    container.clock.advance(3600)
    await container.scheduler.tick(wait_for_completion=True)
    select.assert_not_called()
    assert client.post("/api/v1/boya/reserve/901").status_code == 400


def test_boya_campus_filter_switch(container, boya_reservation):
    from app.db.models import BoyaCourseCache

    client, *_ = boya_reservation
    with container.session_factory() as session:
        for cid, campus, selected in [
            (902, "杭州校区", False),
            (903, "学院路校区", True),
        ]:
            session.add(
                BoyaCourseCache(
                    course_id=cid, name=campus, campus=campus, selected=selected
                )
            )
        session.commit()
    assert {
        c["course_id"] for c in client.get("/api/v1/boya/courses").json()["courses"]
    } == {901, 903}
    assert (
        client.put(
            "/api/v1/network/modules/boya",
            json={"enabled": True, "config_json": {"filter_by_campus": False}},
        ).status_code
        == 200
    )
    assert {
        c["course_id"] for c in client.get("/api/v1/boya/courses").json()["courses"]
    } == {901, 902, 903}


async def test_boya_campus_reminders_and_refresh_preserve_reservation(
    container, boya_reservation, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.buaa.boya import BoyaClient, BoyaStatsData, parse_course
    from app.modules.services import sync_boya_all

    client, raw, _, _ = boya_reservation
    client.post("/api/v1/boya/reserve/901")
    remote = parse_course(
        {**raw, "id": 902, "courseCampusList": ["杭州校区"], "courseCurrentCount": 9}
    )
    monkeypatch.setattr(BoyaClient, "query_courses", AsyncMock(return_value=[remote]))
    monkeypatch.setattr(
        BoyaClient, "query_statistics", AsyncMock(return_value=BoyaStatsData(0, [], {}))
    )
    for enabled in (True, False):
        client.put(
            "/api/v1/network/modules/boya",
            json={"enabled": True, "config_json": {"filter_by_campus": enabled}},
        )
        await sync_boya_all(
            container.session_factory,
            container.session_mgr,
            container.registry,
            container.clock,
        )
        with container.session_factory() as session:
            assert session.query(NotificationEvent).filter_by(
                event_type="signup_reminder", status="pending"
            ).count() == int(not enabled)
            assert (
                session.query(TaskSchedule)
                .filter_by(task_type="boya.auto_select")
                .one()
                .status
                == "enabled"
            )
