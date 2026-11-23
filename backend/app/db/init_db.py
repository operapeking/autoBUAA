from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from app.core.network import DEFAULT_NETWORK_MODE
from app.modules.config import MODULE_SETTINGS
from sqlalchemy.orm import Session

from .models import (
    CampusAccount,
    ModuleConfig,
    NotificationChannel,
    NotificationRule,
    SystemSetting,
)

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
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def seed_defaults(session: Session) -> None:
    """Insert missing default rows and drop notification rules no longer defined in code."""
    if session.get(SystemSetting, "network_mode") is None:
        session.add(SystemSetting(key="network_mode", value_json=DEFAULT_NETWORK_MODE))

    if session.query(CampusAccount).first() is None:
        session.add(CampusAccount())

    for module, schema in MODULE_SETTINGS.items():
        if session.get(ModuleConfig, module) is None:
            session.add(
                ModuleConfig(
                    module=module, enabled=False, config_json=schema().model_dump()
                )
            )

    channel = session.query(NotificationChannel).first()
    if channel is None:
        channel = NotificationChannel(
            name="默认 Bark 推送", enabled=False, extra_json={"group": "autoBUAA"}
        )
        session.add(channel)
        session.flush()

    defined = {(event_type, module) for event_type, module, _ in DEFAULT_RULES}
    existing = {}
    for rule in session.query(NotificationRule).all():
        if (rule.event_type, rule.module) in defined:
            existing[(rule.event_type, rule.module)] = rule
        else:
            session.delete(rule)
    for event_type, module, advance_minutes in DEFAULT_RULES:
        if (event_type, module) not in existing:
            session.add(
                NotificationRule(
                    event_type=event_type,
                    module=module,
                    enabled=True,
                    advance_minutes=advance_minutes,
                    channel_id=channel.id,
                )
            )

    session.commit()
