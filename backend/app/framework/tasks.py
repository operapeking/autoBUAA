from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

from app.core.time import Clock, default_clock
from app.db.models import (
    TaskDefinition,
    TaskSchedule,
    TaskStage,
)
from app.framework.triggers import compute_next_run, validate_trigger
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session


class ParameterValidationError(ValueError):
    """Field-level task parameter error."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        msg = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        super().__init__(msg)


class DuplicateTaskTypeError(RuntimeError):
    pass


class UnknownTaskTypeError(ValueError):
    pass


@dataclass
class PendingNotification:
    event_type: str
    event_key: str
    payload: dict[str, Any]
    expires_at: datetime | None = None
    module: str | None = None


@dataclass
class TaskResult:
    status: Literal["succeeded", "failed", "waiting", "pending_confirm"]
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    retryable: bool = False
    wait_reason: str | None = None
    resume_at: datetime | None = None
    next_stage: str | None = None
    stage_payload: dict[str, Any] | None = None
    complete_window: bool = False
    events: list[PendingNotification] = field(default_factory=list)


@dataclass
class TaskContext:
    execution_id: int
    schedule_id: int | None
    task_type: str
    module: str
    business_key: str | None
    deadline_at: datetime | None
    stage: str
    retry_count: int
    clock: Clock
    db_session_factory: Any
    cipher: Any

    def get_stage_payload(self, stage_name: str | None = None) -> dict[str, Any]:
        target = stage_name or self.stage
        with self.db_session_factory() as session:
            row = (
                session.query(TaskStage)
                .filter_by(execution_id=self.execution_id, stage_name=target)
                .first()
            )
            return dict(row.payload_json) if row else {}

    def save_stage(
        self,
        stage_name: str,
        status: str,
        payload: dict[str, Any],
        resume_at: datetime | None = None,
    ) -> None:
        with self.db_session_factory() as session:
            row = (
                session.query(TaskStage)
                .filter_by(execution_id=self.execution_id, stage_name=stage_name)
                .first()
            )
            if row is None:
                row = TaskStage(
                    execution_id=self.execution_id,
                    stage_name=stage_name,
                    status=status,
                    payload_json=payload,
                    resume_at=resume_at,
                )
                session.add(row)
            else:
                row.status = status
                row.payload_json = payload
                row.resume_at = resume_at
            session.commit()


TaskHandler = Callable[[TaskContext, BaseModel], Awaitable[TaskResult]]


@dataclass
class RegisteredTask:
    task_type: str
    module: str
    display_name: str
    description: str
    parameter_schema: type[BaseModel]
    handler: TaskHandler
    available: bool = True
    unavailable_reason: str | None = None


class TaskRegistry:
    """Central registry for task types, parameter schemas, and handlers."""

    def __init__(self) -> None:
        self._tasks: dict[str, RegisteredTask] = {}

    def register_task(
        self,
        task_type: str,
        parameter_schema: type[BaseModel],
        handler: TaskHandler,
        *,
        module: str,
        display_name: str,
        description: str = "",
        available: bool = True,
        unavailable_reason: str | None = None,
    ) -> None:
        if task_type in self._tasks:
            raise DuplicateTaskTypeError(
                f"Duplicate task_type registration: {task_type}"
            )
        self._tasks[task_type] = RegisteredTask(
            task_type=task_type,
            module=module,
            display_name=display_name,
            description=description,
            parameter_schema=parameter_schema,
            handler=handler,
            available=available,
            unavailable_reason=unavailable_reason,
        )

    def get(self, task_type: str) -> RegisteredTask:
        if task_type not in self._tasks:
            raise UnknownTaskTypeError(f"Unknown task_type: {task_type}")
        return self._tasks[task_type]

    def all(self) -> list[RegisteredTask]:
        return list(self._tasks.values())

    def validate_parameters(
        self, task_type: str, raw_parameters: dict[str, Any]
    ) -> BaseModel:
        reg = self.get(task_type)
        try:
            return reg.parameter_schema.model_validate(raw_parameters or {})
        except ValidationError as exc:
            errors = []
            for err in exc.errors():
                loc = ".".join(str(part) for part in err.get("loc", ())) or "parameters"
                errors.append({"field": loc, "message": err.get("msg", "参数无效")})
            raise ParameterValidationError(errors) from exc

    def sync_to_db(self, session: Session) -> None:
        for reg in self._tasks.values():
            schema_json = reg.parameter_schema.model_json_schema()
            row = session.get(TaskDefinition, reg.task_type)
            if row is None:
                row = TaskDefinition(
                    task_type=reg.task_type,
                    module=reg.module,
                    display_name=reg.display_name,
                    description=reg.description,
                    parameter_schema_json=schema_json,
                    available=reg.available,
                    unavailable_reason=reg.unavailable_reason,
                )
                session.add(row)
            else:
                row.module = reg.module
                row.display_name = reg.display_name
                row.description = reg.description
                row.parameter_schema_json = schema_json
                row.available = reg.available
                row.unavailable_reason = reg.unavailable_reason
        session.commit()


def create_schedule(
    session: Session,
    registry: TaskRegistry,
    *,
    name: str,
    task_type: str,
    parameters: dict[str, Any],
    trigger_type: str,
    trigger_config: dict[str, Any],
    business_key: str | None = None,
    enabled: bool = True,
    clock: Clock | None = None,
) -> TaskSchedule:
    clk = clock or default_clock
    reg = registry.get(task_type)
    if not reg.available:
        raise ValueError(reg.unavailable_reason or f"任务类型 {task_type} 当前不可用")
    validated_params = registry.validate_parameters(task_type, parameters)
    validate_trigger(trigger_type, trigger_config)
    now_utc = clk.now_utc()
    next_run = (
        compute_next_run(trigger_type, trigger_config, now_utc, inclusive=True)
        if enabled
        else None
    )

    if business_key:
        existing = (
            session.query(TaskSchedule).filter_by(business_key=business_key).first()
        )
        if existing is not None:
            if (
                existing.parameters_json == validated_params.model_dump()
                and existing.trigger_type == trigger_type
                and existing.trigger_config_json == trigger_config
                and (
                    existing.status in {"enabled", "completed"}
                    if enabled
                    else existing.status == "paused"
                )
            ):
                return existing
            existing.name = name
            existing.parameters_json = validated_params.model_dump()
            existing.trigger_type = trigger_type
            existing.trigger_config_json = trigger_config
            existing.version += 1
            existing.status = "enabled" if enabled else "paused"
            existing.next_run_at = next_run
            session.flush()
            return existing

    schedule = TaskSchedule(
        name=name,
        task_type=task_type,
        module=reg.module,
        business_key=business_key,
        version=1,
        parameters_json=validated_params.model_dump(),
        trigger_type=trigger_type,
        trigger_config_json=trigger_config,
        status="enabled" if enabled else "paused",
        next_run_at=next_run,
    )
    session.add(schedule)
    session.flush()
    return schedule
