from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UTCDateTime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class CampusAccount(Base):
    __tablename__ = "campus_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    campus: Mapped[str] = mapped_column(String(32), nullable=False, default="北京")
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unconfigured"
    )
    auth_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    captcha_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    captcha_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    captcha_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class ModuleConfig(Base):
    __tablename__ = "module_configs"

    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    health_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class SessionState(Base):
    __tablename__ = "session_states"

    domain: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ClassroomCourseCache(Base):
    __tablename__ = "classroom_courses_cache"
    __table_args__ = (
        UniqueConstraint(
            "course_date", "course_sched_id", name="uq_classroom_date_sched"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_date: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    course_sched_id: Mapped[str] = mapped_column(String(128), nullable=False)
    course_name: Mapped[str] = mapped_column(String(255), nullable=False)
    class_begin_time: Mapped[str] = mapped_column(String(32), nullable=False)
    class_end_time: Mapped[str] = mapped_column(String(32), nullable=False)
    sign_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class BoyaCourseCache(Base):
    __tablename__ = "boya_courses_cache"

    course_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sub_category: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sign_method: Mapped[str] = mapped_column(
        String(64), nullable=False, default="常规签到"
    )
    has_autonomous_sign: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    chosen_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    course_start: Mapped[str | None] = mapped_column(String(32), nullable=True)
    course_end: Mapped[str | None] = mapped_column(String(32), nullable=True)
    select_start: Mapped[str | None] = mapped_column(String(32), nullable=True)
    select_end: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_end: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campus: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sign_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    sign_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pass_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class BusinessCacheSnapshot(Base):
    __tablename__ = "business_cache_snapshots"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class TaskSchedule(Base):
    __tablename__ = "task_schedules"
    __table_args__ = (
        Index("ix_task_schedules_status_next_run", "status", "next_run_at"),
        Index("ix_task_schedules_module", "module"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    business_key: Mapped[str | None] = mapped_column(
        String(191), unique=True, nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_result_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class TaskExecution(Base):
    __tablename__ = "task_executions"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "schedule_version",
            "scheduled_for",
            name="uq_exec_sched_ver_time",
        ),
        Index("ix_task_exec_status_retry", "status", "next_retry_at"),
        Index("ix_task_exec_status_resume", "status", "resume_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("task_schedules.id", ondelete="SET NULL"), nullable=True
    )
    schedule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    business_key: Mapped[str | None] = mapped_column(
        String(191), nullable=True, index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="init")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    wait_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_data_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    stages: Mapped[list["TaskStage"]] = relationship(
        "TaskStage", back_populates="execution", cascade="all, delete-orphan"
    )


class TaskStage(Base):
    __tablename__ = "task_stages"
    __table_args__ = (
        UniqueConstraint("execution_id", "stage_name", name="uq_task_stage_exec_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("task_executions.id", ondelete="CASCADE"), nullable=False
    )
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    resume_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )

    execution: Mapped[TaskExecution] = relationship(
        "TaskExecution", back_populates="stages"
    )


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="bark"
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="Bark 默认通道"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    server_url: Mapped[str] = mapped_column(
        String(512), nullable=False, default="https://api.day.app"
    )
    encrypted_device_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class NotificationRule(Base):
    __tablename__ = "notification_rules"
    __table_args__ = (
        UniqueConstraint("event_type", "module", name="uq_notif_rule_event_module"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False, default="*")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    advance_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    channel_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("notification_channels.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    __table_args__ = (Index("ix_notif_events_status_retry", "status", "next_retry_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(191), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("task_executions.id", ondelete="SET NULL"), nullable=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("task_schedules.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now
    )

    deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        "NotificationDelivery", back_populates="event", cascade="all, delete-orphan"
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("notification_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("notification_channels.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    event: Mapped[NotificationEvent] = relationship(
        "NotificationEvent", back_populates="deliveries"
    )
