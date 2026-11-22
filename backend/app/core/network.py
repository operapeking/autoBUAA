from __future__ import annotations

from typing import Literal

from app.db.models import ModuleConfig, SystemSetting
from sqlalchemy.orm import Session

NetworkMode = Literal["direct", "webvpn", "vpn_buaa"]
VALID_NETWORK_MODES: tuple[NetworkMode, ...] = ("direct", "webvpn", "vpn_buaa")


def get_unified_network_mode(session: Session) -> NetworkMode:
    row = session.get(SystemSetting, "network_mode")
    if row and row.value_json in VALID_NETWORK_MODES:
        return row.value_json  # type: ignore[return-value]
    legacy_env = session.get(SystemSetting, "network_environment")
    if legacy_env and legacy_env.value_json == "campus":
        return "direct"
    return "webvpn"


def apply_unified_network_mode(
    session: Session,
    *,
    network_mode: NetworkMode,
) -> NetworkMode:
    """Persist unified network channel and sync module configs."""
    mode_row = session.get(SystemSetting, "network_mode")
    if mode_row is None:
        session.add(SystemSetting(key="network_mode", value_json=network_mode))
    else:
        mode_row.value_json = network_mode

    legacy_env = session.get(SystemSetting, "network_environment")
    if legacy_env is not None:
        session.delete(legacy_env)

    for mod in session.query(ModuleConfig).all():
        mod.network_mode = network_mode

    return network_mode
