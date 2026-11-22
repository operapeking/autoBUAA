import re

from app.core.time import Clock
from app.db.models import (
    ModuleConfig,
    NotificationEvent,
    TaskDefinition,
    TaskExecution,
    TaskSchedule,
)
from app.framework.tasks import TaskRegistry, create_schedule
from app.modules.config import MODULE_SETTINGS
from sqlalchemy.orm import Session


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
        .filter(
            TaskSchedule.module == module,
            TaskSchedule.trigger_type.in_(["interval", "daily"]),
            TaskSchedule.status == "enabled",
        )
        .first()
    )
    if schedule:
        schedule.next_run_at = clock.now_utc()


def reconcile_automation(
    session: Session,
    registry: TaskRegistry,
    clock: Clock,
    *,
    changed: str | None = None,
) -> None:
    """Reconcile business switches with persisted schedules in the same transaction."""
    now = clock.now_utc()
    supported = {task.task_type for task in registry.all()}
    plans = {
        "classroom.sync_today": ("interval", r"classroom:sync_today"),
        "boya.sync_courses": ("interval", r"boya:sync_courses"),
        "ygdk.sync_overview": ("daily", r"ygdk:sync_overview"),
        "td.sync_overview": ("interval", r"td:sync_overview"),
        "classroom.auto_signin": ("window", r"classroom:signin:\d{8}:.+"),
        "boya.auto_signin": ("window", r"boya:sign:\d+"),
        "boya.auto_signout": ("window", r"boya:signout:\d+"),
        "boya.auto_select": ("window", r"boya:reserve:\d+"),
    }
    for sched in session.query(TaskSchedule).all():
        spec = plans.get(sched.task_type)
        if (
            sched.task_type not in supported
            or spec is None
            or sched.trigger_type != spec[0]
            or re.fullmatch(spec[1], sched.business_key or "") is None
        ):
            sched.status = "cancelled"
            sched.next_run_at = None
    for execution in (
        session.query(TaskExecution)
        .filter(
            TaskExecution.status.in_(
                ["pending", "waiting", "pending_confirm", "running"]
            )
        )
        .all()
    ):
        schedule = (
            session.get(TaskSchedule, execution.schedule_id)
            if execution.schedule_id
            else None
        )
        if execution.task_type not in supported or (
            schedule and schedule.status == "cancelled"
        ):
            execution.status = "cancelled"
            execution.next_retry_at = execution.resume_at = None
            execution.finished_at = now
    for event in session.query(NotificationEvent).filter_by(status="pending").all():
        if (
            event.module not in {*MODULE_SETTINGS, "system", "auth", "*"}
            or event.event_type == "course_discovered"
        ):
            event.status = "expired"
            event.next_retry_at = None
    for definition in session.query(TaskDefinition).all():
        if definition.task_type not in supported:
            session.delete(definition)
    for mod in session.query(ModuleConfig).all():
        if mod.module not in MODULE_SETTINGS:
            session.delete(mod)
            continue
        schema = MODULE_SETTINGS[mod.module]
        config_values = {
            k: v for k, v in (mod.config_json or {}).items() if k in schema.model_fields
        }
        if "signin_offset_minutes" in config_values:
            config_values["signin_offset_minutes"] = max(
                -5, min(29, int(config_values["signin_offset_minutes"]))
            )
        mod.config_json = schema.model_validate(config_values).model_dump()
        if changed and mod.module != changed:
            continue
        refresh = {
            "td": (
                "td.sync_overview",
                "体能锻炼状态更新",
                "interval",
                {"interval_seconds": 3600},
            ),
            "classroom": (
                "classroom.sync_today",
                "课堂课程更新",
                "interval",
                {"interval_seconds": 3600},
            ),
            "boya": (
                "boya.sync_courses",
                "博雅课程更新",
                "interval",
                {"interval_seconds": 600},
            ),
            "ygdk": (
                "ygdk.sync_overview",
                "阳光打卡进度更新",
                "daily",
                {"time": mod.config_json.get("reminder_time", "18:00")},
            ),
        }[mod.module]
        task_type, name, trigger, config = refresh
        key = {
            "td": "td:sync_overview",
            "classroom": "classroom:sync_today",
            "boya": "boya:sync_courses",
            "ygdk": "ygdk:sync_overview",
        }[mod.module]
        old = session.query(TaskSchedule).filter_by(business_key=key).first()
        desired_status = "enabled" if mod.enabled else "paused"
        if (
            old is None
            or old.trigger_config_json != config
            or old.status != desired_status
        ):
            old = create_schedule(
                session,
                registry,
                name=name,
                task_type=task_type,
                parameters={},
                trigger_type=trigger,
                trigger_config=config,
                business_key=key,
                enabled=mod.enabled,
                clock=clock,
            )
            if mod.enabled:
                old.next_run_at = now
        elif changed == mod.module and mod.enabled:
            old.next_run_at = now
        # Every changed configuration rebuilds course windows from fresh platform data.
        for sched in (
            session.query(TaskSchedule)
            .filter(TaskSchedule.module == mod.module, TaskSchedule.id != old.id)
            .all()
        ):
            if sched.task_type == "boya.auto_select":
                if not mod.enabled and sched.status == "enabled":
                    sched.status = "paused"
                    sched.next_run_at = None
                elif mod.enabled and sched.status == "paused":
                    sched.status = "enabled"
                    sched.next_run_at = now
                continue
            if (
                not mod.enabled
                or changed == mod.module
                or not mod.config_json.get("auto_sign", True)
            ):
                if sched.status == "enabled":
                    sched.status = "paused"
                    sched.next_run_at = None
        if not mod.enabled or changed == mod.module:
            for ex in (
                session.query(TaskExecution)
                .filter(
                    TaskExecution.module == mod.module,
                    TaskExecution.status.in_(["pending", "waiting", "pending_confirm"]),
                )
                .all()
            ):
                ex.status = "cancelled"
                ex.next_retry_at = ex.resume_at = None
                ex.finished_at = now
            for event in (
                session.query(NotificationEvent)
                .filter_by(module=mod.module, status="pending")
                .all()
            ):
                if not mod.enabled or event.event_type in {
                    "signup_reminder",
                    "signin_reminder",
                    "signout_reminder",
                    "exercise_reminder",
                }:
                    event.status = "expired"
                    event.next_retry_at = None
    session.flush()
