from __future__ import annotations

from app.api.deps import require_admin
from app.container import AppContainer
from app.core.network import NetworkMode, get_network_mode, set_network_mode
from app.modules.automation import refresh_enabled_modules
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/network", tags=["network"])


class NetworkModePayload(BaseModel):
    network_mode: NetworkMode


@router.get("")
def get_network(container: AppContainer = Depends(require_admin)) -> NetworkModePayload:
    with container.session_factory() as session:
        return NetworkModePayload(network_mode=get_network_mode(session))


@router.put("")
def update_network(
    payload: NetworkModePayload, container: AppContainer = Depends(require_admin)
) -> NetworkModePayload:
    with container.session_factory() as session:
        if get_network_mode(session) != payload.network_mode:
            set_network_mode(session, payload.network_mode)
            refresh_enabled_modules(session, container.registry, container.clock)
            session.commit()
    return payload
