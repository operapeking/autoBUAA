"""consolidate legacy runtime state and drop redundant storage

Revision ID: d4f8a2c1b7e3
Revises: c37d15a29b08
"""

from __future__ import annotations

import json

import app.db.base
import sqlalchemy as sa
from alembic import op

revision = "d4f8a2c1b7e3"
down_revision = "c37d15a29b08"
branch_labels = None
depends_on = None

KEPT_SETTINGS = ("network_mode", "admin_password_hash")


def _load(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _migrate_network_mode(conn) -> None:
    settings = {
        key: _load(value)
        for key, value in conn.execute(
            sa.text("SELECT key, value_json FROM system_settings")
        )
    }
    if settings.get("network_mode") not in ("direct", "webvpn", "vpn_buaa"):
        mode = "direct" if settings.get("network_environment") == "campus" else "webvpn"
        conn.execute(sa.text("DELETE FROM system_settings WHERE key = 'network_mode'"))
        conn.execute(
            sa.text(
                "INSERT INTO system_settings (key, value_json, updated_at) "
                "VALUES ('network_mode', :value, CURRENT_TIMESTAMP)"
            ),
            {"value": json.dumps(mode)},
        )
    if "boya_signup_reminder_5m_v1" not in settings:
        conn.execute(
            sa.text(
                "UPDATE notification_rules SET advance_minutes = 5 "
                "WHERE event_type = 'signup_reminder' AND module = 'boya' "
                "AND advance_minutes = 15"
            )
        )
    conn.execute(
        sa.text("DELETE FROM system_settings WHERE key NOT IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": list(KEPT_SETTINGS)},
    )


def _migrate_classroom_offset(conn) -> None:
    row = conn.execute(
        sa.text("SELECT config_json FROM module_configs WHERE module = 'classroom'")
    ).fetchone()
    if row is None:
        return
    config = dict(_load(row[0]) or {})
    offset = config.pop("offset_minutes", None)
    legacy_advance = config.pop("advance_minutes", None)
    if config.get("signin_offset_minutes") is None:
        if offset is not None:
            config["signin_offset_minutes"] = int(offset)
        elif legacy_advance is not None:
            config["signin_offset_minutes"] = -abs(int(legacy_advance))
    if config.get("signin_offset_minutes") is not None:
        config["signin_offset_minutes"] = max(
            -5, min(29, int(config["signin_offset_minutes"]))
        )
    conn.execute(
        sa.text(
            "UPDATE module_configs SET config_json = :config WHERE module = 'classroom'"
        ),
        {"config": json.dumps(config, ensure_ascii=False)},
    )


def upgrade() -> None:
    conn = op.get_bind()
    _migrate_network_mode(conn)
    _migrate_classroom_offset(conn)
    conn.execute(
        sa.text(
            "UPDATE notification_events SET status = 'expired', next_retry_at = NULL "
            "WHERE status = 'pending' AND event_type = 'course_discovered'"
        )
    )
    with op.batch_alter_table("module_configs") as batch:
        batch.drop_column("network_mode")
    with op.batch_alter_table("campus_accounts") as batch:
        batch.drop_column("captcha_execution")
    op.drop_table("task_definitions")


def downgrade() -> None:
    op.create_table(
        "task_definitions",
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task_type"),
    )
    with op.batch_alter_table("campus_accounts") as batch:
        batch.add_column(sa.Column("captcha_execution", sa.Text(), nullable=True))
    with op.batch_alter_table("module_configs") as batch:
        batch.add_column(
            sa.Column(
                "network_mode",
                sa.String(length=16),
                nullable=False,
                server_default="webvpn",
            )
        )
