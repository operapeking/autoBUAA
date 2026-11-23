from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.core.time import Clock
from app.db.models import ModuleConfig, NotificationEvent, TaskExecution, TaskSchedule
from app.framework.tasks import (
    TaskRegistry,
    cancel_active_executions,
    cancel_schedule,
    create_schedule,
)
from app.modules.config import MODULE_SETTINGS
from app.notifications.service import REMINDER_EVENTS, expire_events
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RefreshPlan:
    task_type: str
    name: str
    trigger_type: str
    trigger_config: Callable[[dict[str, Any]], dict[str, Any]]

    @property
    def business_key(self) -> str:
        return self.task_type.replace(".", ":")


REFRESH_PLANS: dict[str, RefreshPlan] = {
    "classroom": RefreshPlan(
        "classroom.sync_today",
        "课堂课程更新",
        "interval",
        lambda _cfg: {"interval_seconds": 3600},
    ),
    "boya": RefreshPlan(
        "boya.sync_courses",
        "博雅课程更新",
        "interval",
        lambda _cfg: {"interval_seconds": 600},
    ),
    "ygdk": RefreshPlan(
        "ygdk.sync_overview",
        "阳光打卡进度更新",
        "daily",
        lambda cfg: {"time": cfg.get("reminder_time", "18:00")},
    ),
    "td": RefreshPlan(
        "td.sync_overview",
        "体能锻炼状态更新",
        "interval",
        lambda _cfg: {"interval_seconds": 3600},
    ),
}

# Reservations are user intent and survive course refreshes; they only pause with the module.
RESERVATION_TASK = "boya.auto_select"


def cleanup_obsolete_state(
    session: Session, registry: TaskRegistry, clock: Clock
) -> None:
    """Retire persisted state whose task type or module no longer exists in code."""
    now = clock.now_utc()
    for sched in session.query(TaskSchedule).filter(TaskSchedule.status != "cancelled"):
        if sched.task_type not in registry:
            cancel_schedule(session, sched, now)
    session.flush()
    cancelled = {
        sched.id for sched in session.query(TaskSchedule).filter_by(status="cancelled")
    }
    for execution in session.query(TaskExecution).filter(
        TaskExecution.status.in_(["pending", "waiting", "pending_confirm", "running"])
    ):
        if execution.task_type not in registry or execution.schedule_id in cancelled:
            execution.status = "cancelled"
            execution.next_retry_at = execution.resume_at = None
            execution.finished_at = now
    known_modules = {*MODULE_SETTINGS, "system", "auth", "*"}
    expire_events(
        event
        for event in session.query(NotificationEvent).filter_by(status="pending")
        if event.module not in known_modules
    )
    for mod in session.query(ModuleConfig).all():
        schema = MODULE_SETTINGS.get(mod.module)
        if schema is None:
            session.delete(mod)
            continue
        known = {k: v for k, v in mod.config_json.items() if k in schema.model_fields}
        mod.config_json = schema.model_validate(known).model_dump()
    session.flush()


def reconcile_automation(
    session: Session,
    registry: TaskRegistry,
    clock: Clock,
    *,
    changed: str | None = None,
) -> None:
    """Align persisted schedules with module switches.

    `changed` names a module whose settings were just edited: its course windows and
    reminders are dropped and its refresh task runs immediately to rebuild them.
    """
    for module in MODULE_SETTINGS if changed is None else (changed,):
        mod = session.get(ModuleConfig, module)
        if mod is not None:
            _reconcile_module(session, registry, clock, mod, rebuild=module == changed)
    session.flush()


def refresh_enabled_modules(
    session: Session, registry: TaskRegistry, clock: Clock
) -> None:
    for module in MODULE_SETTINGS:
        mod = session.get(ModuleConfig, module)
        if mod and mod.enabled:
            reconcile_automation(session, registry, clock, changed=module)


def request_module_refresh(session: Session, module: str, clock: Clock) -> None:
    schedule = (
        session.query(TaskSchedule)
        .filter_by(business_key=REFRESH_PLANS[module].business_key, status="enabled")
        .first()
    )
    if schedule:
        schedule.next_run_at = clock.now_utc()


def _reconcile_module(
    session: Session,
    registry: TaskRegistry,
    clock: Clock,
    mod: ModuleConfig,
    *,
    rebuild: bool,
) -> None:
    now = clock.now_utc()
    plan = REFRESH_PLANS[mod.module]
    trigger_config = plan.trigger_config(mod.config_json)
    refresh = (
        session.query(TaskSchedule).filter_by(business_key=plan.business_key).first()
    )
    if (
        refresh is None
        or refresh.trigger_config_json != trigger_config
        or refresh.status != ("enabled" if mod.enabled else "paused")
    ):
        refresh = create_schedule(
            session,
            registry,
            name=plan.name,
            task_type=plan.task_type,
            parameters={},
            trigger_type=plan.trigger_type,
            trigger_config=trigger_config,
            business_key=plan.business_key,
            enabled=mod.enabled,
            clock=clock,
        )
        if mod.enabled:
            refresh.next_run_at = now
    elif rebuild and mod.enabled:
        refresh.next_run_at = now

    auto_sign = mod.config_json.get("auto_sign", True)
    for sched in session.query(TaskSchedule).filter(
        TaskSchedule.module == mod.module, TaskSchedule.id != refresh.id
    ):
        if sched.task_type == RESERVATION_TASK:
            if not mod.enabled and sched.status == "enabled":
                sched.status, sched.next_run_at = "paused", None
            elif mod.enabled and sched.status == "paused":
                sched.status, sched.next_run_at = "enabled", now
        elif sched.status == "enabled" and (
            not mod.enabled or rebuild or not auto_sign
        ):
            sched.status, sched.next_run_at = "paused", None

    if not mod.enabled or rebuild:
        cancel_active_executions(session, now, TaskExecution.module == mod.module)
        expire_events(
            event
            for event in session.query(NotificationEvent).filter_by(
                module=mod.module, status="pending"
            )
            if not mod.enabled or event.event_type in REMINDER_EVENTS
        )
