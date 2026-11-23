from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _alembic_config(db_path: Path) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_legacy_state_is_consolidated(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "c37d15a29b08")
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO system_settings (key, value_json, updated_at) VALUES (?, ?, '2026-01-01')",
            [
                ("network_environment", json.dumps("campus")),
                ("normal_refresh_seconds", "300"),
                ("admin_password_hash", json.dumps("hash")),
            ],
        )
        db.execute(
            "INSERT INTO module_configs (module, enabled, network_mode, config_json, "
            "health_status, updated_at) VALUES ('classroom', 1, 'direct', ?, 'unknown', "
            "'2026-01-01')",
            (json.dumps({"auto_sign": True, "advance_minutes": 8}),),
        )
        db.execute(
            "INSERT INTO notification_rules (event_type, module, enabled, advance_minutes, "
            "created_at, updated_at) VALUES ('signup_reminder', 'boya', 1, 15, "
            "'2026-01-01', '2026-01-01')"
        )

    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as db:
        settings = dict(db.execute("SELECT key, value_json FROM system_settings"))
        assert settings == {
            "network_mode": json.dumps("direct"),
            "admin_password_hash": json.dumps("hash"),
        }
        config = json.loads(
            db.execute(
                "SELECT config_json FROM module_configs WHERE module = 'classroom'"
            ).fetchone()[0]
        )
        assert config == {"auto_sign": True, "signin_offset_minutes": -5}
        assert db.execute(
            "SELECT advance_minutes FROM notification_rules WHERE module = 'boya'"
        ).fetchone() == (5,)
        columns = {row[1] for row in db.execute("PRAGMA table_info(module_configs)")}
        assert "network_mode" not in columns
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'task_definitions'"
        ).fetchall()

    command.downgrade(cfg, "c37d15a29b08")
