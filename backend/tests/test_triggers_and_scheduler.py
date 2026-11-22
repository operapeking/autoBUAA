from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from app.container import AppContainer
from app.core.config import Settings
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock
from app.db.models import TaskExecution, TaskSchedule
from app.framework.tasks import (
    DuplicateTaskTypeError,
    ParameterValidationError,
    TaskContext,
    TaskRegistry,
    TaskResult,
    UnknownTaskTypeError,
    create_schedule,
)
from app.framework.triggers import TriggerValidationError, preview_next_runs
from pydantic import BaseModel


def _make_container(tmp_path: Path, fixed_shanghai: datetime) -> AppContainer:
    clock = Clock(fixed_shanghai.astimezone(UTC_TZ))
    settings = Settings(
        data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
    )
    container = AppContainer(settings=settings, clock=clock, run_migrations=True)

    class RetryParams(BaseModel):
        message: str = "hello"
        fail_times: int = 0

    async def retry_handler(ctx: TaskContext, params: RetryParams) -> TaskResult:
        attempts = ctx.get_stage_payload("demo").get("attempts", 0)
        if attempts < params.fail_times:
            return TaskResult(
                status="failed",
                summary="retry",
                retryable=True,
                next_stage="demo",
                stage_payload={"attempts": attempts + 1},
            )
        return TaskResult(
            status="succeeded",
            summary=f"示例任务完成: {params.message}",
            complete_window=True,
        )

    container.registry.register_task(
        "demo.echo",
        RetryParams,
        retry_handler,
        module="system",
        display_name="Test retry",
    )
    return container


def test_task_registration_rejects_duplicate_and_invalid_params() -> None:
    registry = TaskRegistry()

    class P(BaseModel):
        count: int

    async def handler(_ctx: TaskContext, _p: P) -> TaskResult:
        return TaskResult(status="succeeded", summary="ok")

    registry.register_task("t.one", P, handler, module="system", display_name="One")
    with pytest.raises(DuplicateTaskTypeError):
        registry.register_task("t.one", P, handler, module="system", display_name="Dup")

    with pytest.raises(UnknownTaskTypeError):
        registry.validate_parameters("t.unknown", {})

    with pytest.raises(ParameterValidationError) as exc_info:
        registry.validate_parameters("t.one", {"count": "not-an-int"})
    assert exc_info.value.errors[0]["field"] == "count"


def test_trigger_previews_and_validation() -> None:
    now_utc = datetime(2026, 11, 15, 10, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)

    # Daily
    daily_runs = preview_next_runs("daily", {"time": "12:00:00"}, now_utc, count=10)
    assert len(daily_runs) == 10
    assert daily_runs[0].startswith("2026-11-15T12:00:00+08:00")
    assert daily_runs[1].startswith("2026-11-16T12:00:00+08:00")

    # Interval
    int_runs = preview_next_runs(
        "interval", {"interval_seconds": 60}, now_utc, count=10
    )
    assert len(int_runs) == 10
    assert int_runs[0].startswith("2026-11-15T10:00:00+08:00")
    assert int_runs[1].startswith("2026-11-15T10:01:00+08:00")

    # Cross-day Window
    win_runs = preview_next_runs(
        "window",
        {
            "start_at": "2026-11-15 23:30:00",
            "end_at": "2026-11-16 00:30:00",
            "poll_interval_seconds": 1200,
        },
        now_utc,
        count=10,
    )
    assert win_runs == [
        "2026-11-15T23:30:00+08:00",
        "2026-11-15T23:50:00+08:00",
        "2026-11-16T00:10:00+08:00",
    ]

    # Invalid window end <= start
    with pytest.raises(TriggerValidationError) as exc:
        preview_next_runs(
            "window",
            {"start_at": "2026-11-15 12:00:00", "end_at": "2026-11-15 11:00:00"},
            now_utc,
        )
    assert exc.value.field == "end_at"


async def test_scheduler_once_retry_window_and_recovery(tmp_path: Path) -> None:
    start_sh = datetime(2026, 11, 15, 10, 0, 0, tzinfo=SHANGHAI_TZ)
    container = _make_container(tmp_path, start_sh)

    # 1. Create a once schedule that fails twice then succeeds (5s, 15s backoff)
    with container.session_factory() as session:
        sched = create_schedule(
            session,
            container.registry,
            name="Retry Demo",
            task_type="demo.echo",
            parameters={"message": "done", "fail_times": 2},
            trigger_type="once",
            trigger_config={"run_at": "2026-11-15 10:00:00"},
            clock=container.clock,
        )
        session.commit()
        sched_id = sched.id

    claimed = await container.scheduler.tick(wait_for_completion=True)
    assert claimed == 1
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=sched_id).one()
        assert ex.status == "pending"
        assert ex.retry_count == 1

    # Advance 5s -> second attempt fails -> schedules +15s
    container.clock.advance(5)
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=sched_id).one()
        assert ex.status == "pending"
        assert ex.retry_count == 2

    # Advance 15s -> third attempt succeeds!
    container.clock.advance(15)
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=sched_id).one()
        sched = session.get(TaskSchedule, sched_id)
        assert ex.status == "succeeded"
        assert ex.result_summary == "示例任务完成: done"
        assert sched is not None and sched.status == "completed"


async def test_scheduler_window_stops_at_deadline_and_recovers_running(
    tmp_path: Path,
) -> None:
    start_sh = datetime(2026, 11, 15, 23, 50, 0, tzinfo=SHANGHAI_TZ)
    container = _make_container(tmp_path, start_sh)

    # Window crossing midnight: 23:55 -> 00:05
    with container.session_factory() as session:
        sched = create_schedule(
            session,
            container.registry,
            name="Cross Day Window",
            task_type="demo.echo",
            parameters={"message": "win", "fail_times": 10},
            trigger_type="window",
            trigger_config={
                "start_at": "2026-11-15 23:55:00",
                "end_at": "2026-11-16 00:05:00",
                "poll_interval_seconds": 60,
            },
            clock=container.clock,
        )
        session.commit()
        sched_id = sched.id

    # Before window: nothing runs
    assert await container.scheduler.tick(wait_for_completion=True) == 0

    # At 23:55:00: runs and fails retryably
    container.clock.advance(300)
    assert await container.scheduler.tick(wait_for_completion=True) == 1

    # Simulate container crash while an execution was running, then restart after deadline (00:06:00)
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=sched_id).first()
        assert ex is not None
        ex.status = "running"
        session.commit()

    container.clock.set(datetime(2026, 11, 16, 0, 6, 0, tzinfo=SHANGHAI_TZ))
    container.scheduler.recover_on_startup()
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=sched_id).first()
        assert ex is not None and ex.status == "expired"
