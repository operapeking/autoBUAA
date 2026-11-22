from __future__ import annotations

from typing import Any

from app.api.deps import require_admin
from app.core.time import format_shanghai_iso
from app.db.models import NotificationEvent, TaskExecution, TaskSchedule
from fastapi import APIRouter, Depends, HTTPException, Query

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _serialize_schedule(s: TaskSchedule) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "task_type": s.task_type,
        "module": s.module,
        "business_key": s.business_key,
        "version": s.version,
        "parameters": s.parameters_json,
        "trigger_type": s.trigger_type,
        "trigger_config": s.trigger_config_json,
        "status": s.status,
        "next_run_at": format_shanghai_iso(s.next_run_at),
        "last_run_at": format_shanghai_iso(s.last_run_at),
        "last_result_status": s.last_result_status,
        "last_result_summary": s.last_result_summary,
        "blocking_reason": s.blocking_reason,
        "created_at": format_shanghai_iso(s.created_at),
        "updated_at": format_shanghai_iso(s.updated_at),
    }


def _serialize_execution(
    ex: TaskExecution, include_details: bool = False
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": ex.id,
        "schedule_id": ex.schedule_id,
        "schedule_version": ex.schedule_version,
        "task_type": ex.task_type,
        "module": ex.module,
        "business_key": ex.business_key,
        "scheduled_for": format_shanghai_iso(ex.scheduled_for),
        "deadline_at": format_shanghai_iso(ex.deadline_at),
        "started_at": format_shanghai_iso(ex.started_at),
        "finished_at": format_shanghai_iso(ex.finished_at),
        "status": ex.status,
        "stage": ex.stage,
        "retry_count": ex.retry_count,
        "next_retry_at": format_shanghai_iso(ex.next_retry_at),
        "wait_reason": ex.wait_reason,
        "resume_at": format_shanghai_iso(ex.resume_at),
        "result_summary": ex.result_summary,
        "error_message": ex.error_message,
        "created_at": format_shanghai_iso(ex.created_at),
    }
    if include_details:
        data["result_data"] = ex.result_data_json
        data["stages"] = [
            {
                "stage_name": st.stage_name,
                "status": st.status,
                "payload": st.payload_json,
                "resume_at": format_shanghai_iso(st.resume_at),
                "updated_at": format_shanghai_iso(st.updated_at),
            }
            for st in ex.stages
        ]
    return data


@router.get("/schedules")
def list_schedules(
    module: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    container: Any = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        q = session.query(TaskSchedule)
        if module:
            q = q.filter(TaskSchedule.module == module)
        if status_filter:
            q = q.filter(TaskSchedule.status == status_filter)
        rows = q.order_by(TaskSchedule.id.desc()).all()
        return [_serialize_schedule(r) for r in rows]


@router.get("/executions")
def list_executions(
    module: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    schedule_id: int | None = None,
    date: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    container: Any = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        q = session.query(TaskExecution)
        if module:
            q = q.filter(TaskExecution.module == module)
        if status_filter:
            q = q.filter(TaskExecution.status == status_filter)
        if schedule_id is not None:
            q = q.filter(TaskExecution.schedule_id == schedule_id)
        rows = q.order_by(TaskExecution.id.desc()).limit(limit).all()
        items = [_serialize_execution(r) for r in rows]
        if date:
            items = [i for i in items if (i["scheduled_for"] or "").startswith(date)]
        return items


@router.get("/executions/{execution_id}")
def get_execution_detail(
    execution_id: int,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        ex = session.get(TaskExecution, execution_id)
        if ex is None:
            raise HTTPException(status_code=404, detail="执行记录不存在")
        detail = _serialize_execution(ex, include_details=True)
        events = (
            session.query(NotificationEvent)
            .filter_by(execution_id=execution_id)
            .order_by(NotificationEvent.id.asc())
            .all()
        )
        detail["notifications"] = [
            {
                "id": ev.id,
                "event_key": ev.event_key,
                "event_type": ev.event_type,
                "title": ev.title,
                "body": ev.body,
                "status": ev.status,
                "attempts": ev.attempts,
                "created_at": format_shanghai_iso(ev.created_at),
            }
            for ev in events
        ]
        return detail
