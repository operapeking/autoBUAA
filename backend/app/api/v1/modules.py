from __future__ import annotations

from typing import Any

from app.api.deps import require_admin
from app.buaa.boya import (
    BoyaClient,
    course_matches_campus,
    infer_course_campus,
    normalize_sub_category,
    parse_boya_dt,
    parse_course,
    resolve_sign_coordinates,
)
from app.buaa.classroom import ClassroomClient
from app.buaa.exceptions import BuaaError
from app.core.network import get_unified_network_mode
from app.core.time import format_shanghai_iso
from app.db.models import (
    BoyaCourseCache,
    BusinessCacheSnapshot,
    CampusAccount,
    ClassroomCourseCache,
    ModuleConfig,
    TaskExecution,
    TaskSchedule,
)
from app.framework.tasks import create_schedule
from app.modules.services import (
    classroom_signin_bounds,
    parse_classroom_dt,
    sync_boya_all,
    sync_classroom_today,
    sync_td_progress,
    sync_ygdk_progress,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status

router = APIRouter(tags=["modules"])


# ---------- Classroom ----------
@router.get("/classroom/courses")
def list_classroom_courses(
    date: str | None = None,
    container: Any = Depends(require_admin),
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


@router.post("/classroom/sync")
async def trigger_classroom_sync(
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    async with container.scheduler.module_lock("classroom"):
        try:
            courses = await sync_classroom_today(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
            )
            return {"ok": True, "count": len(courses)}
        except BuaaError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc


@router.post("/classroom/signin/{course_sched_id}")
async def manual_classroom_signin(
    course_sched_id: str,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    async with container.scheduler.module_lock("classroom"):
        with container.session_factory() as session:
            mode = get_unified_network_mode(session)
        client = ClassroomClient(container.session_mgr, network_mode=mode)
        date_str = container.clock.now_shanghai().strftime("%Y%m%d")
        try:
            courses = await client.get_today_courses(date_str)
            course = next(
                (c for c in courses if c.course_sched_id == course_sched_id), None
            )
            if course is None:
                raise HTTPException(status_code=404, detail="今日课程不存在")
            begin = parse_classroom_dt(course.class_begin_time, date_str)
            end = parse_classroom_dt(course.class_end_time, date_str)
            now = container.clock.now_shanghai()
            if not course.signed and (
                not begin
                or not end
                or not (
                    classroom_signin_bounds(begin, end)[0]
                    <= now
                    < classroom_signin_bounds(begin, end)[1]
                )
            ):
                raise HTTPException(
                    status_code=400, detail="当前时间不在课程签到窗口内"
                )
            res = await client.sign_in(course_sched_id, date_str)
            await sync_classroom_today(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
                auto_schedule=True,
            )
            return {
                "ok": res.success,
                "already_signed": res.already_signed,
                "message": res.message,
            }
        except BuaaError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc


# ---------- TD ----------
@router.get("/td/overview")
def get_td_overview(container: Any = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        snapshot = session.get(BusinessCacheSnapshot, "td_overview")
        account = session.query(CampusAccount).first()
        if (
            snapshot is None
            or not account
            or snapshot.payload_json.get("student_id") != account.student_id
        ):
            return {"synced": False, "overview": None, "synced_at": None}
        config = session.get(ModuleConfig, "td")
        overview = {k: v for k, v in snapshot.payload_json.items() if k != "student_id"}
        overview["term_target"] = (config.config_json if config else {}).get(
            "term_target", 32
        )
        return {
            "synced": True,
            "overview": overview,
            "synced_at": format_shanghai_iso(snapshot.synced_at),
        }


@router.post("/td/sync")
async def trigger_td_sync(container: Any = Depends(require_admin)) -> dict[str, Any]:
    async with container.scheduler.module_lock("td"):
        try:
            overview = await sync_td_progress(
                container.session_factory, container.session_mgr, container.clock
            )
            return {"ok": True, "term_count": overview.term_count}
        except BuaaError as exc:
            with container.session_factory() as session:
                mod = session.get(ModuleConfig, "td")
                if mod:
                    mod.health_status = "error"
                    mod.last_error = str(exc)
                    mod.last_checked_at = container.clock.now_utc()
                    session.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- YGDK ----------
@router.get("/ygdk/overview")
def get_ygdk_overview(container: Any = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        snap = session.get(BusinessCacheSnapshot, "ygdk_overview")
        if snap is None:
            return {"synced": False, "overview": None, "synced_at": None}
        return {
            "synced": True,
            "overview": snap.payload_json,
            "synced_at": format_shanghai_iso(snap.synced_at),
        }


@router.post("/ygdk/sync")
async def trigger_ygdk_sync(container: Any = Depends(require_admin)) -> dict[str, Any]:
    async with container.scheduler.module_lock("ygdk"):
        try:
            overview = await sync_ygdk_progress(
                container.session_factory, container.session_mgr, container.clock
            )
            return {"ok": True, "term_count": overview.term_count}
        except BuaaError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc


# ---------- Boya ----------
@router.get("/boya/courses")
def list_boya_courses(
    only_selected: bool = False,
    sub_category: str | None = None,
    campus: str | None = None,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    now_sh = container.clock.now_shanghai()
    with container.session_factory() as session:
        rows = (
            session.query(BoyaCourseCache)
            .order_by(BoyaCourseCache.course_id.desc())
            .all()
        )
        stats_snap = session.get(BusinessCacheSnapshot, "boya_statistics")
        stats_data = (
            stats_snap.payload_json
            if stats_snap
            else {"valid_count": 0, "categories": []}
        )
        completed = {
            schedule.business_key
            for schedule in session.query(TaskSchedule)
            .filter_by(
                module="boya", status="completed", last_result_status="succeeded"
            )
            .all()
        }
        account = session.query(CampusAccount).first()
        user_campus = account.campus if account else "北京"
        mod = session.get(ModuleConfig, "boya")
        filter_by_campus = (
            bool((mod.config_json or {}).get("filter_by_campus", True)) if mod else True
        )
        reservations = {
            s.business_key: s.status
            for s in session.query(TaskSchedule)
            .filter_by(task_type="boya.auto_select")
            .all()
        }

    items: list[dict[str, Any]] = []
    for r in rows:
        course_start = parse_boya_dt(r.course_start)
        course_end = parse_boya_dt(r.course_end)
        if not r.selected and course_start and course_start <= now_sh:
            continue
        if only_selected and not r.selected:
            continue
        if sub_category and normalize_sub_category(
            r.sub_category
        ) != normalize_sub_category(sub_category):
            continue
        parsed = parse_course(r.raw_json or {"id": r.course_id, "courseName": r.name})
        parsed.name = r.name
        parsed.location = r.location
        parsed.campus = r.campus
        parsed.selected = r.selected
        parsed.sub_category = r.sub_category
        parsed.has_autonomous_sign = r.has_autonomous_sign
        parsed.sign_method = r.sign_method
        resolved_campus = infer_course_campus(parsed)
        parsed.resolved_campus = resolved_campus
        if (
            filter_by_campus
            and not r.selected
            and not course_matches_campus(parsed, user_campus)
        ):
            continue
        if campus and resolved_campus != campus:
            continue
        s_start = parse_boya_dt(r.select_start)
        is_preview = bool(s_start and now_sh < s_start)
        items.append(
            {
                "course_id": r.course_id,
                "name": r.name,
                "location": r.location,
                "category": r.category,
                "sub_category": r.sub_category,
                "sign_method": "自主签到" if r.has_autonomous_sign else "常规签到",
                "has_autonomous_sign": r.has_autonomous_sign,
                "selected": r.selected,
                "reservation_status": reservations.get(f"boya:reserve:{r.course_id}"),
                "chosen_id": r.chosen_id,
                "course_start": format_shanghai_iso(course_start),
                "course_end": format_shanghai_iso(course_end),
                "is_ended": bool(course_end and course_end <= now_sh),
                "select_start": format_shanghai_iso(parse_boya_dt(r.select_start)),
                "select_end": format_shanghai_iso(parse_boya_dt(r.select_end)),
                "cancel_end": format_shanghai_iso(parse_boya_dt(r.cancel_end)),
                "current_count": r.current_count,
                "max_count": r.max_count,
                "campus": r.campus,
                "resolved_campus": resolved_campus,
                "is_preview": is_preview,
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
        "statistics": stats_data,
        "user_campus": user_campus,
    }


@router.post("/boya/reserve/{course_id}")
async def reserve_boya_course(
    course_id: int, container: Any = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            course = session.get(BoyaCourseCache, course_id)
            if course is None:
                raise HTTPException(status_code=404, detail="课程不存在")
            if course.selected:
                raise HTTPException(status_code=400, detail="课程已报名")
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
                raise HTTPException(status_code=400, detail="课程选课时间无效或已截止")
            mod = session.get(ModuleConfig, "boya")
            schedule = create_schedule(
                session,
                container.registry,
                name=f"博雅预约报名: {course.name}",
                task_type="boya.auto_select",
                parameters={"course_id": course_id, "course_name": course.name},
                trigger_type="window",
                trigger_config={
                    "start_at": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_at": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "poll_interval_seconds": 30,
                },
                business_key=f"boya:reserve:{course_id}",
                enabled=bool(mod and mod.enabled),
                clock=container.clock,
            )
            session.commit()
            return {"ok": True, "status": schedule.status}


@router.post("/boya/reserve/{course_id}/cancel")
async def cancel_boya_reservation(
    course_id: int, container: Any = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            schedule = (
                session.query(TaskSchedule)
                .filter_by(business_key=f"boya:reserve:{course_id}")
                .first()
            )
            if schedule:
                schedule.status = "cancelled"
                schedule.next_run_at = None
                for execution in (
                    session.query(TaskExecution)
                    .filter(
                        TaskExecution.schedule_id == schedule.id,
                        TaskExecution.status.in_(
                            ["pending", "waiting", "pending_confirm"]
                        ),
                    )
                    .all()
                ):
                    execution.status = "cancelled"
                    execution.next_retry_at = execution.resume_at = None
                    execution.finished_at = container.clock.now_utc()
                session.commit()
            return {"ok": True}


@router.post("/boya/sync")
async def trigger_boya_sync(container: Any = Depends(require_admin)) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        try:
            courses, chosen, stats = await sync_boya_all(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
            )
            return {
                "ok": True,
                "courses": len(courses),
                "chosen": len(chosen),
                "valid_count": stats.valid_count,
            }
        except BuaaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/boya/select/{course_id}")
async def manual_boya_select(
    course_id: int, container: Any = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            mode = get_unified_network_mode(session)
        client = BoyaClient(container.session_mgr, network_mode=mode)
        try:
            res = await client.select_course(course_id)
            await sync_boya_all(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
                auto_schedule=True,
            )
            return {"ok": True, "data": res.get("data")}
        except BuaaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/boya/drop/{chosen_id}")
async def manual_boya_drop(
    chosen_id: int, container: Any = Depends(require_admin)
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        with container.session_factory() as session:
            mode = get_unified_network_mode(session)
        client = BoyaClient(container.session_mgr, network_mode=mode)
        try:
            res = await client.drop_course(chosen_id)
            await sync_boya_all(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
                auto_schedule=True,
            )
            return {"ok": True, "data": res.get("data")}
        except BuaaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/boya/sign/{course_id}")
async def manual_boya_sign(
    course_id: int,
    sign_type: int = Query(1, ge=1, le=2),
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    async with container.scheduler.module_lock("boya"):
        try:
            await sync_boya_all(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
            )
            with container.session_factory() as session:
                mode = get_unified_network_mode(session)
                course = session.get(BoyaCourseCache, course_id)
                if (
                    course is None
                    or not course.selected
                    or not course.has_autonomous_sign
                ):
                    raise HTTPException(
                        status_code=400, detail="课程需要已选且支持自主签到"
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
                    status_code=400, detail="当前时间不在课程签到或签退窗口内"
                )
            client = BoyaClient(container.session_mgr, network_mode=mode)
            lat, lng = resolve_sign_coordinates(sign_cfg)
            res = await client.sign_course(course_id, lat, lng, sign_type)
            await sync_boya_all(
                container.session_factory,
                container.session_mgr,
                container.registry,
                container.clock,
            )
            return {"ok": True, "data": res.get("data")}
        except BuaaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
