from __future__ import annotations

from typing import Any

from app.api.deps import require_admin, run_module_sync, upstream_errors
from app.buaa.classroom import ClassroomClient
from app.container import AppContainer
from app.core.network import read_network_mode
from app.core.time import format_shanghai_iso
from app.db.models import ClassroomCourseCache
from app.modules.services import (
    classroom_signin_bounds,
    parse_classroom_dt,
    sync_classroom_today,
)
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/classroom", tags=["classroom"])


def _sync(container: AppContainer):
    return sync_classroom_today(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )


@router.get("/courses")
def list_courses(
    date: str | None = None, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    target_date = date or container.clock.now_shanghai().strftime("%Y%m%d")
    with container.session_factory() as session:
        rows = (
            session.query(ClassroomCourseCache)
            .filter_by(course_date=target_date)
            .order_by(ClassroomCourseCache.class_begin_time.asc())
            .all()
        )
        return {
            "date": target_date,
            "courses": [
                {
                    "course_sched_id": r.course_sched_id,
                    "course_name": r.course_name,
                    "class_begin_time": r.class_begin_time,
                    "class_end_time": r.class_end_time,
                    "sign_status": r.sign_status,
                    "signed": r.sign_status == 1,
                    "synced_at": format_shanghai_iso(r.synced_at),
                }
                for r in rows
            ],
        }


@router.post("/sync")
async def sync_courses(
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    courses = await run_module_sync(container, "classroom", lambda: _sync(container))
    return {"ok": True, "count": len(courses)}


@router.post("/signin/{course_sched_id}")
async def manual_signin(
    course_sched_id: str, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    client = ClassroomClient(
        container.session_mgr, read_network_mode(container.session_factory)
    )
    date_str = container.clock.now_shanghai().strftime("%Y%m%d")
    async with container.scheduler.module_lock("classroom"):
        with upstream_errors():
            courses = await client.get_today_courses(date_str)
            course = next(
                (c for c in courses if c.course_sched_id == course_sched_id), None
            )
            if course is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "今日课程不存在")
            if not course.signed:
                begin = parse_classroom_dt(course.class_begin_time, date_str)
                end = parse_classroom_dt(course.class_end_time, date_str)
                earliest, latest = (
                    classroom_signin_bounds(begin, end)
                    if begin and end
                    else (None, None)
                )
                if (
                    not earliest
                    or not latest
                    or not (earliest <= container.clock.now_shanghai() < latest)
                ):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, "当前时间不在课程签到窗口内"
                    )
            res = await client.sign_in(course_sched_id, date_str)
            await _sync(container)
    return {
        "ok": res.success,
        "already_signed": res.already_signed,
        "message": res.message,
    }
