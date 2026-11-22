from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from app.core.network import get_unified_network_mode
from app.modules.config import MODULE_SETTINGS
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from .models import (
    CampusAccount,
    ModuleConfig,
    NotificationChannel,
    NotificationRule,
    SystemSetting,
)

DEFAULT_MODULES: list[dict[str, Any]] = [
    {
        "module": module,
        "enabled": False,
        "network_mode": "webvpn",
        "config_json": schema().model_dump(),
        "health_status": "unknown",
    }
    for module, schema in MODULE_SETTINGS.items()
]

DEFAULT_SETTINGS: dict[str, Any] = {"network_mode": "webvpn"}

DEFAULT_RULES: list[tuple[str, str, int]] = [
    ("exercise_reminder", "ygdk", 0),
    ("signup_reminder", "boya", 5),
    ("signin_reminder", "classroom", 15),
    ("signin_reminder", "boya", 15),
    ("signout_reminder", "boya", 15),
    ("task_succeeded", "*", 0),
    ("task_failed", "*", 0),
    ("outcome_unknown", "*", 0),
    ("auth_captcha_required", "*", 0),
]


def run_migrations(db_url: str) -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    alembic_ini = backend_dir / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def seed_defaults(session: Session) -> None:
    if session.get(SystemSetting, "network_mode") is None:
        derived_mode = get_unified_network_mode(session)
        session.add(SystemSetting(key="network_mode", value_json=derived_mode))
        for mod_row in session.query(ModuleConfig).all():
            mod_row.network_mode = derived_mode
    legacy_env = session.get(SystemSetting, "network_environment")
    if legacy_env is not None:
        session.delete(legacy_env)
    session.flush()

    for key, value in DEFAULT_SETTINGS.items():
        if session.get(SystemSetting, key) is None:
            session.add(SystemSetting(key=key, value_json=value))

    session.flush()

    if session.query(CampusAccount).first() is None:
        session.add(
            CampusAccount(
                student_id="",
                campus="北京",
                enabled=True,
                auth_status="unconfigured",
            )
        )

    for mod in DEFAULT_MODULES:
        if session.get(ModuleConfig, mod["module"]) is None:
            session.add(ModuleConfig(**mod))

    classroom_mod = session.get(ModuleConfig, "classroom")
    if classroom_mod is not None:
        cfg = dict(classroom_mod.config_json or {})
        if "signin_offset_minutes" not in cfg:
            if "advance_minutes" in cfg and cfg["advance_minutes"] is not None:
                legacy = int(cfg.pop("advance_minutes"))
                cfg["signin_offset_minutes"] = legacy if legacy <= 0 else -legacy
            else:
                cfg["signin_offset_minutes"] = -8
            classroom_mod.config_json = cfg

    default_channel = session.query(NotificationChannel).first()
    if default_channel is None:
        default_channel = NotificationChannel(
            channel_type="bark",
            name="默认 Bark 推送",
            enabled=False,
            server_url="https://api.day.app",
            encrypted_device_key=None,
            extra_json={"group": "autoBUAA"},
        )
        session.add(default_channel)
        session.flush()

    for event_type, module, advance_minutes in DEFAULT_RULES:
        exists = (
            session.query(NotificationRule)
            .filter_by(event_type=event_type, module=module)
            .first()
        )
        if exists is None:
            session.add(
                NotificationRule(
                    event_type=event_type,
                    module=module,
                    enabled=True,
                    advance_minutes=advance_minutes,
                    channel_id=default_channel.id,
                )
            )

    for rule in session.query(NotificationRule).all():
        if (rule.event_type, rule.module) not in {
            (event, module) for event, module, _ in DEFAULT_RULES
        }:
            session.delete(rule)

    boya_rem_migrated = session.get(SystemSetting, "boya_signup_reminder_5m_v1")
    if boya_rem_migrated is None:
        boya_signup_rule = (
            session.query(NotificationRule)
            .filter_by(event_type="signup_reminder", module="boya")
            .first()
        )
        if boya_signup_rule is not None and boya_signup_rule.advance_minutes == 15:
            boya_signup_rule.advance_minutes = 5
        session.add(SystemSetting(key="boya_signup_reminder_5m_v1", value_json=True))

    session.commit()


def init_database(engine: Engine, db_url: str, session_factory: Any) -> None:
    run_migrations(db_url)
    with session_factory() as session:
        seed_defaults(session)
