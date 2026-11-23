from __future__ import annotations

from typing import Any

from app.api.deps import require_admin, run_module_sync
from app.container import AppContainer
from app.core.time import format_shanghai_iso
from app.db.models import BusinessCacheSnapshot
from app.modules.services import sync_ygdk_progress
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/ygdk", tags=["ygdk"])


@router.get("/overview")
def get_overview(container: AppContainer = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        snap = session.get(BusinessCacheSnapshot, "ygdk_overview")
        if snap is None:
            return {"synced": False, "overview": None, "synced_at": None}
        return {
            "synced": True,
            "overview": snap.payload_json,
            "synced_at": format_shanghai_iso(snap.synced_at),
        }


@router.post("/sync")
async def sync_overview(
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    overview = await run_module_sync(
        container,
        "ygdk",
        lambda: sync_ygdk_progress(
            container.session_factory, container.session_mgr, container.clock
        ),
    )
    return {"ok": True, "term_count": overview.term_count}
