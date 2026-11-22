from __future__ import annotations

from datetime import timedelta

from app.buaa.boya import (
    BoyaClient,
    parse_boya_dt,
    resolve_semester_window,
    resolve_sign_coordinates,
)
from app.buaa.classroom import ClassroomClient
from app.buaa.exceptions import (
    BuaaError,
    BusinessTerminalError,
    CaptchaRequiredError,
    OutcomeUnknownError,
)
from app.buaa.sso import BuaaSessionManager
from app.core.network import get_unified_network_mode
from app.core.time import UTC_TZ
from app.db.models import (
    BoyaCourseCache,
    ClassroomCourseCache,
    ModuleConfig,
    NotificationEvent,
)
from app.framework.tasks import (
    PendingNotification,
    TaskContext,
    TaskRegistry,
    TaskResult,
)
from app.modules.automation import request_module_refresh
from pydantic import BaseModel, Field

from .services import (
    classroom_signin_bounds,
    parse_classroom_dt,
    sync_boya_all,
    sync_classroom_today,
    sync_td_progress,
    sync_ygdk_progress,
)


class EmptyParams(BaseModel):
    pass


class ClassroomSigninParams(BaseModel):
    course_sched_id: str = Field(..., min_length=1, description="课程安排 ID")
    course_name: str = Field(default="", description="课程名称")
    course_date: str = Field(default="", description="课程日期 YYYYMMDD")


class BoyaCourseActionParams(BaseModel):
    course_id: int = Field(..., gt=0, description="博雅课程 ID")
    course_name: str = Field(default="", description="课程名称")


def _module_network_mode(ctx: TaskContext, _module: str) -> str:
    with ctx.db_session_factory() as session:
        return get_unified_network_mode(session)


def _mark_module_error(ctx: TaskContext, module: str, message: str) -> None:
    with ctx.db_session_factory() as session:
        row = session.get(ModuleConfig, module)
        if row:
            row.health_status = "error"
            row.last_error = message
            row.last_checked_at = ctx.clock.now_utc()
            session.commit()


def _record_classroom_signin(
    ctx: TaskContext, params: ClassroomSigninParams, date_str: str
) -> None:
    with ctx.db_session_factory() as session:
        course = (
            session.query(ClassroomCourseCache)
            .filter_by(course_date=date_str, course_sched_id=params.course_sched_id)
            .first()
        )
        if course:
            course.sign_status = 1
        reminder = (
            session.query(NotificationEvent)
            .filter_by(
                event_key=f"classroom:reminder:{params.course_sched_id}:{date_str}",
                status="pending",
            )
            .first()
        )
        if reminder:
            reminder.status = "expired"
            reminder.next_retry_at = None
        session.commit()


def register_all_tasks(
    registry: TaskRegistry,
    session_mgr: BuaaSessionManager,
) -> None:
    """Register all framework and BUAA business tasks at application startup."""

    async def handle_classroom_sync(
        ctx: TaskContext, _params: EmptyParams
    ) -> TaskResult:
        try:
            courses = await sync_classroom_today(
                ctx.db_session_factory, session_mgr, registry, ctx.clock
            )
            return TaskResult(
                status="succeeded",
                summary=f"课堂课表已同步: 当日 {len(courses)} 门课程",
                data={"count": len(courses)},
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BuaaError as exc:
            _mark_module_error(ctx, "classroom", str(exc))
            return TaskResult(
                status="failed",
                summary=f"课堂课表同步失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    async def handle_classroom_signin(
        ctx: TaskContext, params: ClassroomSigninParams
    ) -> TaskResult:
        mode = _module_network_mode(ctx, "classroom")
        client = ClassroomClient(session_mgr, network_mode=mode)
        date_str = params.course_date or ctx.clock.now_shanghai().strftime("%Y%m%d")

        # Enforce hard window [class_begin - 5 min, min(class_begin + 30 min, class_end))
        with ctx.db_session_factory() as session:
            cached_course = (
                session.query(ClassroomCourseCache)
                .filter_by(course_date=date_str, course_sched_id=params.course_sched_id)
                .first()
            )
            begin_str = cached_course.class_begin_time if cached_course else ""
            end_str = cached_course.class_end_time if cached_course else ""
        begin_sh = parse_classroom_dt(begin_str, date_str)
        end_sh = parse_classroom_dt(end_str, date_str)
        if not begin_sh or not end_sh:
            return TaskResult(
                status="failed", summary="课程缺少有效上课时间", complete_window=True
            )
        if begin_sh and end_sh:
            now_sh = ctx.clock.now_shanghai()
            earliest_sh, latest_sh = classroom_signin_bounds(begin_sh, end_sh)
            if now_sh < earliest_sh:
                return TaskResult(
                    status="waiting",
                    summary=f"签到时间未到（{earliest_sh.strftime('%H:%M')} 开始）",
                    wait_reason="before_classroom_signin_window",
                    resume_at=earliest_sh.astimezone(UTC_TZ),
                )
            if now_sh >= latest_sh:
                return TaskResult(
                    status="failed",
                    summary=f"已超过签到截止时间（{latest_sh.strftime('%H:%M')}）",
                    error_message="after_classroom_signin_window",
                    retryable=False,
                    complete_window=True,
                )

        # If recovering from pending_confirm, query platform state first
        if ctx.stage == "pending_confirm":
            try:
                courses = await client.get_today_courses(date_str)
                found = next(
                    (c for c in courses if c.course_sched_id == params.course_sched_id),
                    None,
                )
                if found and found.signed:
                    _record_classroom_signin(ctx, params, date_str)
                    return TaskResult(
                        status="succeeded",
                        summary=f"课堂签到成功: {found.course_name}（平台已确认）",
                        complete_window=True,
                        events=[
                            PendingNotification(
                                event_type="task_succeeded",
                                event_key=f"classroom:signed:{params.course_sched_id}:{date_str}",
                                payload={
                                    "title": f"课堂签到成功: {found.course_name}",
                                    "course_name": found.course_name,
                                    "summary": "平台记录已确认签到",
                                },
                            )
                        ],
                    )
            except BuaaError as exc:
                return TaskResult(
                    status="pending_confirm",
                    summary=f"确认签到状态失败，等待重试: {exc}",
                    wait_reason=str(exc),
                    next_stage="pending_confirm",
                )

        try:
            res = await client.sign_in(params.course_sched_id, date_str)
            if res.success:
                _record_classroom_signin(ctx, params, date_str)
                label = "此前已签到" if res.already_signed else "课堂签到成功"
                events = (
                    []
                    if res.already_signed
                    else [
                        PendingNotification(
                            event_type="task_succeeded",
                            event_key=f"classroom:signed:{params.course_sched_id}:{date_str}",
                            payload={
                                "title": f"课堂签到成功: {params.course_name or params.course_sched_id}",
                                "course_name": params.course_name
                                or params.course_sched_id,
                                "summary": res.message,
                            },
                        )
                    ]
                )
                return TaskResult(
                    status="succeeded",
                    summary=f"{label}: {params.course_name or params.course_sched_id}",
                    complete_window=True,
                    events=events,
                )
            return TaskResult(
                status="waiting",
                summary=res.message,
                wait_reason="等待平台签到开放",
                resume_at=ctx.clock.now_utc() + timedelta(seconds=30),
            )
        except OutcomeUnknownError as exc:
            return TaskResult(
                status="pending_confirm",
                summary=str(exc),
                wait_reason=str(exc),
                next_stage="pending_confirm",
                events=[
                    PendingNotification(
                        event_type="outcome_unknown",
                        event_key=f"classroom:unknown:{ctx.execution_id}",
                        payload={
                            "title": f"课堂签到结果待确认: {params.course_name}",
                            "course_name": params.course_name,
                            "reason": str(exc),
                        },
                    )
                ],
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BusinessTerminalError as exc:
            return TaskResult(
                status="failed",
                summary=str(exc),
                error_message=str(exc),
                retryable=False,
                complete_window=True,
            )
        except BuaaError as exc:
            return TaskResult(
                status="failed",
                summary=f"课堂签到失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    async def handle_ygdk_sync(ctx: TaskContext, _params: EmptyParams) -> TaskResult:
        try:
            overview = await sync_ygdk_progress(
                ctx.db_session_factory, session_mgr, ctx.clock
            )
            return TaskResult(
                status="succeeded",
                summary=f"阳光打卡已同步: 本学期累计 {overview.term_count} 次",
                data={"term_count": overview.term_count},
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BuaaError as exc:
            _mark_module_error(ctx, "ygdk", str(exc))
            return TaskResult(
                status="failed",
                summary=f"阳光打卡同步失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    async def handle_boya_sync(ctx: TaskContext, _params: EmptyParams) -> TaskResult:
        try:
            courses, chosen, stats = await sync_boya_all(
                ctx.db_session_factory, session_mgr, registry, ctx.clock
            )
            return TaskResult(
                status="succeeded",
                summary=f"博雅课程已同步: 可选 {len(courses)} 门，已选 {len(chosen)} 门，有效次数 {stats.valid_count}",
                data={
                    "courses": len(courses),
                    "chosen": len(chosen),
                    "valid_count": stats.valid_count,
                },
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BuaaError as exc:
            _mark_module_error(ctx, "boya", str(exc))
            return TaskResult(
                status="failed",
                summary=f"博雅课程同步失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    async def handle_boya_select(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        client = BoyaClient(session_mgr, network_mode=_module_network_mode(ctx, "boya"))
        now = ctx.clock.now_shanghai()

        def wait(message: str) -> TaskResult:
            return TaskResult(
                status="waiting",
                summary=message,
                wait_reason=message,
                resume_at=ctx.clock.now_utc() + timedelta(seconds=30),
            )

        def success() -> TaskResult:
            with ctx.db_session_factory() as session:
                course = session.get(BoyaCourseCache, params.course_id)
                if course:
                    course.selected = True
                request_module_refresh(session, "boya", ctx.clock)
                session.commit()
            return TaskResult(
                status="succeeded",
                summary=f"博雅报名成功: {params.course_name}",
                complete_window=True,
                events=[
                    PendingNotification(
                        event_type="task_succeeded",
                        event_key=f"boya:select_ok:{ctx.schedule_id}",
                        payload={
                            "title": f"博雅报名成功: {params.course_name}",
                            "course_name": params.course_name,
                            "message": f"已报名《{params.course_name}》",
                        },
                    )
                ],
            )

        try:
            config = await client.get_all_config()
            chosen = await client.query_chosen_courses(
                *resolve_semester_window(config, now)
            )
            if any(c.course_id == params.course_id for c in chosen):
                return success()
            if ctx.stage == "confirm" or ctx.get_stage_payload("select_request").get(
                "pending"
            ):
                return wait(f"等待平台确认报名结果: {params.course_name}")
            course = await client.query_course_detail(params.course_id)
            start, end = (
                parse_boya_dt(course.select_start),
                parse_boya_dt(course.select_end),
            )
            if not start or not end or now >= end:
                return TaskResult(
                    status="failed",
                    summary="选课窗口无效或已截止",
                    complete_window=True,
                )
            if now < start:
                return TaskResult(
                    status="waiting",
                    summary="等待选课开始",
                    resume_at=start.astimezone(UTC_TZ),
                )
            if (
                course.max_count is not None
                and course.current_count is not None
                and course.current_count >= course.max_count
            ):
                return wait(f"名额已满，等待空余名额: {params.course_name}")
            # Persist the confirmation stage before sending a non-idempotent request.
            ctx.save_stage("select_request", "pending", {"pending": True})
            await client.select_course(params.course_id)
            return success()
        except OutcomeUnknownError as exc:
            return TaskResult(
                status="pending_confirm",
                summary=f"报名结果待确认: {params.course_name}",
                error_message=str(exc),
                next_stage="confirm",
            )
        except BusinessTerminalError as exc:
            ctx.save_stage("select_request", "failed", {"pending": False})
            if any(
                word in str(exc)
                for word in (
                    "容量已满",
                    "人数已满",
                    "名额已满",
                    "未到选课时间",
                    "不在选课时间",
                    "已选",
                    "重复选择",
                )
            ):
                return wait(str(exc))
            return TaskResult(
                status="failed", summary=f"博雅报名失败: {exc}", complete_window=True
            )
        except BuaaError as exc:
            if ctx.get_stage_payload("select_request").get("pending"):
                return TaskResult(
                    status="pending_confirm",
                    summary=f"报名结果待确认: {params.course_name}",
                    error_message=str(exc),
                    next_stage="confirm",
                )
            return wait(f"报名出错，等待重试: {exc}")

    async def _handle_boya_sign_action(
        ctx: TaskContext, params: BoyaCourseActionParams, sign_type: int
    ) -> TaskResult:
        label = "签到" if sign_type == 1 else "签退"
        mode = _module_network_mode(ctx, "boya")
        client = BoyaClient(session_mgr, network_mode=mode)

        try:
            all_cfg = await client.get_all_config()
            sem_s, sem_e = resolve_semester_window(all_cfg, ctx.clock.now_shanghai())
            chosen = await client.query_chosen_courses(sem_s, sem_e)
            course = next((c for c in chosen if c.course_id == params.course_id), None)
            if course is None:
                return TaskResult(
                    status="failed",
                    summary=f"未在已选课程中找到: {params.course_name or params.course_id}",
                    error_message="not_selected",
                    retryable=False,
                    complete_window=True,
                )

            if sign_type == 1 and course.checkin not in (None, 0):
                return TaskResult(
                    status="succeeded",
                    summary=f"博雅已签到: {course.name}",
                    complete_window=True,
                )
            if sign_type == 2 and course.pass_status == 1:
                return TaskResult(
                    status="succeeded",
                    summary=f"博雅已通过考核: {course.name}",
                    complete_window=True,
                )

            if ctx.stage == "pending_confirm":
                return TaskResult(
                    status="failed",
                    summary=f"平台状态显示博雅{label}尚未完成，继续尝试",
                    error_message="pending_confirm_not_yet_done",
                    retryable=True,
                )

            sign_cfg = course.sign_config
            if not sign_cfg:
                detail = await client.query_course_detail(params.course_id)
                sign_cfg = detail.sign_config

            prefix = "sign" if sign_type == 1 else "signOut"
            start = parse_boya_dt(sign_cfg.get(f"{prefix}StartDate"))
            end = parse_boya_dt(sign_cfg.get(f"{prefix}EndDate"))
            now = ctx.clock.now_shanghai()
            if not start or not end or now >= end:
                return TaskResult(
                    status="failed",
                    summary=f"博雅{label}窗口无效或已结束",
                    complete_window=True,
                )
            if now < start:
                return TaskResult(
                    status="waiting",
                    summary=f"博雅{label}时间未到",
                    resume_at=start.astimezone(UTC_TZ),
                )

            lat, lng = resolve_sign_coordinates(sign_cfg)
            await client.sign_course(params.course_id, lat, lng, sign_type)
            with ctx.db_session_factory() as session:
                reminder_prefix = "signin_rem" if sign_type == 1 else "signout_rem"
                for reminder in (
                    session.query(NotificationEvent)
                    .filter(
                        NotificationEvent.event_key.like(
                            f"boya:{reminder_prefix}:{params.course_id}:%"
                        ),
                        NotificationEvent.status == "pending",
                    )
                    .all()
                ):
                    reminder.status = "expired"
                    reminder.next_retry_at = None
                session.commit()
            return TaskResult(
                status="succeeded",
                summary=f"博雅{label}成功: {course.name}",
                complete_window=True,
                events=[
                    PendingNotification(
                        event_type="task_succeeded",
                        event_key=f"boya:sign_ok:{sign_type}:{params.course_id}",
                        payload={
                            "title": f"博雅{label}成功: {course.name}",
                            "course_name": course.name,
                            "summary": f"已完成博雅{label}",
                        },
                    )
                ],
            )
        except OutcomeUnknownError as exc:
            return TaskResult(
                status="pending_confirm",
                summary=str(exc),
                wait_reason=str(exc),
                next_stage="pending_confirm",
                events=[
                    PendingNotification(
                        event_type="outcome_unknown",
                        event_key=f"boya:sign_unknown:{sign_type}:{ctx.execution_id}",
                        payload={
                            "title": f"博雅{label}结果待确认: {params.course_name}",
                            "course_name": params.course_name,
                            "reason": str(exc),
                        },
                    )
                ],
            )
        except BusinessTerminalError as exc:
            return TaskResult(
                status="failed",
                summary=f"博雅{label}失败: {exc}",
                error_message=str(exc),
                retryable=False,
                complete_window=True,
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BuaaError as exc:
            return TaskResult(
                status="failed",
                summary=f"博雅{label}失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    async def handle_boya_signin(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        return await _handle_boya_sign_action(ctx, params, sign_type=1)

    async def handle_boya_signout(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        return await _handle_boya_sign_action(ctx, params, sign_type=2)

    async def handle_td_sync(ctx: TaskContext, _params: EmptyParams) -> TaskResult:
        try:
            overview = await sync_td_progress(
                ctx.db_session_factory, session_mgr, ctx.clock
            )
            count = overview.term_count
            return TaskResult(
                status="succeeded",
                summary=f"体能锻炼已同步: 本学期累计 {count:g} 次"
                if count is not None
                else "体能锻炼已同步: 本学期暂无记录",
                data={"term_count": count},
            )
        except CaptchaRequiredError as exc:
            return TaskResult(
                status="waiting",
                summary="统一认证需要验证码，已暂停",
                wait_reason=str(exc),
                resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
            )
        except BuaaError as exc:
            _mark_module_error(ctx, "td", str(exc))
            return TaskResult(
                status="failed",
                summary=f"体能锻炼同步失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    registry.register_task(
        "td.sync_overview",
        EmptyParams,
        handle_td_sync,
        module="td",
        display_name="体能锻炼状态更新",
        description="查询健康云当前学期的体能锻炼次数",
    )
    registry.register_task(
        "classroom.sync_today",
        EmptyParams,
        handle_classroom_sync,
        module="classroom",
        display_name="课堂课表刷新与签到发现",
        description="查询当日课堂安排，生成开课前 5 分钟（可配置，限课前 5 分钟至上课后 30 分钟内）的自动签到任务与提前提醒",
    )
    registry.register_task(
        "classroom.auto_signin",
        ClassroomSigninParams,
        handle_classroom_signin,
        module="classroom",
        display_name="课堂自动签到",
        description="在课程签到时间窗口内轮询课程状态并自动完成课堂签到",
    )
    registry.register_task(
        "ygdk.sync_overview",
        EmptyParams,
        handle_ygdk_sync,
        module="ygdk",
        display_name="阳光打卡概览与记录刷新",
        description="查询当前学期、项目列表、累计次数与打卡记录",
    )
    registry.register_task(
        "boya.sync_courses",
        EmptyParams,
        handle_boya_sync,
        module="boya",
        display_name="博雅课程刷新与选课/签到发现",
        description="刷新博雅课程、已选课程与类别次数统计，安排自主签到及签退任务",
    )
    registry.register_task(
        "boya.auto_select",
        BoyaCourseActionParams,
        handle_boya_select,
        module="boya",
        display_name="博雅预约报名",
        description="在选课时间内自动报名，满额后每 30 秒查询，成功后通知",
    )
    registry.register_task(
        "boya.auto_signin",
        BoyaCourseActionParams,
        handle_boya_signin,
        module="boya",
        display_name="博雅自主签到",
        description="在已选博雅课程签到窗口内自动提交位置签到",
    )
    registry.register_task(
        "boya.auto_signout",
        BoyaCourseActionParams,
        handle_boya_signout,
        module="boya",
        display_name="博雅自主签退",
        description="在已选博雅课程签退窗口内自动提交位置签退",
    )
