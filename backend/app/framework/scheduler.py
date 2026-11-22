from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.crypto import SecretCipher
from app.core.time import Clock, default_clock, ensure_utc
from app.db.models import (
    CampusAccount,
    ModuleConfig,
    TaskDefinition,
    TaskExecution,
    TaskSchedule,
    TaskStage,
)
from app.framework.tasks import TaskContext, TaskRegistry, TaskResult
from app.framework.triggers import compute_deadline, compute_next_run
from app.notifications.service import NotificationDispatcher, publish_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

QUERY_RETRY_DELAYS = (5, 15, 45)

MODULE_NAMES = {
    "classroom": "课堂签到",
    "boya": "博雅课程",
    "ygdk": "阳光打卡",
    "td": "体能锻炼",
}


def publish_failure(session: Session, ex: TaskExecution, clock: Clock) -> None:
    label = None
    if ex.schedule_id:
        sched = session.get(TaskSchedule, ex.schedule_id)
        label = sched.name if sched else None
    if not label:
        definition = session.get(TaskDefinition, ex.task_type)
        label = (definition.display_name if definition else None) or ex.task_type
    action, sep, obj = label.partition(": ")
    title = f"{action}失败: {obj}" if sep else f"{label}失败"
    publish_event(
        session,
        event_type="task_failed",
        module=ex.module,
        event_key=f"task:failed:{ex.business_key or ex.task_type}:{clock.now_shanghai().date()}",
        payload={
            "title": title,
            "reason": ex.result_summary or ex.error_message or "已超过任务有效时间",
        },
        execution_id=ex.id,
        schedule_id=ex.schedule_id,
        clock=clock,
    )


class UnifiedScheduler:
    """Persistent business scheduler driven by one asyncio loop."""

    def __init__(
        self,
        session_factory: Any,
        registry: TaskRegistry,
        cipher: SecretCipher,
        notification_dispatcher: NotificationDispatcher,
        clock: Clock | None = None,
        concurrency: int = 4,
        poll_seconds: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.cipher = cipher
        self.notification_dispatcher = notification_dispatcher
        self.clock = clock or default_clock
        self.concurrency = max(1, concurrency)
        self.poll_seconds = poll_seconds

        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._module_locks: dict[str, asyncio.Lock] = {}
        self._runner: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[Any]] = set()
        self.last_heartbeat_at: datetime | None = None

    def module_lock(self, module: str) -> asyncio.Lock:
        if module not in self._module_locks:
            self._module_locks[module] = asyncio.Lock()
        return self._module_locks[module]

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

    def recover_on_startup(self) -> None:
        """Recover interrupted executions and stale schedule triggers after restart."""
        now_utc = self.clock.now_utc()
        with self.session_factory() as session:
            interrupted = (
                session.query(TaskExecution)
                .filter(TaskExecution.status == "running")
                .all()
            )
            for ex in interrupted:
                if ex.deadline_at and now_utc >= ex.deadline_at:
                    ex.status = "expired"
                    ex.finished_at = now_utc
                    ex.error_message = "服务停机期间已超过任务有效时间"
                    ex.result_summary = "已超过任务有效时间"
                    publish_failure(session, ex, self.clock)
                else:
                    ex.status = "pending"
                    ex.next_retry_at = now_utc
                    ex.wait_reason = "服务重启，等待重新执行"

            enabled_schedules = (
                session.query(TaskSchedule)
                .filter(TaskSchedule.status == "enabled")
                .all()
            )
            for sched in enabled_schedules:
                if sched.next_run_at and sched.next_run_at < now_utc:
                    if sched.trigger_type == "window":
                        end_at = compute_deadline(
                            sched.trigger_type,
                            sched.trigger_config_json,
                            sched.next_run_at,
                        )
                        if end_at and now_utc >= end_at:
                            sched.status = "completed"
                            sched.next_run_at = None
                            sched.last_result_status = "expired"
                            sched.last_result_summary = "服务停机期间已超过任务有效时间"
                            continue
                    elif sched.trigger_type == "once":
                        deadline = compute_deadline(
                            sched.trigger_type,
                            sched.trigger_config_json,
                            sched.next_run_at,
                        )
                        if deadline and now_utc >= deadline:
                            sched.status = "completed"
                            sched.next_run_at = None
                            sched.last_result_status = "expired"
                            sched.last_result_summary = "服务停机期间已超过任务有效时间"
                            continue
            session.commit()

    def _check_module_blocking(
        self, session: Session, task_type: str, module: str
    ) -> str | None:
        if module == "system":
            return None
        if task_type not in {reg.task_type for reg in self.registry.all()}:
            return "任务类型不可用"

        mod_cfg = session.get(ModuleConfig, module)
        if mod_cfg is not None and not mod_cfg.enabled:
            return f"{MODULE_NAMES.get(module, module)}模块未启用"
        if (
            mod_cfg
            and "auto_sign" in task_type
            and not (mod_cfg.config_json or {}).get("auto_sign", True)
        ):
            return "自动签到已关闭"

        reg = self.registry.get(task_type)
        if not reg.available:
            return reg.unavailable_reason or "任务类型当前不可用"

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
                        TaskSchedule.next_run_at.is_not(None),
                        TaskSchedule.next_run_at <= now_utc,
                    )
                    .order_by(TaskSchedule.next_run_at.asc(), TaskSchedule.id.asc())
                    .all()
                )
                for sched in due_schedules:
                    ex_id = self._claim_due_schedule(session, sched, now_utc)
                    if ex_id is not None:
                        claimed_ids.append(ex_id)

                due_retries = (
                    session.query(TaskExecution)
                    .filter(
                        TaskExecution.status.in_(
                            ["pending", "waiting", "pending_confirm"]
                        ),
                        (
                            (
                                (
                                    TaskExecution.status.in_(
                                        ["pending", "pending_confirm"]
                                    )
                                )
                                & (
                                    TaskExecution.next_retry_at.is_(None)
                                    | (TaskExecution.next_retry_at <= now_utc)
                                )
                            )
                            | (
                                (TaskExecution.status == "waiting")
                                & (
                                    TaskExecution.resume_at.is_not(None)
                                    & (TaskExecution.resume_at <= now_utc)
                                )
                            )
                        ),
                    )
                    .order_by(TaskExecution.id.asc())
                    .limit(20)
                    .all()
                )
                for ex in due_retries:
                    if ex.id not in claimed_ids:
                        if ex.deadline_at and now_utc >= ex.deadline_at:
                            ex.status = "expired"
                            ex.finished_at = now_utc
                            ex.result_summary = "已超过任务有效时间"
                            publish_failure(session, ex, self.clock)
                            if ex.schedule_id:
                                sched = session.get(TaskSchedule, ex.schedule_id)
                                if sched and sched.trigger_type in {"once", "window"}:
                                    sched.status = "completed"
                                    sched.next_run_at = None
                                    sched.last_result_status = "expired"
                                    sched.last_result_summary = ex.result_summary
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
                                ex.wait_reason = block_reason
                                ex.resume_at = now_utc + timedelta(seconds=30)
                                ex.status = "waiting"
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

        # Compute next future trigger (coalescing missed runs)
        if sched.trigger_type == "once":
            sched.next_run_at = None
        else:
            sched.next_run_at = compute_next_run(
                sched.trigger_type,
                sched.trigger_config_json,
                max(now_utc, due_at),
                inclusive=False,
            )
            if sched.next_run_at is None and sched.trigger_type == "window":
                # Keep enabled until execution finishes or expires
                pass

        if sched.trigger_type in {"daily", "interval"}:
            due_at = now_utc
        deadline = compute_deadline(
            sched.trigger_type, sched.trigger_config_json, due_at
        )
        if deadline and now_utc >= deadline:
            ex = TaskExecution(
                schedule_id=sched.id,
                schedule_version=sched.version,
                task_type=sched.task_type,
                module=sched.module,
                business_key=sched.business_key,
                scheduled_for=due_at,
                deadline_at=deadline,
                status="expired",
                stage="init",
                finished_at=now_utc,
                result_summary="已超过任务有效时间",
            )
            try:
                with session.begin_nested():
                    session.add(ex)
                    session.flush()
                    publish_failure(session, ex, self.clock)
            except IntegrityError:
                return None
            if sched.trigger_type in {"once", "window"} and sched.next_run_at is None:
                sched.status = "completed"
            sched.last_result_status = "expired"
            sched.last_result_summary = ex.result_summary
            return None

        block_reason = self._check_module_blocking(
            session, sched.task_type, sched.module
        )
        if block_reason:
            sched.blocking_reason = block_reason
            return None
        sched.blocking_reason = None

        running = (
            session.query(TaskExecution)
            .filter(
                TaskExecution.schedule_id == sched.id,
                TaskExecution.status.in_(
                    ["pending", "running", "waiting", "pending_confirm"]
                ),
            )
            .first()
        )
        if running is not None:
            return None

        ex = TaskExecution(
            schedule_id=sched.id,
            schedule_version=sched.version,
            task_type=sched.task_type,
            module=sched.module,
            business_key=sched.business_key,
            scheduled_for=due_at,
            deadline_at=deadline,
            status="pending",
            stage="init",
            next_retry_at=now_utc,
        )
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
                if ex is None or ex.status in {
                    "succeeded",
                    "failed",
                    "expired",
                    "skipped",
                    "cancelled",
                }:
                    return
                if ex.deadline_at and now_utc >= ex.deadline_at:
                    ex.status = "expired"
                    ex.finished_at = now_utc
                    ex.result_summary = "已超过任务有效时间"
                    publish_failure(session, ex, self.clock)
                    session.commit()
                    return
                if ex.schedule_id:
                    sched = session.get(TaskSchedule, ex.schedule_id)
                    if sched and sched.status in {"paused", "cancelled"}:
                        return
                ex.status = "running"
                if ex.started_at is None:
                    ex.started_at = now_utc
                task_type = ex.task_type
                module = ex.module
                schedule_id = ex.schedule_id
                business_key = ex.business_key
                deadline_at = ex.deadline_at
                stage = ex.stage
                retry_count = ex.retry_count
                raw_params = {}
                if schedule_id:
                    sched = session.get(TaskSchedule, schedule_id)
                    if sched:
                        raw_params = dict(sched.parameters_json or {})
                session.commit()

            lock = self.module_lock(module)
            async with lock:
                with self.session_factory() as session:
                    current = session.get(TaskExecution, execution_id)
                    schedule = (
                        session.get(TaskSchedule, schedule_id) if schedule_id else None
                    )
                    blocked = self._check_module_blocking(session, task_type, module)
                    if current is None or current.status == "cancelled":
                        return
                    if blocked or (
                        schedule and schedule.status in {"paused", "cancelled"}
                    ):
                        current.status = "cancelled"
                        current.finished_at = self.clock.now_utc()
                        current.result_summary = blocked or "计划已停止"
                        current.next_retry_at = current.resume_at = None
                        session.commit()
                        return
                reg = self.registry.get(task_type)
                validated_params = self.registry.validate_parameters(
                    task_type, raw_params
                )
                ctx = TaskContext(
                    execution_id=execution_id,
                    schedule_id=schedule_id,
                    task_type=task_type,
                    module=module,
                    business_key=business_key,
                    deadline_at=deadline_at,
                    stage=stage,
                    retry_count=retry_count,
                    clock=self.clock,
                    db_session_factory=self.session_factory,
                    cipher=self.cipher,
                )
                logger.debug("Executing task %s (%s)", execution_id, task_type)
                try:
                    result = await reg.handler(ctx, validated_params)
                except Exception as exc:
                    logger.exception(
                        "Task execution %s (%s) raised exception",
                        execution_id,
                        task_type,
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
                    task_type,
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
                stage_row = (
                    session.query(TaskStage)
                    .filter_by(execution_id=execution_id, stage_name=ex.stage)
                    .first()
                )
                if stage_row is None:
                    session.add(
                        TaskStage(
                            execution_id=execution_id,
                            stage_name=ex.stage,
                            status=result.status,
                            payload_json=result.stage_payload,
                            resume_at=result.resume_at,
                        )
                    )
                else:
                    stage_row.status = result.status
                    stage_row.payload_json = result.stage_payload
                    stage_row.resume_at = result.resume_at

            ex.result_summary = result.summary
            merged_data = dict(ex.result_data_json or {})
            merged_data.update(result.data or {})
            ex.result_data_json = merged_data

            if result.status == "succeeded":
                ex.status = "succeeded"
                ex.finished_at = now_utc
                ex.next_retry_at = None
                ex.resume_at = None
                ex.wait_reason = None
                ex.error_message = None
                if sched:
                    sched.last_run_at = now_utc
                    sched.last_result_status = "succeeded"
                    sched.last_result_summary = result.summary
                    if sched.trigger_type == "once" or (
                        sched.trigger_type == "window" and result.complete_window
                    ):
                        sched.status = "completed"
                        sched.next_run_at = None

            elif result.status == "waiting":
                ex.status = "waiting"
                ex.wait_reason = result.wait_reason
                ex.resume_at = (
                    ensure_utc(result.resume_at)
                    if result.resume_at
                    else (now_utc + timedelta(seconds=30))
                )
                if sched:
                    sched.last_run_at = now_utc
                    sched.last_result_status = "waiting"
                    sched.last_result_summary = result.summary

            elif result.status == "pending_confirm":
                ex.status = "pending_confirm"
                ex.wait_reason = (
                    result.wait_reason or "写操作超时或状态不明确，等待查询确认"
                )
                ex.next_retry_at = now_utc + timedelta(seconds=10)
                if sched:
                    sched.last_run_at = now_utc
                    sched.last_result_status = "pending_confirm"
                    sched.last_result_summary = result.summary

            else:  # failed
                if result.retryable and ex.retry_count < len(QUERY_RETRY_DELAYS):
                    delay = QUERY_RETRY_DELAYS[ex.retry_count]
                    next_try = now_utc + timedelta(seconds=delay)
                    if ex.deadline_at and next_try >= ex.deadline_at:
                        ex.status = "expired"
                        ex.finished_at = now_utc
                        ex.error_message = result.error_message or result.summary
                        ex.next_retry_at = None
                    else:
                        ex.retry_count += 1
                        ex.status = "pending"
                        ex.next_retry_at = next_try
                        ex.error_message = result.error_message or result.summary
                else:
                    ex.status = "failed"
                    ex.finished_at = now_utc
                    ex.error_message = result.error_message or result.summary
                    ex.next_retry_at = None
                    if sched and (
                        sched.trigger_type == "once" or result.complete_window
                    ):
                        sched.status = "completed"
                        sched.next_run_at = None
                if sched:
                    sched.last_run_at = now_utc
                    sched.last_result_status = ex.status
                    sched.last_result_summary = result.summary

            # Persist notification events in the SAME DB transaction
            if ex.status in {"failed", "expired"}:
                publish_failure(session, ex, self.clock)
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
