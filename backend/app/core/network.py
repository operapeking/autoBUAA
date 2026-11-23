from __future__ import annotations

from typing import Any, Literal, cast

from app.db.models import SystemSetting
from sqlalchemy.orm import Session

NetworkMode = Literal["direct", "webvpn", "vpn_buaa"]
VALID_NETWORK_MODES: tuple[NetworkMode, ...] = ("direct", "webvpn", "vpn_buaa")
DEFAULT_NETWORK_MODE: NetworkMode = "webvpn"


def get_network_mode(session: Session) -> NetworkMode:
    row = session.get(SystemSetting, "network_mode")
    if row and row.value_json in VALID_NETWORK_MODES:
        return cast(NetworkMode, row.value_json)
    return DEFAULT_NETWORK_MODE


def read_network_mode(session_factory: Any) -> NetworkMode:
    with session_factory() as session:
        return get_network_mode(session)


def set_network_mode(session: Session, network_mode: NetworkMode) -> None:
    row = session.get(SystemSetting, "network_mode")
    if row is None:
        session.add(SystemSetting(key="network_mode", value_json=network_mode))
    else:
        row.value_json = network_mode
