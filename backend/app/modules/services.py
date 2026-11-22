from __future__ import annotations

from dataclasses import asdict
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
from app.core.network import get_unified_network_mode
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock, default_clock
from app.db.models import (
    BoyaCourseCache,
    BusinessCacheSnapshot,
    CampusAccount,
    ClassroomCourseCache,
    ModuleConfig,
    NotificationEvent,
    NotificationRule,
    TaskExecution,
    TaskSchedule,
)
from app.framework.tasks import TaskRegistry, create_schedule
from app.notifications.service import (
    publish_event as _publish_event,
)
from app.notifications.service import (
    render_notification_text,
)
from sqlalchemy.orm import Session


def publish_event(session: Session, **kwargs: Any) -> Any:
    session.info.setdefault("course_event_keys", set()).add(kwargs["event_key"])
    existing = (
        session.query(NotificationEvent)
        .filter_by(event_key=kwargs["event_key"])
        .first()
    )
    if (
        existing
        and existing.attempts == 0
        and existing.status in {"pending", "expired"}
    ):
        clk = kwargs.get("clock") or default_clock
        send_at = max(clk.now_utc(), kwargs.get("send_at") or clk.now_utc())
        expires_at = kwargs.get("expires_at")
        if expires_at and expires_at > send_at:
            existing.status = "pending"
            existing.next_retry_at = send_at
            existing.expires_at = expires_at
            existing.title, existing.body = render_notification_text(
                kwargs["event_type"], kwargs["module"], kwargs["payload"]
            )
            existing.payload_json = kwargs["payload"]
        return existing
    return _publish_event(session, **kwargs)


def plan_course_schedule(
    session: Session, registry: TaskRegistry, **kwargs: Any
) -> TaskSchedule:
    session.info.setdefault("course_schedule_keys", set()).add(kwargs["business_key"])
    return create_schedule(session, registry, **kwargs)


def course_window_completed(session: Session, key: str, end: datetime) -> bool:
    schedule = (
        session.query(TaskSchedule)
        .filter_by(business_key=key, status="completed", last_result_status="succeeded")
        .first()
    )
    return bool(
        schedule
        and schedule.trigger_config_json.get("end_at")
        == end.strftime("%Y-%m-%d %H:%M:%S")
    )


def finish_course_refresh(
    session: Session, module: str, *, auto_schedule: bool
) -> None:
    if auto_schedule:
        keys = session.info.get("course_schedule_keys", set())
        for sched in (
            session.query(TaskSchedule)
            .filter_by(module=module, trigger_type="window", status="enabled")
            .all()
        ):
            if sched.task_type == "boya.auto_select":
                continue
            if sched.business_key not in keys:
                sched.status = "cancelled"
                sched.next_run_at = None
                for execution in (
                    session.query(TaskExecution)
                    .filter(
                        TaskExecution.schedule_id == sched.id,
                        TaskExecution.status.in_(
                            ["pending", "waiting", "pending_confirm"]
                        ),
                    )
                    .all()
                ):
                    execution.status = "cancelled"
                    execution.next_retry_at = execution.resume_at = None
    event_keys = session.info.get("course_event_keys", set())
    for event in (
        session.query(NotificationEvent)
        .filter_by(module=module, status="pending")
        .all()
    ):
        if (
            event.event_type
            in {"signup_reminder", "signin_reminder", "signout_reminder"}
            and event.event_key not in event_keys
        ):
            event.status = "expired"
            event.next_retry_at = None


def _get_module_config(session: Session, module: str) -> ModuleConfig:
    cfg = session.get(ModuleConfig, module)
    if cfg is None:
        cfg = ModuleConfig(
            module=module, enabled=False, network_mode="webvpn", config_json={}
        )
        session.add(cfg)
        session.flush()
    return cfg


def resolve_classroom_offset_minutes(cfg_json: dict[str, Any]) -> int:
    """Resolve classroom sign-in offset minutes relative to class begin (negative = before class)."""
    if cfg_json.get("signin_offset_minutes") is not None:
        return int(cfg_json["signin_offset_minutes"])
    if cfg_json.get("offset_minutes") is not None:
        return int(cfg_json["offset_minutes"])
    if cfg_json.get("advance_minutes") is not None:
        legacy = int(cfg_json["advance_minutes"])
        return legacy if legacy <= 0 else -legacy
    return -5


def classroom_signin_bounds(
    begin_sh: datetime, end_sh: datetime
) -> tuple[datetime, datetime]:
    """Hard sign-in bounds: [begin - 5m, min(begin + 30m, end)], per iClass server window."""
    earliest_sh = begin_sh - timedelta(minutes=5)
    latest_sh = min(begin_sh + timedelta(minutes=30), end_sh)
    return earliest_sh, latest_sh


def compute_classroom_signin_window(
    begin_sh: datetime,
    end_sh: datetime,
    offset_minutes: int,
) -> tuple[datetime, datetime] | None:
    """Compute sign-in window clamped strictly within the platform sign-in bounds."""
    earliest_sh, latest_sh = classroom_signin_bounds(begin_sh, end_sh)
    if latest_sh <= earliest_sh:
        return None
    desired_start_sh = begin_sh + timedelta(minutes=offset_minutes)
    win_start_sh = max(earliest_sh, desired_start_sh)
    if win_start_sh >= latest_sh:
        return None
    return win_start_sh, latest_sh


def _get_advance_minutes(
    session: Session, event_type: str, module: str, default: int = 15
) -> int:
    rule = (
        session.query(NotificationRule)
        .filter_by(event_type=event_type, module=module)
        .first()
    )
    if rule:
        return max(0, int(rule.advance_minutes))
    return default


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
        d = datetime.strptime(date_str, "%Y%m%d").date()
        fmt = "%H:%M:%S" if len(text) == 8 else "%H:%M"
        t = datetime.strptime(text, fmt).time()
        return datetime.combine(d, t, tzinfo=SHANGHAI_TZ)
    return None


async def sync_classroom_today(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    registry: TaskRegistry,
    clock: Clock | None = None,
    *,
    auto_schedule: bool = True,
) -> list[ClassroomCourse]:
    clk = clock or default_clock
    now_sh = clk.now_shanghai()
    date_str = now_sh.strftime("%Y%m%d")

    with session_factory() as session:
        mod_cfg = _get_module_config(session, "classroom")
        network_mode = get_unified_network_mode(session)
        cfg_json = dict(mod_cfg.config_json or {})
        offset_min = resolve_classroom_offset_minutes(cfg_json)
        poll_interval = 30
        mod_enabled = mod_cfg.enabled

    client = ClassroomClient(session_mgr, network_mode=network_mode)
    courses = await client.get_today_courses(date_str)

    now_utc = clk.now_utc()

    with session_factory() as session:
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
            if begin_sh and end_sh:
                if course_window_completed(
                    session,
                    f"classroom:signin:{date_str}:{c.course_sched_id}",
                    classroom_signin_bounds(begin_sh, end_sh)[1],
                ):
                    c.sign_status = 1
                    cached.sign_status = 1
                # Reminder 15 minutes before class start (or immediate if already inside)
                rem_adv = _get_advance_minutes(
                    session, "signin_reminder", "classroom", 15
                )
                rem_start = begin_sh - timedelta(minutes=rem_adv)
                if mod_enabled and not c.signed and now_sh < end_sh:
                    publish_event(
                        session,
                        event_type="signin_reminder",
                        event_key=f"classroom:reminder:{c.course_sched_id}:{date_str}",
                        module="classroom",
                        payload={
                            "title": f"课堂签到提醒: {c.course_name}",
                            "course_name": c.course_name,
                            "message": f"上课时间 {begin_sh.strftime('%H:%M')} ~ {end_sh.strftime('%H:%M')}",
                        },
                        send_at=rem_start.astimezone(UTC_TZ),
                        expires_at=end_sh.astimezone(UTC_TZ),
                        clock=clk,
                    )

                win = compute_classroom_signin_window(begin_sh, end_sh, offset_min)
                if win is not None:
                    win_start_sh, win_end_sh = win
                    if (
                        auto_schedule
                        and mod_enabled
                        and cfg_json.get("auto_sign", True)
                        and not c.signed
                        and now_sh < win_end_sh
                    ):
                        plan_course_schedule(
                            session,
                            registry,
                            name=f"课堂签到: {c.course_name}",
                            task_type="classroom.auto_signin",
                            parameters={
                                "course_sched_id": c.course_sched_id,
                                "course_name": c.course_name,
                                "course_date": date_str,
                            },
                            trigger_type="window",
                            trigger_config={
                                "start_at": win_start_sh.strftime("%Y-%m-%d %H:%M:%S"),
                                "end_at": win_end_sh.strftime("%Y-%m-%d %H:%M:%S"),
                                "poll_interval_seconds": poll_interval,
                            },
                            business_key=f"classroom:signin:{date_str}:{c.course_sched_id}",
                            enabled=True,
                            clock=clk,
                        )

        finish_course_refresh(session, "classroom", auto_schedule=auto_schedule)
        mod_cfg = _get_module_config(session, "classroom")
        mod_cfg.health_status = "healthy"
        mod_cfg.last_error = None
        mod_cfg.last_checked_at = now_utc

        session.commit()

    return courses


async def sync_td_progress(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    clock: Clock | None = None,
) -> TdOverviewData:
    clk = clock or default_clock
    with session_factory() as session:
        network_mode = get_unified_network_mode(session)
        account = session.query(CampusAccount).first()
        if not account or not account.student_id:
            raise BuaaError("请先配置校园账号")
        student_id = account.student_id
    overview = await TdClient(session_mgr, network_mode).get_overview()
    now = clk.now_utc()
    with session_factory() as session:
        account = session.query(CampusAccount).first()
        if not account or account.student_id != student_id:
            raise BuaaError("校园账号已变更，请重新刷新体能锻炼状态")
        snapshot = session.get(BusinessCacheSnapshot, "td_overview")
        payload = {**asdict(overview), "student_id": student_id}
        if snapshot is None:
            session.add(
                BusinessCacheSnapshot(
                    key="td_overview", payload_json=payload, synced_at=now
                )
            )
        else:
            snapshot.payload_json = payload
            snapshot.synced_at = now
        mod = _get_module_config(session, "td")
        mod.health_status = "healthy"
        mod.last_error = None
        mod.last_checked_at = now
        session.commit()
    return overview


async def sync_ygdk_progress(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    clock: Clock | None = None,
) -> YgdkOverviewData:
    clk = clock or default_clock
    now_utc = clk.now_utc()
    with session_factory() as session:
        _get_module_config(session, "ygdk")
        network_mode = get_unified_network_mode(session)

    client = YgdkClient(session_mgr, network_mode=network_mode)
    overview = await client.get_overview()

    with session_factory() as session:
        snap = session.get(BusinessCacheSnapshot, "ygdk_overview")
        payload = asdict(overview)
        if snap is None:
            session.add(
                BusinessCacheSnapshot(
                    key="ygdk_overview", payload_json=payload, synced_at=now_utc
                )
            )
        else:
            snap.payload_json = payload
            snap.synced_at = now_utc

        mod_cfg = _get_module_config(session, "ygdk")
        reminder_time = (mod_cfg.config_json or {}).get("reminder_time", "18:00")
        now_sh = clk.now_shanghai()
        if (
            mod_cfg.enabled
            and now_sh.strftime("%H:%M") >= reminder_time
            and overview.term_target is not None
            and overview.term_target > 0
            and overview.week_target is not None
            and overview.week_target > 0
            and overview.term_count is not None
            and overview.term_count >= 0
            and overview.week_count is not None
            and overview.week_count >= 0
            and overview.term_count < overview.term_target
            and overview.week_count < overview.week_target
        ):
            tomorrow = (now_sh + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            _publish_event(
                session,
                event_type="exercise_reminder",
                module="ygdk",
                event_key=f"ygdk:reminder:{overview.term_id}:{now_sh.date().isoformat()}",
                payload={
                    "title": "阳光打卡提醒",
                    "message": f"本学期 {overview.term_count}/{overview.term_target} 次，本周 {overview.week_count}/{overview.week_target} 次，记得打卡。",
                },
                expires_at=tomorrow.astimezone(UTC_TZ),
                clock=clk,
            )
        else:
            for event in (
                session.query(NotificationEvent)
                .filter_by(
                    module="ygdk", event_type="exercise_reminder", status="pending"
                )
                .all()
            ):
                event.status = "expired"
                event.next_retry_at = None
        mod_cfg.health_status = "healthy"
        mod_cfg.last_error = None
        mod_cfg.last_checked_at = now_utc
        session.commit()

    return overview


def _sync_boya_course_notifications(
    session: Session,
    c: BoyaCourseData,
    *,
    cid: int,
    user_campus: str,
    now_sh: datetime,
    now_utc: datetime,
    clk: Clock,
) -> None:
    """Publish discovery and scheduled pre-selection notifications for a Boya course."""
    s_start = parse_boya_dt(c.select_start)
    s_end = parse_boya_dt(c.select_end)
    if not s_start or not s_end:
        return

    resolved_campus = c.resolved_campus or infer_course_campus(c)
    campus_ok = user_campus == "全部" or course_matches_campus(c, user_campus)

    adv = _get_advance_minutes(session, "signup_reminder", "boya", 5)
    rem_key = f"boya:signup_rem:{cid}:{c.select_start}"
    pending_rems = (
        session.query(NotificationEvent)
        .filter(
            NotificationEvent.module == "boya",
            NotificationEvent.event_type == "signup_reminder",
            NotificationEvent.event_key.like(f"boya:signup_rem:{cid}:%"),
            NotificationEvent.status == "pending",
        )
        .all()
    )

    if (
        not c.selected
        and now_sh < s_end
        and campus_ok
        and (not c.max_count or (c.current_count or 0) < c.max_count)
    ):
        target_send_sh = s_start - timedelta(minutes=adv)
        send_at_utc = max(now_utc, target_send_sh.astimezone(UTC_TZ))
        s_end_utc = s_end.astimezone(UTC_TZ)
        if send_at_utc >= s_end_utc:
            send_at_utc = now_utc

        for rem in pending_rems:
            if rem.event_key != rem_key:
                rem.status = "expired"
                rem.next_retry_at = None

        payload = {
            "title": f"博雅选课提醒: {c.name}",
            "course_name": c.name,
            "campus": resolved_campus,
            "location": c.location,
            "message": (
                f"分类: {c.sub_category or '未分类'} / {c.sign_method}"
                f"\n地点: {resolved_campus} {c.location}".rstrip()
                + f"\n选课: {s_start:%m-%d %H:%M} ~ {s_end:%m-%d %H:%M}"
            ),
        }

        publish_event(
            session,
            event_type="signup_reminder",
            event_key=rem_key,
            module="boya",
            payload=payload,
            send_at=send_at_utc,
            expires_at=s_end_utc,
            clock=clk,
        )
    else:
        for rem in pending_rems:
            rem.status = "expired"
            rem.next_retry_at = None


async def sync_boya_all(
    session_factory: Any,
    session_mgr: BuaaSessionManager,
    registry: TaskRegistry,
    clock: Clock | None = None,
    *,
    auto_schedule: bool = True,
) -> tuple[list[BoyaCourseData], list[BoyaCourseData], BoyaStatsData]:
    clk = clock or default_clock
    now_sh = clk.now_shanghai()
    now_utc = clk.now_utc()

    with session_factory() as session:
        mod_cfg = _get_module_config(session, "boya")
        network_mode = get_unified_network_mode(session)
        cfg_json = dict(mod_cfg.config_json or {})
        mod_enabled = mod_cfg.enabled
        account = session.query(CampusAccount).first()
        user_campus = (
            (account.campus if account else "北京")
            if cfg_json.get("filter_by_campus", True)
            else "全部"
        )

    client = BoyaClient(session_mgr, network_mode=network_mode)
    all_config = await client.get_all_config()
    sem_start, sem_end = resolve_semester_window(all_config, now_sh)
    courses = await client.query_courses()
    chosen = await client.query_chosen_courses(sem_start, sem_end)
    stats = await client.query_statistics()

    chosen_by_id = {c.course_id: c for c in chosen}
    auto_sign_enabled = bool(cfg_json.get("auto_sign", True))
    poll_interval = 30
    with session_factory() as session:
        merged_by_id: dict[int, BoyaCourseData] = {c.course_id: c for c in courses}
        for cid, ch in chosen_by_id.items():
            if cid in merged_by_id:
                base = merged_by_id[cid]
                base.selected = True
                base.chosen_id = ch.chosen_id
                base.checkin = ch.checkin
                base.pass_status = ch.pass_status
                base.score = ch.score
                if not base.sign_config and ch.sign_config:
                    base.sign_config = ch.sign_config
                    base.has_autonomous_sign = ch.has_autonomous_sign
                    base.sign_method = ch.sign_method
            else:
                merged_by_id[cid] = ch

        session.query(BoyaCourseCache).filter(
            BoyaCourseCache.course_id.notin_(merged_by_id)
        ).delete(synchronize_session=False)

        for cid, c in merged_by_id.items():
            cached = session.get(BoyaCourseCache, cid)
            if cached is None:
                cached = BoyaCourseCache(course_id=cid, name=c.name)
                session.add(cached)
            cached.name = c.name
            cached.location = c.location
            cached.category = c.category
            cached.sub_category = c.sub_category
            cached.sign_method = c.sign_method
            cached.has_autonomous_sign = c.has_autonomous_sign
            cached.selected = c.selected
            cached.chosen_id = c.chosen_id
            cached.course_start = c.course_start
            cached.course_end = c.course_end
            cached.select_start = c.select_start
            cached.select_end = c.select_end
            cached.cancel_end = c.cancel_end
            cached.current_count = c.current_count
            cached.max_count = c.max_count
            cached.campus = c.campus
            cached.sign_config_json = c.sign_config
            cached.sign_type = c.sign_type
            cached.checkin = c.checkin
            cached.pass_status = c.pass_status
            cached.score = c.score
            cached.raw_json = c.raw
            cached.synced_at = now_utc

            if mod_enabled:
                _sync_boya_course_notifications(
                    session,
                    c,
                    cid=cid,
                    user_campus=user_campus,
                    now_sh=now_sh,
                    now_utc=now_utc,
                    clk=clk,
                )

            if mod_enabled and c.selected and c.pass_status != 1:
                cfg = c.sign_config or {}
                actions = (
                    (
                        "signin",
                        "签到",
                        "sign",
                        "sign",
                        c.checkin in (None, 0),
                        c.course_start,
                        c.course_end,
                    ),
                    (
                        "signout",
                        "签退",
                        "signOut",
                        "signout",
                        c.checkin in (None, 0, 5, 6),
                        c.course_end,
                        None,
                    ),
                )
                for (
                    action,
                    label,
                    prefix,
                    key_prefix,
                    needed,
                    course_time,
                    course_end,
                ) in actions:
                    start = parse_boya_dt(cfg.get(f"{prefix}StartDate"))
                    end = parse_boya_dt(cfg.get(f"{prefix}EndDate"))
                    business_key = f"boya:{key_prefix}:{cid}"
                    if not needed or (
                        end and course_window_completed(session, business_key, end)
                    ):
                        continue
                    reminder_start = start or parse_boya_dt(course_time)
                    reminder_end = end or parse_boya_dt(course_end)
                    if reminder_start and not reminder_end:
                        reminder_end = reminder_start + timedelta(minutes=15)
                    if reminder_start and reminder_end and now_sh < reminder_end:
                        advance = _get_advance_minutes(
                            session, f"{action}_reminder", "boya", 15
                        )
                        publish_event(
                            session,
                            event_type=f"{action}_reminder",
                            event_key=f"boya:{action}_rem:{cid}:{cfg.get(f'{prefix}StartDate') or course_time}",
                            module="boya",
                            payload={
                                "title": f"博雅{label}提醒: {c.name}",
                                "course_name": c.name,
                                "message": f"{label}时间 {start:%m-%d %H:%M} ~ {end:%H:%M}"
                                if start and end
                                else f"课程{'开始' if action == 'signin' else '结束'}时间 {reminder_start:%m-%d %H:%M}",
                            },
                            send_at=(
                                reminder_start - timedelta(minutes=advance)
                            ).astimezone(UTC_TZ),
                            expires_at=reminder_end.astimezone(UTC_TZ),
                            clock=clk,
                        )
                    if (
                        auto_schedule
                        and auto_sign_enabled
                        and c.has_autonomous_sign
                        and start
                        and end
                        and start < end
                        and now_sh < end
                    ):
                        plan_course_schedule(
                            session,
                            registry,
                            name=f"博雅自主{label}: {c.name}",
                            task_type=f"boya.auto_{action}",
                            parameters={"course_id": cid, "course_name": c.name},
                            trigger_type="window",
                            trigger_config={
                                "start_at": start.strftime("%Y-%m-%d %H:%M:%S"),
                                "end_at": end.strftime("%Y-%m-%d %H:%M:%S"),
                                "poll_interval_seconds": poll_interval,
                            },
                            business_key=business_key,
                            enabled=True,
                            clock=clk,
                        )

        snap = session.get(BusinessCacheSnapshot, "boya_statistics")
        stats_payload = {
            "valid_count": stats.valid_count,
            "categories": [
                {
                    "category": cs.category,
                    "sub_category": cs.sub_category,
                    "required_count": cs.required_count,
                    "passed_count": cs.passed_count,
                }
                for cs in stats.categories
            ],
            "raw": stats.raw,
        }
        if snap is None:
            session.add(
                BusinessCacheSnapshot(
                    key="boya_statistics", payload_json=stats_payload, synced_at=now_utc
                )
            )
        else:
            snap.payload_json = stats_payload
            snap.synced_at = now_utc

        finish_course_refresh(session, "boya", auto_schedule=auto_schedule)
        mod_cfg = _get_module_config(session, "boya")
        mod_cfg.health_status = "healthy"
        mod_cfg.last_error = None
        mod_cfg.last_checked_at = now_utc

        session.commit()

    return courses, chosen, stats
