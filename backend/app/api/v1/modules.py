from __future__ import annotations

from typing import Any

from app.api.deps import require_admin
from app.container import AppContainer
from app.core.time import format_shanghai_iso
from app.db.models import ModuleConfig
from app.modules.automation import reconcile_automation
from app.modules.config import MODULE_SETTINGS
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

router = APIRouter(prefix="/modules", tags=["modules"])


class ModuleUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    config_json: dict[str, Any] = Field(default_factory=dict)


def _serialize_module(mod: ModuleConfig) -> dict[str, Any]:
    return {
        "module": mod.module,
        "enabled": mod.enabled,
        "config_json": mod.config_json,
        "health_status": mod.health_status,
        "last_error": mod.last_error,
        "last_checked_at": format_shanghai_iso(mod.last_checked_at),
    }


@router.get("")
def list_modules(
    container: AppContainer = Depends(require_admin),
) -> list[dict[str, Any]]:
    with container.session_factory() as session:
        mods = session.query(ModuleConfig).order_by(ModuleConfig.module.asc()).all()
        return [_serialize_module(m) for m in mods]


@router.put("/{module}")
def update_module(
    module: str,
    payload: ModuleUpdatePayload,
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        mod = session.get(ModuleConfig, module)
        if mod is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"未知模块: {module}")
        try:
            config = (
                MODULE_SETTINGS[module]
                .model_validate({**mod.config_json, **payload.config_json})
                .model_dump()
            )
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, exc.errors(include_context=False)
            ) from exc
        if mod.config_json != config or mod.enabled != payload.enabled:
            mod.enabled = payload.enabled
            mod.config_json = config
            reconcile_automation(
                session, container.registry, container.clock, changed=module
            )
            session.commit()
        return _serialize_module(mod)
