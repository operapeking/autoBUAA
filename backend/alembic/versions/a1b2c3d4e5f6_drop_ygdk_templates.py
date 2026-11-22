"""drop ygdk_templates and migrate to ygdk.clockin schedules

Revision ID: a1b2c3d4e5f6
Revises: eb48a779e889
Create Date: 2026-11-16 13:45:00.000000
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "eb48a779e889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "ygdk_templates" in inspector.get_table_names():
        tpl_rows = conn.execute(
            sa.text(
                "SELECT id, item_id, item_name, place, start_time_str, end_time_str, "
                "photo_id, share_to_square FROM ygdk_templates"
            )
        ).fetchall()
        tpl_map = {
            row[0]: {
                "item_id": row[1],
                "item_name": row[2] or "",
                "place": row[3] or "操场",
                "start_time_str": row[4] or "08:00",
                "end_time_str": row[5] or "09:00",
                "photo_id": row[6],
                "share_to_square": bool(row[7]),
            }
            for row in tpl_rows
        }

        sched_rows = conn.execute(
            sa.text(
                "SELECT id, parameters_json FROM task_schedules WHERE task_type = 'ygdk.template_clockin'"
            )
        ).fetchall()
        for sched_id, raw_params in sched_rows:
            params = json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
            tpl_id = params.get("template_id")
            inline_params = tpl_map.get(
                tpl_id,
                {
                    "item_id": None,
                    "item_name": "",
                    "place": "操场",
                    "start_time_str": "08:00",
                    "end_time_str": "09:00",
                    "photo_id": None,
                    "share_to_square": False,
                },
            )
            conn.execute(
                sa.text(
                    "UPDATE task_schedules "
                    "SET task_type = 'ygdk.clockin', parameters_json = :params, business_key = NULL "
                    "WHERE id = :id"
                ),
                {"id": sched_id, "params": json.dumps(inline_params, ensure_ascii=False)},
            )

        conn.execute(
            sa.text("DELETE FROM task_definitions WHERE task_type = 'ygdk.template_clockin'")
        )
        op.drop_table("ygdk_templates")


def downgrade() -> None:
    pass
