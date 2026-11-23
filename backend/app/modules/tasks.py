from __future__ import annotations

from datetime import timedelta
from typing import Any, Awaitable, Callable

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
from app.core.network import read_network_mode
from app.core.time import UTC_TZ
from app.db.models import BoyaCourseCache, ClassroomCourseCache, NotificationEvent
from app.framework.tasks import (
    PendingNotification,
    TaskContext,
    TaskRegistry,
    TaskResult,
)
from app.modules.automation import request_module_refresh
from app.notifications.service import expire_events
from pydantic import BaseModel, Field

from .services import (
    classroom_signin_bounds,
    mark_module_error,
    parse_classroom_dt,
    sync_boya_all,
    sync_classroom_today,
    sync_td_progress,
    sync_ygdk_progress,
)

# Stage entered after a write whose outcome is unknown; the next run checks platform state.
CONFIRM_STAGE = "confirm"

# Upstream messages meaning "not possible right now"; reservations keep polling on these.
BOYA_RETRY_LATER_WORDS = (
    "容量已满",
    "人数已满",
    "名额已满",
    "未到选课时间",
    "不在选课时间",
    "已选",
    "重复选择",
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


def _captcha_wait(ctx: TaskContext, exc: CaptchaRequiredError) -> TaskResult:
    return TaskResult(
        status="waiting",
        summary="统一认证需要验证码，已暂停",
        wait_reason=str(exc),
        resume_at=ctx.clock.now_utc() + timedelta(seconds=60),
    )


def _wait(ctx: TaskContext, message: str, seconds: int = 30) -> TaskResult:
    return TaskResult(
        status="waiting",
        summary=message,
        wait_reason=message,
        resume_at=ctx.clock.now_utc() + timedelta(seconds=seconds),
    )


def _success_event(
    event_key: str, title: str, course_name: str, **extra: str
) -> PendingNotification:
    return PendingNotification(
        event_type="task_succeeded",
        event_key=event_key,
        payload={"title": title, "course_name": course_name, **extra},
    )


def _unknown_outcome(
    ctx: TaskContext,
    exc: OutcomeUnknownError,
    event_key: str,
    title: str,
    course_name: str,
) -> TaskResult:
    return TaskResult(
        status="pending_confirm",
        summary=str(exc),
        wait_reason=str(exc),
        next_stage=CONFIRM_STAGE,
        events=[
            PendingNotification(
                event_type="outcome_unknown",
                event_key=event_key,
                payload={
                    "title": title,
                    "course_name": course_name,
                    "reason": str(exc),
                },
            )
        ],
    )


def _sync_handler(
    module: str,
    label: str,
    run: Callable[[TaskContext], Awaitable[Any]],
    summarize: Callable[[Any], TaskResult],
) -> Callable[[TaskContext, EmptyParams], Awaitable[TaskResult]]:
    """Wrap a module refresh with the shared captcha pause and retryable failure handling."""

    async def handler(ctx: TaskContext, _params: EmptyParams) -> TaskResult:
        try:
            return summarize(await run(ctx))
        except CaptchaRequiredError as exc:
            return _captcha_wait(ctx, exc)
        except BuaaError as exc:
            mark_module_error(ctx.db_session_factory, module, str(exc), ctx.clock)
            return TaskResult(
                status="failed",
                summary=f"{label}同步失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

    return handler


def _record_classroom_signin(
    ctx: TaskContext, course_sched_id: str, date_str: str
) -> None:
    with ctx.db_session_factory() as session:
        course = (
            session.query(ClassroomCourseCache)
            .filter_by(course_date=date_str, course_sched_id=course_sched_id)
            .first()
        )
        if course:
            course.sign_status = 1
        expire_events(
            session.query(NotificationEvent).filter_by(
                event_key=f"classroom:reminder:{course_sched_id}:{date_str}",
                status="pending",
            )
        )
        session.commit()


def register_all_tasks(registry: TaskRegistry, session_mgr: BuaaSessionManager) -> None:
    """Register all BUAA business tasks at application startup."""

    async def handle_classroom_signin(
        ctx: TaskContext, params: ClassroomSigninParams
    ) -> TaskResult:
        client = ClassroomClient(session_mgr, read_network_mode(ctx.db_session_factory))
        date_str = params.course_date or ctx.clock.now_shanghai().strftime("%Y%m%d")
        course_name = params.course_name or params.course_sched_id
        signed_key = f"classroom:signed:{params.course_sched_id}:{date_str}"

        with ctx.db_session_factory() as session:
            cached = (
                session.query(ClassroomCourseCache)
                .filter_by(course_date=date_str, course_sched_id=params.course_sched_id)
                .first()
            )
            begin_sh = (
                parse_classroom_dt(cached.class_begin_time, date_str)
                if cached
                else None
            )
            end_sh = (
                parse_classroom_dt(cached.class_end_time, date_str) if cached else None
            )
        if not begin_sh or not end_sh:
            return TaskResult(
                status="failed", summary="课程缺少有效上课时间", complete_window=True
            )
        now_sh = ctx.clock.now_shanghai()
        earliest_sh, latest_sh = classroom_signin_bounds(begin_sh, end_sh)
        if now_sh < earliest_sh:
            return TaskResult(
                status="waiting",
                summary=f"签到时间未到（{earliest_sh:%H:%M} 开始）",
                wait_reason="before_classroom_signin_window",
                resume_at=earliest_sh.astimezone(UTC_TZ),
            )
        if now_sh >= latest_sh:
            return TaskResult(
                status="failed",
                summary=f"已超过签到截止时间（{latest_sh:%H:%M}）",
                error_message="after_classroom_signin_window",
                complete_window=True,
            )

        if ctx.stage == CONFIRM_STAGE:
            try:
                courses = await client.get_today_courses(date_str)
            except BuaaError as exc:
                return TaskResult(
                    status="pending_confirm",
                    summary=f"确认签到状态失败，等待重试: {exc}",
                    wait_reason=str(exc),
                    next_stage=CONFIRM_STAGE,
                )
            found = next(
                (c for c in courses if c.course_sched_id == params.course_sched_id),
                None,
            )
            if found and found.signed:
                _record_classroom_signin(ctx, params.course_sched_id, date_str)
                return TaskResult(
                    status="succeeded",
                    summary=f"课堂签到成功: {found.course_name}（平台已确认）",
                    complete_window=True,
                    events=[
                        _success_event(
                            signed_key,
                            f"课堂签到成功: {found.course_name}",
                            found.course_name,
                            summary="平台记录已确认签到",
                        )
                    ],
                )

        try:
            res = await client.sign_in(params.course_sched_id, date_str)
        except OutcomeUnknownError as exc:
            return _unknown_outcome(
                ctx,
                exc,
                f"classroom:unknown:{ctx.execution_id}",
                f"课堂签到结果待确认: {params.course_name}",
                params.course_name,
            )
        except CaptchaRequiredError as exc:
            return _captcha_wait(ctx, exc)
        except BusinessTerminalError as exc:
            return TaskResult(
                status="failed",
                summary=str(exc),
                error_message=str(exc),
                complete_window=True,
            )
        except BuaaError as exc:
            return TaskResult(
                status="failed",
                summary=f"课堂签到失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )
        if not res.success:
            return TaskResult(
                status="waiting",
                summary=res.message,
                wait_reason="等待平台签到开放",
                resume_at=ctx.clock.now_utc() + timedelta(seconds=30),
            )
        _record_classroom_signin(ctx, params.course_sched_id, date_str)
        return TaskResult(
            status="succeeded",
            summary=f"{'此前已签到' if res.already_signed else '课堂签到成功'}: {course_name}",
            complete_window=True,
            events=[]
            if res.already_signed
            else [
                _success_event(
                    signed_key,
                    f"课堂签到成功: {course_name}",
                    course_name,
                    summary=res.message,
                )
            ],
        )

    async def handle_boya_select(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        client = BoyaClient(session_mgr, read_network_mode(ctx.db_session_factory))
        now = ctx.clock.now_shanghai()

        def selected() -> TaskResult:
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
                    _success_event(
                        f"boya:select_ok:{ctx.schedule_id}",
                        f"博雅报名成功: {params.course_name}",
                        params.course_name,
                        message=f"已报名《{params.course_name}》",
                    )
                ],
            )

        def awaiting_confirmation(exc: BuaaError) -> TaskResult:
            return TaskResult(
                status="pending_confirm",
                summary=f"报名结果待确认: {params.course_name}",
                error_message=str(exc),
                next_stage=CONFIRM_STAGE,
            )

        try:
            config = await client.get_all_config()
            chosen = await client.query_chosen_courses(
                *resolve_semester_window(config, now)
            )
            if any(c.course_id == params.course_id for c in chosen):
                return selected()
            if ctx.stage == CONFIRM_STAGE or ctx.get_stage_payload(
                "select_request"
            ).get("pending"):
                return _wait(ctx, f"等待平台确认报名结果: {params.course_name}")
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
                return _wait(ctx, f"名额已满，等待空余名额: {params.course_name}")
            # Persist the confirmation marker before sending a non-idempotent request.
            ctx.save_stage("select_request", "pending", {"pending": True})
            await client.select_course(params.course_id)
            return selected()
        except OutcomeUnknownError as exc:
            return awaiting_confirmation(exc)
        except BusinessTerminalError as exc:
            ctx.save_stage("select_request", "failed", {"pending": False})
            if any(word in str(exc) for word in BOYA_RETRY_LATER_WORDS):
                return _wait(ctx, str(exc))
            return TaskResult(
                status="failed", summary=f"博雅报名失败: {exc}", complete_window=True
            )
        except BuaaError as exc:
            if ctx.get_stage_payload("select_request").get("pending"):
                return awaiting_confirmation(exc)
            return _wait(ctx, f"报名出错，等待重试: {exc}")

    async def handle_boya_sign(
        ctx: TaskContext, params: BoyaCourseActionParams, sign_type: int
    ) -> TaskResult:
        label = "签到" if sign_type == 1 else "签退"
        client = BoyaClient(session_mgr, read_network_mode(ctx.db_session_factory))
        try:
            all_cfg = await client.get_all_config()
            chosen = await client.query_chosen_courses(
                *resolve_semester_window(all_cfg, ctx.clock.now_shanghai())
            )
            course = next((c for c in chosen if c.course_id == params.course_id), None)
            if course is None:
                return TaskResult(
                    status="failed",
                    summary=f"未在已选课程中找到: {params.course_name or params.course_id}",
                    error_message="not_selected",
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
            if ctx.stage == CONFIRM_STAGE:
                return TaskResult(
                    status="failed",
                    summary=f"平台状态显示博雅{label}尚未完成，继续尝试",
                    error_message="pending_confirm_not_yet_done",
                    retryable=True,
                )

            sign_cfg = (
                course.sign_config
                or (await client.query_course_detail(params.course_id)).sign_config
            )
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
        except OutcomeUnknownError as exc:
            return _unknown_outcome(
                ctx,
                exc,
                f"boya:sign_unknown:{sign_type}:{ctx.execution_id}",
                f"博雅{label}结果待确认: {params.course_name}",
                params.course_name,
            )
        except BusinessTerminalError as exc:
            return TaskResult(
                status="failed",
                summary=f"博雅{label}失败: {exc}",
                error_message=str(exc),
                complete_window=True,
            )
        except CaptchaRequiredError as exc:
            return _captcha_wait(ctx, exc)
        except BuaaError as exc:
            return TaskResult(
                status="failed",
                summary=f"博雅{label}失败: {exc}",
                error_message=str(exc),
                retryable=True,
            )

        reminder_prefix = "signin_rem" if sign_type == 1 else "signout_rem"
        with ctx.db_session_factory() as session:
            expire_events(
                session.query(NotificationEvent).filter(
                    NotificationEvent.event_key.like(
                        f"boya:{reminder_prefix}:{params.course_id}:%"
                    ),
                    NotificationEvent.status == "pending",
                )
            )
            session.commit()
        return TaskResult(
            status="succeeded",
            summary=f"博雅{label}成功: {course.name}",
            complete_window=True,
            events=[
                _success_event(
                    f"boya:sign_ok:{sign_type}:{params.course_id}",
                    f"博雅{label}成功: {course.name}",
                    course.name,
                    summary=f"已完成博雅{label}",
                )
            ],
        )

    async def handle_boya_signin(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        return await handle_boya_sign(ctx, params, sign_type=1)

    async def handle_boya_signout(
        ctx: TaskContext, params: BoyaCourseActionParams
    ) -> TaskResult:
        return await handle_boya_sign(ctx, params, sign_type=2)

    def sync_summary(summary: str, data: dict[str, Any]) -> TaskResult:
        return TaskResult(status="succeeded", summary=summary, data=data)

    handle_classroom_sync = _sync_handler(
        "classroom",
        "课堂课表",
        lambda ctx: sync_classroom_today(
            ctx.db_session_factory, session_mgr, registry, ctx.clock
        ),
        lambda courses: sync_summary(
            f"课堂课表已同步: 当日 {len(courses)} 门课程", {"count": len(courses)}
        ),
    )
    handle_ygdk_sync = _sync_handler(
        "ygdk",
        "阳光打卡",
        lambda ctx: sync_ygdk_progress(ctx.db_session_factory, session_mgr, ctx.clock),
        lambda overview: sync_summary(
            f"阳光打卡已同步: 本学期累计 {overview.term_count} 次",
            {"term_count": overview.term_count},
        ),
    )
    handle_td_sync = _sync_handler(
        "td",
        "体能锻炼",
        lambda ctx: sync_td_progress(ctx.db_session_factory, session_mgr, ctx.clock),
        lambda overview: sync_summary(
            f"体能锻炼已同步: 本学期累计 {overview.term_count:g} 次"
            if overview.term_count is not None
            else "体能锻炼已同步: 本学期暂无记录",
            {"term_count": overview.term_count},
        ),
    )
    handle_boya_sync = _sync_handler(
        "boya",
        "博雅课程",
        lambda ctx: sync_boya_all(
            ctx.db_session_factory, session_mgr, registry, ctx.clock
        ),
        lambda result: sync_summary(
            f"博雅课程已同步: 可选 {len(result[0])} 门，已选 {len(result[1])} 门，有效次数 {result[2].valid_count}",
            {
                "courses": len(result[0]),
                "chosen": len(result[1]),
                "valid_count": result[2].valid_count,
            },
        ),
    )

    for task_type, schema, handler, module, display_name, description in (
        (
            "td.sync_overview",
            EmptyParams,
            handle_td_sync,
            "td",
            "体能锻炼状态更新",
            "查询健康云当前学期的体能锻炼次数",
        ),
        (
            "classroom.sync_today",
            EmptyParams,
            handle_classroom_sync,
            "classroom",
            "课堂课表刷新与签到发现",
            "查询当日课堂安排，生成开课前 5 分钟（可配置，限课前 5 分钟至上课后 30 分钟内）的自动签到任务与提前提醒",
        ),
        (
            "classroom.auto_signin",
            ClassroomSigninParams,
            handle_classroom_signin,
            "classroom",
            "课堂自动签到",
            "在课程签到时间窗口内轮询课程状态并自动完成课堂签到",
        ),
        (
            "ygdk.sync_overview",
            EmptyParams,
            handle_ygdk_sync,
            "ygdk",
            "阳光打卡进度刷新",
            "查询当前学期阳光打卡完成次数与目标",
        ),
        (
            "boya.sync_courses",
            EmptyParams,
            handle_boya_sync,
            "boya",
            "博雅课程刷新与选课/签到发现",
            "刷新博雅课程、已选课程与类别次数统计，安排自主签到及签退任务",
        ),
        (
            "boya.auto_select",
            BoyaCourseActionParams,
            handle_boya_select,
            "boya",
            "博雅预约报名",
            "在选课时间内自动报名，满额后每 30 秒查询，成功后通知",
        ),
        (
            "boya.auto_signin",
            BoyaCourseActionParams,
            handle_boya_signin,
            "boya",
            "博雅自主签到",
            "在已选博雅课程签到窗口内自动提交位置签到",
        ),
        (
            "boya.auto_signout",
            BoyaCourseActionParams,
            handle_boya_signout,
            "boya",
            "博雅自主签退",
            "在已选博雅课程签退窗口内自动提交位置签退",
        ),
    ):
        registry.register_task(
            task_type,
            schema,
            handler,
            module=module,
            display_name=display_name,
            description=description,
        )
