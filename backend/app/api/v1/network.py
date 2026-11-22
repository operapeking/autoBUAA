from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.api.deps import require_admin
from app.buaa.boya import BoyaClient
from app.buaa.classroom import ClassroomClient
from app.buaa.td import TdClient
from app.buaa.webvpn import detect_campus_gateway, diagnose_host_connection
from app.buaa.ygdk import YgdkClient
from app.core.network import (
    NetworkMode,
    apply_unified_network_mode,
    get_unified_network_mode,
)
from app.core.time import format_shanghai_iso
from app.db.models import CampusAccount, ModuleConfig
from app.modules.automation import reconcile_automation, refresh_enabled_modules
from app.modules.config import MODULE_SETTINGS
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

router = APIRouter(prefix="/network", tags=["network"])


class NetworkModePayload(BaseModel):
    network_mode: NetworkMode


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
def get_network_and_modules(container: Any = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        net_mode = get_unified_network_mode(session)
        mods = session.query(ModuleConfig).order_by(ModuleConfig.module.asc()).all()
        return {
            "network_mode": net_mode,
            "modules": [_serialize_module(m) for m in mods],
        }


@router.put("/mode")
def update_network_mode(
    payload: NetworkModePayload,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        previous_mode = get_unified_network_mode(session)
        net_mode = apply_unified_network_mode(
            session,
            network_mode=payload.network_mode,
        )
        if previous_mode != net_mode:
            refresh_enabled_modules(session, container.registry, container.clock)
        session.commit()
        mods = session.query(ModuleConfig).order_by(ModuleConfig.module.asc()).all()
        return {
            "ok": True,
            "network_mode": net_mode,
            "modules": [_serialize_module(m) for m in mods],
        }


@router.put("/modules/{module}")
def update_module(
    module: str,
    payload: ModuleUpdatePayload,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        mod = session.get(ModuleConfig, module)
        if mod is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"未知模块: {module}"
            )
        net_mode = get_unified_network_mode(session)
        was_enabled = mod.enabled
        mod.network_mode = net_mode
        merged = dict(mod.config_json or {})
        merged.update(payload.config_json or {})
        try:
            config = MODULE_SETTINGS[module].model_validate(merged).model_dump()
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_context=False)
            ) from exc
        if mod.config_json == config and was_enabled == payload.enabled:
            return _serialize_module(mod)
        mod.enabled = payload.enabled
        mod.config_json = config
        reconcile_automation(
            session, container.registry, container.clock, changed=module
        )
        session.commit()
        session.refresh(mod)
        return _serialize_module(mod)


@router.post("/diagnose")
def run_network_diagnostics(container: Any = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        net_mode = get_unified_network_mode(session)
        mods = {m.module: m for m in session.query(ModuleConfig).all()}
        account = session.query(CampusAccount).first()
        auth_status = account.auth_status if account else "unconfigured"

    campus_detected = detect_campus_gateway()
    sso_diag = diagnose_host_connection("https://sso.buaa.edu.cn/login", net_mode)

    module_results: dict[str, Any] = {}
    for name in mods:
        if name == "classroom":
            diag = ClassroomClient(container.session_mgr, net_mode).diagnose()
        elif name == "ygdk":
            diag = YgdkClient(container.session_mgr, net_mode).diagnose()
        elif name == "boya":
            diag = BoyaClient(container.session_mgr, net_mode).diagnose()
        elif name == "td":
            diag = TdClient(container.session_mgr, net_mode).diagnose()
        else:
            continue
        module_results[name] = asdict(diag)

    now_utc = container.clock.now_utc()
    with container.session_factory() as session:
        for name, res in module_results.items():
            row = session.get(ModuleConfig, name)
            if row:
                row.last_checked_at = now_utc
                if res["failed_step"] is None:
                    row.health_status = "healthy"
                    row.last_error = None
                else:
                    row.health_status = "error"
                    row.last_error = res["message"]
        session.commit()

    return {
        "network_mode": net_mode,
        "campus_gateway_detected": campus_detected,
        "auth_status": auth_status,
        "sso": asdict(sso_diag),
        "modules": module_results,
    }
