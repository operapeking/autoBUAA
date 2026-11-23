from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.time import Clock, default_clock, ensure_utc
from app.db.models import CampusAccount, ModuleConfig, TaskExecution, TaskSchedule
from app.framework.tasks import TaskContext, TaskRegistry, TaskResult, upsert_stage
from app.framework.triggers import compute_deadline, compute_next_run
from app.notifications.service import NotificationDispatcher, publish_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

QUERY_RETRY_DELAYS = (5, 15, 45)
ACTIVE_STATUSES = ("pending", "running", "waiting", "pending_confirm")
TERMINAL_STATUSES = {"succeeded", "failed", "expired", "skipped", "cancelled"}
AUTO_SIGN_TASKS = {"classroom.auto_signin", "boya.auto_signin", "boya.auto_signout"}
EXPIRED_SUMMARY = "已超过任务有效时间"

MODULE_NAMES = {
    "classroom": "课堂签到",
    "boya": "博雅课程",
    "ygdk": "阳光打卡",
    "td": "体能锻炼",
}


class UnifiedScheduler:
    """Persistent business scheduler driven by one asyncio loop."""

    def __init__(
        self,
        session_factory: Any,
        registry: TaskRegistry,
        notification_dispatcher: NotificationDispatcher,
        clock: Clock | None = None,
        concurrency: int = 4,
        poll_seconds: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.notification_dispatcher = notification_dispatcher
        self.clock = clock or default_clock
        self.poll_seconds = poll_seconds

        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._module_locks: dict[str, asyncio.Lock] = {}
        self._runner: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[Any]] = set()
        self.last_heartbeat_at: datetime | None = None

    def module_lock(self, module: str) -> asyncio.Lock:
        return self._module_locks.setdefault(module, asyncio.Lock())

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def start(self) -> None:
        if not self.running:
            self.recover_on_startup()
            self._runner = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    def _publish_failure(self, session: Session, ex: TaskExecution) -> None:
        sched = session.get(TaskSchedule, ex.schedule_id) if ex.schedule_id else None
        label = sched.name if sched else None
        if not label:
            label = (
                self.registry.get(ex.task_type).display_name
                if ex.task_type in self.registry
                else ex.task_type
            )
        action, sep, obj = label.partition(": ")
        publish_event(
            session,
            event_type="task_failed",
            module=ex.module,
            event_key=f"task:failed:{ex.business_key or ex.task_type}:{self.clock.now_shanghai().date()}",
            payload={
                "title": f"{action}失败: {obj}" if sep else f"{label}失败",
                "reason": ex.result_summary or ex.error_message or EXPIRED_SUMMARY,
            },
            execution_id=ex.id,
            schedule_id=ex.schedule_id,
            clock=self.clock,
        )

    def _expire_execution(
        self,
        session: Session,
        ex: TaskExecution,
        now_utc: datetime,
        error_message: str | None = None,
    ) -> None:
        """Mark an execution past its deadline and close its one-shot schedule."""
        ex.status = "expired"
        ex.finished_at = now_utc
        ex.next_retry_at = ex.resume_at = None
        ex.result_summary = EXPIRED_SUMMARY
        if error_message:
            ex.error_message = error_message
        self._publish_failure(session, ex)
        sched = session.get(TaskSchedule, ex.schedule_id) if ex.schedule_id else None
        if sched and sched.trigger_type in {"once", "window"}:
            sched.status = "completed"
            sched.next_run_at = None
            sched.last_result_status = "expired"
            sched.last_result_summary = EXPIRED_SUMMARY

    def recover_on_startup(self) -> None:
        """Recover interrupted executions and stale schedule triggers after restart."""
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            for ex in session.query(TaskExecution).filter_by(status="running").all():
                if ex.deadline_at and now_utc >= ex.deadline_at:
                    self._expire_execution(
                        session, ex, now_utc, "服务停机期间已超过任务有效时间"
                    )
                else:
                    ex.status = "pending"
                    ex.next_retry_at = now_utc
                    ex.wait_reason = "服务重启，等待重新执行"

            stale = (
                session.query(TaskSchedule)
                .filter(
                    TaskSchedule.status == "enabled",
                    TaskSchedule.trigger_type.in_(["once", "window"]),
                    TaskSchedule.next_run_at < now_utc,
                )
                .all()
            )
            for sched in stale:
                if sched.status != "enabled" or sched.next_run_at is None:
                    continue  # already closed by an expired execution above
                deadline = compute_deadline(
                    sched.trigger_type, sched.trigger_config_json, sched.next_run_at
                )
                if deadline and now_utc >= deadline:
                    sched.status = "completed"
                    sched.next_run_at = None
                    sched.last_result_status = "expired"
                    sched.last_result_summary = "服务停机期间已超过任务有效时间"
            session.commit()

    def _check_module_blocking(
        self, session: Session, task_type: str, module: str
    ) -> str | None:
        if module == "system":
            return None
        if task_type not in self.registry:
            return "任务类型不可用"

        mod_cfg = session.get(ModuleConfig, module)
        if mod_cfg is not None:
            if not mod_cfg.enabled:
                return f"{MODULE_NAMES.get(module, module)}模块未启用"
            if task_type in AUTO_SIGN_TASKS and not mod_cfg.config_json.get(
                "auto_sign", True
            ):
                return "自动签到已关闭"

        account = session.query(CampusAccount).first()
        if account is not None:
            if not account.enabled:
                return "校园账号已停用"
            if not account.student_id or not account.encrypted_password:
                return "校园账号尚未配置"
            if account.auth_status == "captcha_required":
                return "统一认证需要输入验证码，已暂停业务任务"
        return None

    async def tick(self, wait_for_completion: bool = False) -> int:
        """Single scheduler cycle: claim due schedules/executions and dispatch notifications."""
        async with self._tick_lock:
            now_utc = self.clock.now_utc()
            self.last_heartbeat_at = now_utc
            claimed_ids: list[int] = []

            with self.session_factory() as session:
                due_schedules = (
                    session.query(TaskSchedule)
                    .filter(
                        TaskSchedule.status == "enabled",
                        TaskSchedule.next_run_at <= now_utc,
                    )
                    .order_by(TaskSchedule.next_run_at.asc(), TaskSchedule.id.asc())
                    .all()
                )
                for sched in due_schedules:
                    ex_id = self._claim_due_schedule(session, sched, now_utc)
                    if ex_id is not None:
                        claimed_ids.append(ex_id)

                retry_due = TaskExecution.status.in_(["pending", "pending_confirm"]) & (
                    TaskExecution.next_retry_at.is_(None)
                    | (TaskExecution.next_retry_at <= now_utc)
                )
                resume_due = (TaskExecution.status == "waiting") & (
                    TaskExecution.resume_at <= now_utc
                )
                due_retries = (
                    session.query(TaskExecution)
                    .filter(retry_due | resume_due)
                    .order_by(TaskExecution.id.asc())
                    .limit(20)
                    .all()
                )
                for ex in due_retries:
                    if ex.id in claimed_ids:
                        continue
                    if ex.deadline_at and now_utc >= ex.deadline_at:
                        self._expire_execution(session, ex, now_utc)
                        continue
                    if ex.schedule_id:
                        sched = session.get(TaskSchedule, ex.schedule_id)
                        if sched and sched.status in {"paused", "cancelled"}:
                            continue
                        block_reason = self._check_module_blocking(
                            session, ex.task_type, ex.module
                        )
                        if block_reason:
                            if sched:
                                sched.blocking_reason = block_reason
                            ex.status = "waiting"
                            ex.wait_reason = block_reason
                            ex.resume_at = now_utc + timedelta(seconds=30)
                            continue
                    claimed_ids.append(ex.id)

                session.commit()

            if wait_for_completion:
                await asyncio.gather(*(self.execute_one(eid) for eid in claimed_ids))
            else:
                for eid in claimed_ids:
                    task = asyncio.create_task(self.execute_one(eid))
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)

            await self.notification_dispatcher.dispatch_due_events()
            return len(claimed_ids)

    def _claim_due_schedule(
        self, session: Session, sched: TaskSchedule, now_utc: datetime
    ) -> int | None:
        due_at = sched.next_run_at
        assert due_at is not None

        # Coalesce missed runs into the next future trigger.
        sched.next_run_at = (
            None
            if sched.trigger_type == "once"
            else compute_next_run(
                sched.trigger_type,
                sched.trigger_config_json,
                max(now_utc, due_at),
                inclusive=False,
            )
        )
        if sched.trigger_type in {"daily", "interval"}:
            due_at = now_utc
        deadline = compute_deadline(
            sched.trigger_type, sched.trigger_config_json, due_at
        )
        ex = TaskExecution(
            schedule_id=sched.id,
            schedule_version=sched.version,
            task_type=sched.task_type,
            module=sched.module,
            business_key=sched.business_key,
            scheduled_for=due_at,
            deadline_at=deadline,
            stage="init",
        )

        if deadline and now_utc >= deadline:
            ex.status = "expired"
            ex.finished_at = now_utc
            ex.result_summary = EXPIRED_SUMMARY
            try:
                with session.begin_nested():
                    session.add(ex)
                    session.flush()
                    self._publish_failure(session, ex)
            except IntegrityError:
                return None
            if sched.trigger_type in {"once", "window"} and sched.next_run_at is None:
                sched.status = "completed"
            sched.last_result_status = "expired"
            sched.last_result_summary = EXPIRED_SUMMARY
            return None

        block_reason = self._check_module_blocking(
            session, sched.task_type, sched.module
        )
        sched.blocking_reason = block_reason
        if block_reason:
            return None

        active = (
            session.query(TaskExecution)
            .filter(
                TaskExecution.schedule_id == sched.id,
                TaskExecution.status.in_(ACTIVE_STATUSES),
            )
            .first()
        )
        if active is not None:
            return None

        ex.status = "pending"
        ex.next_retry_at = now_utc
        try:
            with session.begin_nested():
                session.add(ex)
                session.flush()
        except IntegrityError:
            return None
        return ex.id

    async def execute_one(self, execution_id: int) -> None:
        async with self._semaphore:
            now_utc = self.clock.now_utc()
            with self.session_factory() as session:
                ex = session.get(TaskExecution, execution_id)
                if ex is None or ex.status in TERMINAL_STATUSES:
                    return
                if ex.deadline_at and now_utc >= ex.deadline_at:
                    self._expire_execution(session, ex, now_utc)
                    session.commit()
                    return
                sched = (
                    session.get(TaskSchedule, ex.schedule_id)
                    if ex.schedule_id
                    else None
                )
                if sched and sched.status in {"paused", "cancelled"}:
                    return
                ex.status = "running"
                if ex.started_at is None:
                    ex.started_at = now_utc
                raw_params = dict(sched.parameters_json or {}) if sched else {}
                ctx = TaskContext(
                    execution_id=execution_id,
                    schedule_id=ex.schedule_id,
                    task_type=ex.task_type,
                    module=ex.module,
                    business_key=ex.business_key,
                    deadline_at=ex.deadline_at,
                    stage=ex.stage,
                    retry_count=ex.retry_count,
                    clock=self.clock,
                    db_session_factory=self.session_factory,
                )
                session.commit()

            async with self.module_lock(ctx.module):
                with self.session_factory() as session:
                    current = session.get(TaskExecution, execution_id)
                    if current is None or current.status == "cancelled":
                        return
                    schedule = (
                        session.get(TaskSchedule, ctx.schedule_id)
                        if ctx.schedule_id
                        else None
                    )
                    blocked = self._check_module_blocking(
                        session, ctx.task_type, ctx.module
                    )
                    if blocked or (
                        schedule and schedule.status in {"paused", "cancelled"}
                    ):
                        current.status = "cancelled"
                        current.finished_at = self.clock.now_utc()
                        current.result_summary = blocked or "计划已停止"
                        current.next_retry_at = current.resume_at = None
                        session.commit()
                        return
                reg = self.registry.get(ctx.task_type)
                params = self.registry.validate_parameters(ctx.task_type, raw_params)
                logger.debug("Executing task %s (%s)", execution_id, ctx.task_type)
                try:
                    result = await reg.handler(ctx, params)
                except Exception as exc:
                    logger.exception(
                        "Task execution %s (%s) raised exception",
                        execution_id,
                        ctx.task_type,
                    )
                    result = TaskResult(
                        status="failed",
                        summary=f"任务执行异常: {exc}",
                        error_message=str(exc),
                        retryable=True,
                    )
                logger.debug(
                    "Task %s (%s): %s - %s",
                    execution_id,
                    ctx.task_type,
                    result.status,
                    result.summary,
                )
                self._finalize_execution(execution_id, result)

            await self.notification_dispatcher.dispatch_due_events()

    def _finalize_execution(self, execution_id: int, result: TaskResult) -> None:
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            ex = session.get(TaskExecution, execution_id)
            if ex is None:
                return
            sched = (
                session.get(TaskSchedule, ex.schedule_id) if ex.schedule_id else None
            )

            if result.next_stage:
                ex.stage = result.next_stage
            if result.stage_payload is not None:
                upsert_stage(
                    session,
                    execution_id,
                    ex.stage,
                    result.status,
                    result.stage_payload,
                    result.resume_at,
                )

            ex.result_summary = result.summary
            ex.result_data_json = {**(ex.result_data_json or {}), **result.data}

            if result.status == "succeeded":
                ex.status = "succeeded"
                ex.finished_at = now_utc
                ex.next_retry_at = ex.resume_at = None
                ex.wait_reason = ex.error_message = None
                if sched and (
                    sched.trigger_type == "once"
                    or (sched.trigger_type == "window" and result.complete_window)
                ):
                    sched.status = "completed"
                    sched.next_run_at = None

            elif result.status == "waiting":
                ex.status = "waiting"
                ex.wait_reason = result.wait_reason
                ex.resume_at = (
                    ensure_utc(result.resume_at)
                    if result.resume_at
                    else now_utc + timedelta(seconds=30)
                )

            elif result.status == "pending_confirm":
                ex.status = "pending_confirm"
                ex.wait_reason = (
                    result.wait_reason or "写操作超时或状态不明确，等待查询确认"
                )
                ex.next_retry_at = now_utc + timedelta(seconds=10)

            else:
                ex.error_message = result.error_message or result.summary
                ex.next_retry_at = None
                next_try = None
                if result.retryable and ex.retry_count < len(QUERY_RETRY_DELAYS):
                    next_try = now_utc + timedelta(
                        seconds=QUERY_RETRY_DELAYS[ex.retry_count]
                    )
                if next_try and not (ex.deadline_at and next_try >= ex.deadline_at):
                    ex.retry_count += 1
                    ex.status = "pending"
                    ex.next_retry_at = next_try
                else:
                    # A retry that would land past the deadline expires instead of failing.
                    ex.status = "expired" if next_try else "failed"
                    ex.finished_at = now_utc
                    if (
                        ex.status == "failed"
                        and sched
                        and (sched.trigger_type == "once" or result.complete_window)
                    ):
                        sched.status = "completed"
                        sched.next_run_at = None

            if sched:
                sched.last_run_at = now_utc
                sched.last_result_status = ex.status
                sched.last_result_summary = result.summary

            # Persist notification events in the same DB transaction.
            if ex.status in {"failed", "expired"}:
                self._publish_failure(session, ex)
            for ev in result.events:
                publish_event(
                    session,
                    event_type=ev.event_type,
                    event_key=ev.event_key,
                    module=ev.module or ex.module,
                    payload=ev.payload,
                    expires_at=ev.expires_at,
                    execution_id=ex.id,
                    schedule_id=ex.schedule_id,
                    clock=self.clock,
                )

            session.commit()
