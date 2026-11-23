from __future__ import annotations

from typing import Any

from app.api.deps import require_admin, run_module_sync, upstream_errors
from app.buaa.boya import (
    BoyaClient,
    BoyaCourseData,
    course_matches_campus,
    infer_course_campus,
    parse_boya_dt,
    parse_course,
    resolve_sign_coordinates,
)
from app.container import AppContainer
from app.core.network import read_network_mode
from app.core.time import format_shanghai_iso
from app.db.models import (
    BoyaCourseCache,
    BusinessCacheSnapshot,
    CampusAccount,
    ModuleConfig,
    TaskSchedule,
)
from app.framework.tasks import cancel_schedule, create_schedule
from app.modules.automation import RESERVATION_TASK
from app.modules.services import WINDOW_POLL_SECONDS, format_window_time, sync_boya_all
from fastapi import APIRouter, Depends, HTTPException, Query, status

router = APIRouter(prefix="/boya", tags=["boya"])


def _sync(container: AppContainer):
    return sync_boya_all(
        container.session_factory,
        container.session_mgr,
        container.registry,
        container.clock,
    )


def _client(container: AppContainer) -> BoyaClient:
    return BoyaClient(
        container.session_mgr, read_network_mode(container.session_factory)
    )


def _campus_view(row: BoyaCourseCache) -> BoyaCourseData:
    """Rebuild the fields campus inference reads from a cached course row."""
    course = parse_course(row.raw_json or {"id": row.course_id})
    course.name, course.location, course.campus = row.name, row.location, row.campus
    course.resolved_campus = infer_course_campus(course)
    return course


@router.get("/courses")
def list_courses(container: AppContainer = Depends(require_admin)) -> dict[str, Any]:
    now_sh = container.clock.now_shanghai()
    with container.session_factory() as session:
        rows = (
            session.query(BoyaCourseCache)
            .order_by(BoyaCourseCache.course_id.desc())
            .all()
        )
        stats_snap = session.get(BusinessCacheSnapshot, "boya_statistics")
        completed = {
            schedule.business_key
            for schedule in session.query(TaskSchedule).filter_by(
                module="boya", status="completed", last_result_status="succeeded"
            )
        }
        reservations = {
            s.business_key: s.status
            for s in session.query(TaskSchedule).filter_by(task_type=RESERVATION_TASK)
        }
        account = session.query(CampusAccount).first()
        user_campus = account.campus if account else "北京"
        mod = session.get(ModuleConfig, "boya")
        filter_by_campus = (
            bool(mod.config_json.get("filter_by_campus", True)) if mod else True
        )

    items: list[dict[str, Any]] = []
    for r in rows:
        course_start = parse_boya_dt(r.course_start)
        course_end = parse_boya_dt(r.course_end)
        if not r.selected and course_start and course_start <= now_sh:
            continue
        course = _campus_view(r)
        if (
            filter_by_campus
            and not r.selected
            and not course_matches_campus(course, user_campus)
        ):
            continue
        select_start = parse_boya_dt(r.select_start)
        items.append(
            {
                "course_id": r.course_id,
                "name": r.name,
                "location": r.location,
                "category": r.category,
                "sub_category": r.sub_category,
                "sign_method": r.sign_method,
                "has_autonomous_sign": r.has_autonomous_sign,
                "selected": r.selected,
                "reservation_status": reservations.get(f"boya:reserve:{r.course_id}"),
                "chosen_id": r.chosen_id,
                "course_start": format_shanghai_iso(course_start),
                "course_end": format_shanghai_iso(course_end),
                "is_ended": bool(course_end and course_end <= now_sh),
                "select_start": format_shanghai_iso(select_start),
                "select_end": format_shanghai_iso(parse_boya_dt(r.select_end)),
                "cancel_end": format_shanghai_iso(parse_boya_dt(r.cancel_end)),
                "current_count": r.current_count,
                "max_count": r.max_count,
                "campus": r.campus,
                "resolved_campus": course.resolved_campus,
                "is_preview": bool(select_start and now_sh < select_start),
                "signin_done": r.checkin not in (None, 0)
                or f"boya:sign:{r.course_id}" in completed,
                "signout_done": r.pass_status == 1
                or f"boya:signout:{r.course_id}" in completed,
                "checkin": r.checkin,
                "pass_status": r.pass_status,
                "score": r.score,
                "synced_at": format_shanghai_iso(r.synced_at),
            }
        )
    return {
        "courses": items,
        "statistics": stats_snap.payload_json
        if stats_snap
        else {"valid_count": 0, "categories": []},
        "user_campus": user_campus,
    }


@router.post("/reserve/{course_id}")
async def reserve_course(
    course_id: int, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            course = session.get(BoyaCourseCache, course_id)
            if course is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "课程不存在")
            if course.selected:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "课程已报名")
            start, end = (
                parse_boya_dt(course.select_start),
                parse_boya_dt(course.select_end),
            )
            if (
                not start
                or not end
                or start >= end
                or container.clock.now_shanghai() >= end
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "课程选课时间无效或已截止"
                )
            mod = session.get(ModuleConfig, "boya")
            schedule = create_schedule(
                session,
                container.registry,
                name=f"博雅预约报名: {course.name}",
                task_type=RESERVATION_TASK,
                parameters={"course_id": course_id, "course_name": course.name},
                trigger_type="window",
                trigger_config={
                    "start_at": format_window_time(start),
                    "end_at": format_window_time(end),
                    "poll_interval_seconds": WINDOW_POLL_SECONDS,
                },
                business_key=f"boya:reserve:{course_id}",
                enabled=bool(mod and mod.enabled),
                clock=container.clock,
            )
            session.commit()
            return {"ok": True, "status": schedule.status}


@router.post("/reserve/{course_id}/cancel")
async def cancel_reservation(
    course_id: int, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            schedule = (
                session.query(TaskSchedule)
                .filter_by(business_key=f"boya:reserve:{course_id}")
                .first()
            )
            if schedule:
                cancel_schedule(session, schedule, container.clock.now_utc())
                session.commit()
            return {"ok": True}


@router.post("/sync")
async def sync_courses(
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    courses, chosen, stats = await run_module_sync(
        container, "boya", lambda: _sync(container)
    )
    return {
        "ok": True,
        "courses": len(courses),
        "chosen": len(chosen),
        "valid_count": stats.valid_count,
    }


@router.post("/select/{course_id}")
async def select_course(
    course_id: int, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with upstream_errors():
            res = await _client(container).select_course(course_id)
            await _sync(container)
    return {"ok": True, "data": res.get("data")}


@router.post("/drop/{chosen_id}")
async def drop_course(
    chosen_id: int, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with upstream_errors():
            res = await _client(container).drop_course(chosen_id)
            await _sync(container)
    return {"ok": True, "data": res.get("data")}


@router.post("/sign/{course_id}")
async def sign_course(
    course_id: int,
    sign_type: int = Query(1, ge=1, le=2),
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with upstream_errors():
            await _sync(container)
            with container.session_factory() as session:
                course = session.get(BoyaCourseCache, course_id)
                if (
                    course is None
                    or not course.selected
                    or not course.has_autonomous_sign
                ):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, "课程需要已选且支持自主签到"
                    )
                sign_cfg = dict(course.sign_config_json or {})
            prefix = "sign" if sign_type == 1 else "signOut"
            start = parse_boya_dt(sign_cfg.get(f"{prefix}StartDate"))
            end = parse_boya_dt(sign_cfg.get(f"{prefix}EndDate"))
            if (
                not start
                or not end
                or not (start <= container.clock.now_shanghai() < end)
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "当前时间不在课程签到或签退窗口内"
                )
            lat, lng = resolve_sign_coordinates(sign_cfg)
            res = await _client(container).sign_course(course_id, lat, lng, sign_type)
            await _sync(container)
    return {"ok": True, "data": res.get("data")}
