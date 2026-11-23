from __future__ import annotations

from typing import Any

from app.api.deps import get_container
from app.container import AppContainer
from app.core.network import get_network_mode
from app.core.time import format_shanghai_iso
from app.db.models import ModuleConfig
from fastapi import APIRouter, Depends
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    db_ok = False
    net_mode = "webvpn"
    modules: dict[str, Any] = {}
    try:
        with container.session_factory() as session:
            session.execute(text("SELECT 1"))
            db_ok = True
            net_mode = get_network_mode(session)
            for mod in session.query(ModuleConfig).all():
                modules[mod.module] = {
                    "enabled": mod.enabled,
                    "health_status": mod.health_status,
                    "last_error": mod.last_error,
                    "last_checked_at": format_shanghai_iso(mod.last_checked_at),
                }
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "error",
        "network_mode": net_mode,
        "scheduler": {
            "running": container.scheduler.running,
            "last_heartbeat_at": format_shanghai_iso(
                container.scheduler.last_heartbeat_at
            ),
        },
        "modules": modules,
    }
