from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from app.buaa.boya import (
    BoyaClient,
    BoyaCourseData,
    BoyaStatsData,
    course_matches_campus,
    infer_course_campus,
    parse_boya_dt,
    resolve_semester_window,
)
from app.buaa.classroom import ClassroomClient, ClassroomCourse
from app.buaa.exceptions import BuaaError
from app.buaa.sso import BuaaSessionManager
from app.buaa.td import TdClient, TdOverviewData
from app.buaa.ygdk import YgdkClient, YgdkOverviewData
from app.core.network import get_network_mode, read_network_mode
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock
from app.db.models import (
    BoyaCourseCache,
    BusinessCacheSnapshot,
    CampusAccount,
    ClassroomCourseCache,
    ModuleConfig,
    NotificationEvent,
    NotificationRule,
    TaskSchedule,
)
from app.framework.tasks import TaskRegistry, cancel_schedule, create_schedule
from app.modules.automation import RESERVATION_TASK
from app.notifications.service import (
    expire_events,
    publish_event,
    render_notification_text,
)
from sqlalchemy.orm import Session

WINDOW_POLL_SECONDS = 30
COURSE_REMINDERS = {"signup_reminder", "signin_reminder", "signout_reminder"}


def format_window_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


class CourseRefresh:
    """Tracks the course windows and reminders one refresh still wants.

    `finish()` cancels this module's windows and course reminders that the refresh no
    longer produced, so cancelled or rescheduled courses do not leave stale work behind.
    """

    def __init__(
        self, session: Session, registry: TaskRegistry, module: str, clock: Clock
    ) -> None:
        self.session = session
        self.registry = registry
        self.module = module
        self.clock = clock
        self._window_keys: set[str] = set()
        self._reminder_keys: set[str] = set()

    def remind(
        self,
        *,
        event_type: str,
        event_key: str,
        payload: dict[str, Any],
        send_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Publish a reminder, reviving an unsent one whose course time moved."""
        self._reminder_keys.add(event_key)
        existing = (
            self.session.query(NotificationEvent).filter_by(event_key=event_key).first()
        )
        if existing is None:
            publish_event(
                self.session,
                event_type=event_type,
                event_key=event_key,
                module=self.module,
                payload=payload,
                send_at=send_at,
                expires_at=expires_at,
                clock=self.clock,
            )
            return
        if existing.attempts or existing.status not in {"pending", "expired"}:
            return
        send_at = max(self.clock.now_utc(), send_at)
        if expires_at > send_at:
            existing.status = "pending"
            existing.next_retry_at = send_at
            existing.expires_at = expires_at
            existing.title, existing.body = render_notification_text(
                event_type, self.module, payload
            )
            existing.payload_json = payload

    def plan_window(
        self,
        *,
        name: str,
        task_type: str,
        business_key: str,
        parameters: dict[str, Any],
        start: datetime,
        end: datetime,
    ) -> None:
        self._window_keys.add(business_key)
        create_schedule(
            self.session,
            self.registry,
            name=name,
            task_type=task_type,
            parameters=parameters,
            trigger_type="window",
            trigger_config={
                "start_at": format_window_time(start),
                "end_at": format_window_time(end),
                "poll_interval_seconds": WINDOW_POLL_SECONDS,
            },
            business_key=business_key,
            clock=self.clock,
        )

    def window_completed(self, business_key: str, end: datetime) -> bool:
        """Whether the window ending at `end` already succeeded (the action is done)."""
        schedule = (
            self.session.query(TaskSchedule)
            .filter_by(
                business_key=business_key,
                status="completed",
                last_result_status="succeeded",
            )
            .first()
        )
        return bool(
            schedule
            and schedule.trigger_config_json.get("end_at") == format_window_time(end)
        )

    def finish(self) -> None:
        now = self.clock.now_utc()
        for sched in self.session.query(TaskSchedule).filter_by(
            module=self.module, trigger_type="window", status="enabled"
        ):
            if (
                sched.task_type != RESERVATION_TASK
                and sched.business_key not in self._window_keys
            ):
                cancel_schedule(self.session, sched, now)
        expire_events(
            event
            for event in self.session.query(NotificationEvent).filter_by(
                module=self.module, status="pending"
            )
            if event.event_type in COURSE_REMINDERS
            and event.event_key not in self._reminder_keys
        )


def save_snapshot(
    session: Session, key: str, payload: dict[str, Any], now_utc: datetime
) -> None:
    snapshot = session.get(BusinessCacheSnapshot, key)
    if snapshot is None:
        session.add(
            BusinessCacheSnapshot(key=key, payload_json=payload, synced_at=now_utc)
        )
    else:
        snapshot.payload_json = payload
        snapshot.synced_at = now_utc


def set_module_health(
    session: Session, module: str, now_utc: datetime, error: str | None = None
) -> None:
    mod = session.get(ModuleConfig, module)
    if mod is not None:
        mod.health_status = "error" if error else "healthy"
        mod.last_error = error
        mod.last_checked_at = now_utc


def mark_module_error(
    session_factory: Any, module: str, error: str, clock: Clock
) -> None:
    with session_factory() as session:
        set_module_health(session, module, clock.now_utc(), error)
        session.commit()


def _reminder_advance(
    session: Session, event_type: str, module: str, default: int
) -> timedelta:
    rule = (
        session.query(NotificationRule)
        .filter_by(event_type=event_type, module=module)
        .first()
    )
    return timedelta(minutes=max(0, rule.advance_minutes) if rule else default)


def classroom_signin_bounds(
    begin_sh: datetime, end_sh: datetime
) -> tuple[datetime, datetime]:
    """Hard sign-in bounds: [begin - 5m, min(begin + 30m, end)], per iClass server window."""
    return begin_sh - timedelta(minutes=5), min(
        begin_sh + timedelta(minutes=30), end_sh
    )


def compute_classroom_signin_window(
    begin_sh: datetime, end_sh: datetime, offset_minutes: int
) -> tuple[datetime, datetime] | None:
    """Sign-in window starting at begin + offset, clamped to the platform bounds."""
    earliest_sh, latest_sh = classroom_signin_bounds(begin_sh, end_sh)
    start_sh = max(earliest_sh, begin_sh + timedelta(minutes=offset_minutes))
    return (start_sh, latest_sh) if start_sh < latest_sh else None


def parse_classroom_dt(value: str, date_str: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    if len(text) in (5, 8) and ":" in text:
        day = datetime.strptime(date_str, "%Y%m%d").date()
        tod = datetime.strptime(text, "%H:%M:%S" if len(text) == 8 else "%H:%M").time()
        return datetime.combine(day, tod, tzinfo=SHANGHAI_TZ)
    return None


async def sync_classroom_today(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    registry: TaskRegistry,
    clock: Clock,
) -> list[ClassroomCourse]:
    now_sh = clock.now_shanghai()
    date_str = now_sh.strftime("%Y%m%d")
    with session_factory() as session:
        network_mode = get_network_mode(session)
        mod = session.get(ModuleConfig, "classroom")
        enabled = mod.enabled
        auto_sign = mod.config_json.get("auto_sign", True)
        offset_minutes = int(mod.config_json.get("signin_offset_minutes", -5))

    courses = await ClassroomClient(session_mgr, network_mode).get_today_courses(
        date_str
    )
    now_utc = clock.now_utc()

    with session_factory() as session:
        refresh = CourseRefresh(session, registry, "classroom", clock)
        advance = _reminder_advance(session, "signin_reminder", "classroom", 15)
        session.query(ClassroomCourseCache).filter_by(course_date=date_str).delete()
        for c in courses:
            cached = ClassroomCourseCache(
                course_date=date_str,
                course_sched_id=c.course_sched_id,
                course_name=c.course_name,
                class_begin_time=c.class_begin_time,
                class_end_time=c.class_end_time,
                sign_status=c.sign_status,
                raw_json=c.raw,
                synced_at=now_utc,
            )
            session.add(cached)
            begin_sh = parse_classroom_dt(c.class_begin_time, date_str)
            end_sh = parse_classroom_dt(c.class_end_time, date_str)
            if not begin_sh or not end_sh:
                continue
            window_key = f"classroom:signin:{date_str}:{c.course_sched_id}"
            if refresh.window_completed(
                window_key, classroom_signin_bounds(begin_sh, end_sh)[1]
            ):
                c.sign_status = cached.sign_status = 1
            if not enabled or c.signed:
                continue
            if now_sh < end_sh:
                refresh.remind(
                    event_type="signin_reminder",
                    event_key=f"classroom:reminder:{c.course_sched_id}:{date_str}",
                    payload={
                        "title": f"课堂签到提醒: {c.course_name}",
                        "course_name": c.course_name,
                        "message": f"上课时间 {begin_sh:%H:%M} ~ {end_sh:%H:%M}",
                    },
                    send_at=(begin_sh - advance).astimezone(UTC_TZ),
                    expires_at=end_sh.astimezone(UTC_TZ),
                )
            window = compute_classroom_signin_window(begin_sh, end_sh, offset_minutes)
            if auto_sign and window and now_sh < window[1]:
                refresh.plan_window(
                    name=f"课堂签到: {c.course_name}",
                    task_type="classroom.auto_signin",
                    business_key=window_key,
                    parameters={
                        "course_sched_id": c.course_sched_id,
                        "course_name": c.course_name,
                        "course_date": date_str,
                    },
                    start=window[0],
                    end=window[1],
                )
        refresh.finish()
        set_module_health(session, "classroom", now_utc)
        session.commit()
    return courses


async def sync_td_progress(
    session_factory: Any, session_mgr: BuaaSessionManager, clock: Clock
) -> TdOverviewData:
    with session_factory() as session:
        network_mode = get_network_mode(session)
        account = session.query(CampusAccount).first()
        if not account or not account.student_id:
            raise BuaaError("请先配置校园账号")
        student_id = account.student_id
    overview = await TdClient(session_mgr, network_mode).get_overview()
    now = clock.now_utc()
    with session_factory() as session:
        account = session.query(CampusAccount).first()
        if not account or account.student_id != student_id:
            raise BuaaError("校园账号已变更，请重新刷新体能锻炼状态")
        save_snapshot(
            session, "td_overview", {**asdict(overview), "student_id": student_id}, now
        )
        set_module_health(session, "td", now)
        session.commit()
    return overview


def _sunshine_reminder_due(overview: YgdkOverviewData) -> bool:
    """Remind only while both the term and the week are below their platform targets."""
    term, week = overview.term_count, overview.week_count
    return (
        term is not None
        and week is not None
        and 0 <= term < (overview.term_target or 0)
        and 0 <= week < (overview.week_target or 0)
    )


async def sync_ygdk_progress(
    session_factory: Any, session_mgr: BuaaSessionManager, clock: Clock
) -> YgdkOverviewData:
    network_mode = read_network_mode(session_factory)
    overview = await YgdkClient(session_mgr, network_mode).get_overview()
    now_utc = clock.now_utc()
    now_sh = clock.now_shanghai()
    with session_factory() as session:
        save_snapshot(session, "ygdk_overview", asdict(overview), now_utc)
        mod = session.get(ModuleConfig, "ygdk")
        reminder_time = mod.config_json.get("reminder_time", "18:00")
        if (
            mod.enabled
            and now_sh.strftime("%H:%M") >= reminder_time
            and _sunshine_reminder_due(overview)
        ):
            tomorrow = (now_sh + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            publish_event(
                session,
                event_type="exercise_reminder",
                module="ygdk",
                event_key=f"ygdk:reminder:{overview.term_id}:{now_sh.date().isoformat()}",
                payload={
                    "title": "阳光打卡提醒",
                    "message": f"本学期 {overview.term_count}/{overview.term_target} 次，本周 {overview.week_count}/{overview.week_target} 次，记得打卡。",
                },
                expires_at=tomorrow.astimezone(UTC_TZ),
                clock=clock,
            )
        else:
            expire_events(
                session.query(NotificationEvent).filter_by(
                    module="ygdk", event_type="exercise_reminder", status="pending"
                )
            )
        set_module_health(session, "ygdk", now_utc)
        session.commit()
    return overview


@dataclass(frozen=True)
class _SignAction:
    action: str  # notification/task suffix: signin | signout
    label: str
    config_prefix: str  # key prefix inside the platform sign config
    key_prefix: str  # schedule business key prefix
    pending_checkins: tuple[int | None, ...]  # attendance codes meaning "not done yet"


_SIGN_ACTIONS = (
    _SignAction("signin", "签到", "sign", "sign", (None, 0)),
    _SignAction("signout", "签退", "signOut", "signout", (None, 0, 5, 6)),
)

_CACHE_FIELDS = (
    "name",
    "location",
    "category",
    "sub_category",
    "sign_method",
    "has_autonomous_sign",
    "selected",
    "chosen_id",
    "course_start",
    "course_end",
    "select_start",
    "select_end",
    "cancel_end",
    "current_count",
    "max_count",
    "campus",
    "sign_type",
    "checkin",
    "pass_status",
    "score",
)


def _merge_chosen(
    courses: list[BoyaCourseData], chosen: list[BoyaCourseData]
) -> dict[int, BoyaCourseData]:
    """Overlay enrollment state from the chosen list onto the catalogue courses."""
    merged = {c.course_id: c for c in courses}
    for ch in chosen:
        base = merged.setdefault(ch.course_id, ch)
        if base is ch:
            continue
        base.selected = True
        base.chosen_id = ch.chosen_id
        base.checkin = ch.checkin
        base.pass_status = ch.pass_status
        base.score = ch.score
        if not base.sign_config and ch.sign_config:
            base.sign_config = ch.sign_config
            base.has_autonomous_sign = ch.has_autonomous_sign
            base.sign_method = ch.sign_method
    return merged


def _remind_boya_signup(
    refresh: CourseRefresh, c: BoyaCourseData, user_campus: str, advance: timedelta
) -> None:
    s_start = parse_boya_dt(c.select_start)
    s_end = parse_boya_dt(c.select_end)
    now_utc = refresh.clock.now_utc()
    if (
        not s_start
        or not s_end
        or c.selected
        or refresh.clock.now_shanghai() >= s_end
        or (user_campus != "全部" and not course_matches_campus(c, user_campus))
        or (c.max_count and (c.current_count or 0) >= c.max_count)
    ):
        return
    s_end_utc = s_end.astimezone(UTC_TZ)
    send_at = max(now_utc, (s_start - advance).astimezone(UTC_TZ))
    campus = c.resolved_campus or infer_course_campus(c)
    refresh.remind(
        event_type="signup_reminder",
        event_key=f"boya:signup_rem:{c.course_id}:{c.select_start}",
        payload={
            "title": f"博雅选课提醒: {c.name}",
            "course_name": c.name,
            "campus": campus,
            "location": c.location,
            "message": (
                f"分类: {c.sub_category or '未分类'} / {c.sign_method}"
                f"\n地点: {campus} {c.location}".rstrip()
                + f"\n选课: {s_start:%m-%d %H:%M} ~ {s_end:%m-%d %H:%M}"
            ),
        },
        send_at=send_at if send_at < s_end_utc else now_utc,
        expires_at=s_end_utc,
    )


def _sync_boya_sign_windows(
    refresh: CourseRefresh, c: BoyaCourseData, auto_sign: bool
) -> None:
    now_sh = refresh.clock.now_shanghai()
    cfg = c.sign_config or {}
    course_times = {
        "signin": (c.course_start, c.course_end),
        "signout": (c.course_end, None),
    }
    for spec in _SIGN_ACTIONS:
        start = parse_boya_dt(cfg.get(f"{spec.config_prefix}StartDate"))
        end = parse_boya_dt(cfg.get(f"{spec.config_prefix}EndDate"))
        business_key = f"boya:{spec.key_prefix}:{c.course_id}"
        if c.checkin not in spec.pending_checkins or (
            end and refresh.window_completed(business_key, end)
        ):
            continue

        # Regular courses have no sign window; remind around the course time instead.
        course_time, course_time_end = course_times[spec.action]
        reminder_start = start or parse_boya_dt(course_time)
        reminder_end = end or parse_boya_dt(course_time_end)
        if reminder_start and not reminder_end:
            reminder_end = reminder_start + timedelta(minutes=15)
        if reminder_start and reminder_end and now_sh < reminder_end:
            event_type = f"{spec.action}_reminder"
            advance = _reminder_advance(refresh.session, event_type, "boya", 15)
            refresh.remind(
                event_type=event_type,
                event_key=f"boya:{spec.action}_rem:{c.course_id}:{cfg.get(f'{spec.config_prefix}StartDate') or course_time}",
                payload={
                    "title": f"博雅{spec.label}提醒: {c.name}",
                    "course_name": c.name,
                    "message": f"{spec.label}时间 {start:%m-%d %H:%M} ~ {end:%H:%M}"
                    if start and end
                    else f"课程{'开始' if spec.action == 'signin' else '结束'}时间 {reminder_start:%m-%d %H:%M}",
                },
                send_at=(reminder_start - advance).astimezone(UTC_TZ),
                expires_at=reminder_end.astimezone(UTC_TZ),
            )

        if (
            auto_sign
            and c.has_autonomous_sign
            and start
            and end
            and start < end
            and now_sh < end
        ):
            refresh.plan_window(
                name=f"博雅自主{spec.label}: {c.name}",
                task_type=f"boya.auto_{spec.action}",
                business_key=business_key,
                parameters={"course_id": c.course_id, "course_name": c.name},
                start=start,
                end=end,
            )


async def sync_boya_all(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    registry: TaskRegistry,
    clock: Clock,
) -> tuple[list[BoyaCourseData], list[BoyaCourseData], BoyaStatsData]:
    with session_factory() as session:
        network_mode = get_network_mode(session)
        mod = session.get(ModuleConfig, "boya")
        enabled = mod.enabled
        auto_sign = bool(mod.config_json.get("auto_sign", True))
        account = session.query(CampusAccount).first()
        user_campus = (
            (account.campus if account else "北京")
            if mod.config_json.get("filter_by_campus", True)
            else "全部"
        )

    client = BoyaClient(session_mgr, network_mode)
    all_config = await client.get_all_config()
    sem_start, sem_end = resolve_semester_window(all_config, clock.now_shanghai())
    courses = await client.query_courses()
    chosen = await client.query_chosen_courses(sem_start, sem_end)
    stats = await client.query_statistics()
    now_utc = clock.now_utc()

    with session_factory() as session:
        refresh = CourseRefresh(session, registry, "boya", clock)
        signup_advance = _reminder_advance(session, "signup_reminder", "boya", 5)
        merged = _merge_chosen(courses, chosen)
        session.query(BoyaCourseCache).filter(
            BoyaCourseCache.course_id.notin_(merged)
        ).delete(synchronize_session=False)

        for cid, c in merged.items():
            cached = session.get(BoyaCourseCache, cid)
            if cached is None:
                cached = BoyaCourseCache(course_id=cid)
                session.add(cached)
            for name in _CACHE_FIELDS:
                setattr(cached, name, getattr(c, name))
            cached.sign_config_json = c.sign_config
            cached.raw_json = c.raw
            cached.synced_at = now_utc

            if not enabled:
                continue
            _remind_boya_signup(refresh, c, user_campus, signup_advance)
            if c.selected and c.pass_status != 1:
                _sync_boya_sign_windows(refresh, c, auto_sign)

        save_snapshot(session, "boya_statistics", asdict(stats), now_utc)
        refresh.finish()
        set_module_health(session, "boya", now_utc)
        session.commit()

    return courses, chosen, stats
