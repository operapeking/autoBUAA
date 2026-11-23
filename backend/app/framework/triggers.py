from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Literal

from app.core.time import (
    SHANGHAI_TZ,
    UTC_TZ,
    ensure_utc,
    format_shanghai_iso,
    parse_shanghai_datetime,
)
from pydantic import BaseModel, Field


class TriggerValidationError(ValueError):
    """Field-level trigger configuration error."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


TriggerType = Literal["once", "daily", "interval", "window"]


class TriggerSpec(BaseModel):
    trigger_type: TriggerType
    run_at: str | None = None
    expires_at: str | None = None
    time_of_day: str | None = Field(default=None, alias="time")
    interval_seconds: int | None = None
    start_at: str | None = None
    end_at: str | None = None
    poll_interval_seconds: int = 30

    model_config = {"populate_by_name": True}


def _parse_time_of_day(value: str | None, field: str) -> time:
    if value is None or value.strip() == "":
        raise TriggerValidationError(field, "必须指定执行时刻（HH:MM 或 HH:MM:SS）")
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise TriggerValidationError(field, f"时间格式无效（应为 HH:MM[:SS]）: {value}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise TriggerValidationError(field, f"时间值超出范围: {value}") from exc


def _parse_shanghai_dt(value: str | None, field: str) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    try:
        return parse_shanghai_datetime(value)
    except ValueError as exc:
        raise TriggerValidationError(field, f"日期时间格式无效: {value}") from exc


def validate_trigger(trigger_type: str, config: dict[str, Any]) -> TriggerSpec:
    if trigger_type not in {"once", "daily", "interval", "window"}:
        raise TriggerValidationError(
            "trigger_type", f"不支持的计划类型: {trigger_type}"
        )

    merged = dict(config or {})
    merged["trigger_type"] = trigger_type
    try:
        spec = TriggerSpec.model_validate(merged)
    except Exception as exc:
        raise TriggerValidationError("trigger_config", str(exc)) from exc

    if trigger_type == "once":
        run_at = _parse_shanghai_dt(spec.run_at, "run_at")
        if run_at is None:
            raise TriggerValidationError("run_at", "单次计划必须指定执行时间")
        exp = _parse_shanghai_dt(spec.expires_at, "expires_at")
        if exp is not None and exp <= run_at:
            raise TriggerValidationError(
                "expires_at", "单次计划截止时间必须晚于执行时间"
            )

    elif trigger_type == "daily":
        _parse_time_of_day(spec.time_of_day, "time")

    elif trigger_type == "interval":
        if spec.interval_seconds is None or spec.interval_seconds < 5:
            raise TriggerValidationError("interval_seconds", "固定间隔不能小于 5 秒")
        if spec.start_at:
            _parse_shanghai_dt(spec.start_at, "start_at")

    elif trigger_type == "window":
        start_at = _parse_shanghai_dt(spec.start_at, "start_at")
        end_at = _parse_shanghai_dt(spec.end_at, "end_at")
        if start_at is None:
            raise TriggerValidationError("start_at", "时间窗口计划必须指定开始时间")
        if end_at is None:
            raise TriggerValidationError("end_at", "时间窗口计划必须指定结束时间")
        if end_at <= start_at:
            raise TriggerValidationError("end_at", "窗口结束时间必须晚于开始时间")
        if spec.poll_interval_seconds < 5:
            raise TriggerValidationError(
                "poll_interval_seconds", "窗口轮询间隔不能小于 5 秒"
            )
    return spec


def compute_next_run(
    trigger_type: str,
    config: dict[str, Any],
    after_utc: datetime,
    inclusive: bool = True,
) -> datetime | None:
    """Compute next scheduled UTC execution >= (or >) after_utc."""
    spec = validate_trigger(trigger_type, config)
    after = ensure_utc(after_utc)

    if trigger_type == "once":
        run_at = _parse_shanghai_dt(spec.run_at, "run_at")
        assert run_at is not None
        exp = _parse_shanghai_dt(spec.expires_at, "expires_at") or (
            run_at + timedelta(minutes=5)
        )
        return run_at if inclusive and after < exp else None

    if trigger_type == "daily":
        tod = _parse_time_of_day(spec.time_of_day, "time")
        sh_anchor = after.astimezone(SHANGHAI_TZ)
        candidate_date = sh_anchor.date()
        candidate_sh = datetime.combine(candidate_date, tod, tzinfo=SHANGHAI_TZ)
        cmp_ok = candidate_sh >= sh_anchor if inclusive else candidate_sh > sh_anchor
        if not cmp_ok:
            candidate_sh = datetime.combine(
                candidate_date + timedelta(days=1), tod, tzinfo=SHANGHAI_TZ
            )
        return candidate_sh.astimezone(UTC_TZ)

    if trigger_type == "interval":
        interval = timedelta(seconds=spec.interval_seconds or 60)
        if spec.start_at:
            base = _parse_shanghai_dt(spec.start_at, "start_at")
            assert base is not None
            if after <= base:
                candidate = base
            else:
                elapsed = (after - base).total_seconds()
                steps = int(elapsed // interval.total_seconds())
                candidate = base + interval * steps
                if (candidate < after) or (not inclusive and candidate <= after):
                    candidate += interval
        else:
            candidate = after if inclusive else (after + interval)
        return candidate

    if trigger_type == "window":
        start_at = _parse_shanghai_dt(spec.start_at, "start_at")
        end_at = _parse_shanghai_dt(spec.end_at, "end_at")
        assert start_at is not None and end_at is not None
        if after >= end_at:
            return None
        if after < start_at or (inclusive and after == start_at):
            return start_at
        step = spec.poll_interval_seconds
        elapsed = (after - start_at).total_seconds()
        candidate = start_at + timedelta(seconds=int(elapsed // step) * step)
        if (candidate < after) or (not inclusive and candidate <= after):
            candidate += timedelta(seconds=step)
        if candidate >= end_at:
            return None
        return candidate

    return None


def compute_deadline(
    trigger_type: str,
    config: dict[str, Any],
    scheduled_for_utc: datetime,
) -> datetime | None:
    """Compute execution validity deadline (UTC) for a trigger instance."""
    spec = validate_trigger(trigger_type, config)
    sched = ensure_utc(scheduled_for_utc)
    if trigger_type == "once":
        exp = _parse_shanghai_dt(spec.expires_at, "expires_at")
        return exp if exp is not None else (sched + timedelta(minutes=5))
    if trigger_type == "window":
        return _parse_shanghai_dt(spec.end_at, "end_at")
    if trigger_type == "interval" and spec.interval_seconds:
        return sched + timedelta(seconds=max(60, spec.interval_seconds))
    return sched + timedelta(minutes=15)


def preview_next_runs(
    trigger_type: str,
    config: dict[str, Any],
    now_utc: datetime,
    count: int = 10,
) -> list[str]:
    """Return up to `count` future execution times formatted in Asia/Shanghai ISO."""
    validate_trigger(trigger_type, config)
    runs: list[str] = []
    cursor = ensure_utc(now_utc)
    for idx in range(count):
        nxt = compute_next_run(trigger_type, config, cursor, inclusive=(idx == 0))
        if nxt is None:
            break
        iso = format_shanghai_iso(nxt)
        if iso:
            runs.append(iso)
        if trigger_type == "once":
            break
        cursor = nxt
    return runs
