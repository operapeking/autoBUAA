from __future__ import annotations

from typing import Any

from app.api.deps import require_admin, run_module_sync
from app.container import AppContainer
from app.core.time import format_shanghai_iso
from app.db.models import BusinessCacheSnapshot, CampusAccount, ModuleConfig
from app.modules.services import sync_td_progress
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/td", tags=["td"])


@router.get("/overview")
def get_overview(container: AppContainer = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        snapshot = session.get(BusinessCacheSnapshot, "td_overview")
        account = session.query(CampusAccount).first()
        # The snapshot belongs to whichever account synced it; hide it after an account switch.
        if (
            snapshot is None
            or not account
            or snapshot.payload_json.get("student_id") != account.student_id
        ):
            return {"synced": False, "overview": None, "synced_at": None}
        mod = session.get(ModuleConfig, "td")
        overview = {k: v for k, v in snapshot.payload_json.items() if k != "student_id"}
        overview["term_target"] = mod.config_json.get("term_target", 32) if mod else 32
        return {
            "synced": True,
            "overview": overview,
            "synced_at": format_shanghai_iso(snapshot.synced_at),
        }


@router.post("/sync")
async def sync_overview(
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    overview = await run_module_sync(
        container,
        "td",
        lambda: sync_td_progress(
            container.session_factory, container.session_mgr, container.clock
        ),
    )
    return {"ok": True, "term_count": overview.term_count}
