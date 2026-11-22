from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from app.api.deps import require_admin
from app.buaa.exceptions import CaptchaRequiredError, LoginFailedError
from app.core.network import VALID_NETWORK_MODES, NetworkMode, get_unified_network_mode
from app.core.time import format_shanghai_iso
from app.db.models import CampusAccount
from app.modules.automation import refresh_enabled_modules
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/account", tags=["account"])


class AccountUpdatePayload(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=64)
    password: str | None = Field(default=None, max_length=128)
    campus: Literal["北京", "学院路", "沙河", "杭州"] = "北京"
    enabled: bool = True


class CaptchaSubmitPayload(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)


class LoginRequestPayload(BaseModel):
    network_mode: NetworkMode | None = None


def _serialize_account(account: CampusAccount) -> dict[str, Any]:
    return {
        "student_id": account.student_id,
        "has_password": bool(account.encrypted_password),
        "campus": account.campus,
        "display_name": account.display_name,
        "enabled": account.enabled,
        "auth_status": account.auth_status,
        "auth_error": account.auth_error,
        "captcha_required": account.auth_status == "captcha_required",
        "captcha_id": account.captcha_id,
        "captcha_type": account.captcha_type,
        "has_captcha_image": bool(
            account.captcha_image_path and Path(account.captcha_image_path).exists()
        ),
        "last_login_at": format_shanghai_iso(account.last_login_at),
        "updated_at": format_shanghai_iso(account.updated_at),
    }


def _preferred_network_mode(container: Any) -> str:
    with container.session_factory() as session:
        return get_unified_network_mode(session)


@router.get("")
def get_account(container: Any = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        account = session.query(CampusAccount).first()
        if account is None:
            account = CampusAccount(
                student_id="", campus="北京", auth_status="unconfigured"
            )
            session.add(account)
            session.commit()
            session.refresh(account)
        return _serialize_account(account)


@router.put("")
def update_account(
    payload: AccountUpdatePayload,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    with container.session_factory() as session:
        account = session.query(CampusAccount).first()
        if account is None:
            account = CampusAccount()
            session.add(account)
        student_changed = account.student_id != payload.student_id.strip()
        account.student_id = payload.student_id.strip()
        if payload.password:
            account.encrypted_password = container.cipher.encrypt(payload.password)
        account.campus = payload.campus
        account.enabled = payload.enabled
        if student_changed or payload.password:
            account.auth_status = (
                "configured" if account.encrypted_password else "unconfigured"
            )
            account.auth_error = None
        session.commit()
        session.refresh(account)
    if student_changed or payload.password:
        for mode in VALID_NETWORK_MODES:
            container.session_mgr.clear_domain_state(f"sso:{mode}")
            container.session_mgr.clear_domain_state(f"iclass:{mode}")
            container.session_mgr.clear_domain_state(f"ygdk:{mode}")
            container.session_mgr.clear_domain_state(f"bykc:{mode}")
            container.session_mgr.clear_domain_state(f"vpn_pending:{mode}")
            container.session_mgr.clear_domain_state(f"sso_pending:{mode}")
    return _serialize_account(account)


@router.post("/login")
async def login_account(
    payload: LoginRequestPayload | None = None,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    mode = (
        payload.network_mode if payload and payload.network_mode else None
    ) or _preferred_network_mode(container)
    try:
        info = await container.session_mgr.login_sso(network_mode=mode)
        with container.session_factory() as session:
            refresh_enabled_modules(session, container.registry, container.clock)
            session.commit()
            account = session.query(CampusAccount).first()
            return {
                "ok": True,
                "status": "authenticated",
                "display_name": info.get("name")
                or (account.display_name if account else ""),
                "account": _serialize_account(account) if account else None,
            }
    except CaptchaRequiredError as exc:
        with container.session_factory() as session:
            account = session.query(CampusAccount).first()
            return {
                "ok": False,
                "status": "captcha_required",
                "message": str(exc),
                "account": _serialize_account(account) if account else None,
            }
    except LoginFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"认证请求异常: {exc}"
        ) from exc


@router.get("/captcha/image")
def get_captcha_image(container: Any = Depends(require_admin)) -> FileResponse:
    with container.session_factory() as session:
        account = session.query(CampusAccount).first()
        if account is None or not account.captcha_image_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="当前没有待处理的验证码"
            )
        path = Path(account.captcha_image_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="验证码图片文件不存在"
        )
    return FileResponse(path)


@router.post("/captcha")
async def submit_captcha(
    payload: CaptchaSubmitPayload,
    container: Any = Depends(require_admin),
) -> dict[str, Any]:
    mode = _preferred_network_mode(container)
    try:
        info = await container.session_mgr.login_sso(
            network_mode=mode, captcha_code=payload.code.strip()
        )
        with container.session_factory() as session:
            refresh_enabled_modules(session, container.registry, container.clock)
            session.commit()
            account = session.query(CampusAccount).first()
            return {
                "ok": True,
                "status": "authenticated",
                "display_name": info.get("name") or "",
                "account": _serialize_account(account) if account else None,
            }
    except CaptchaRequiredError as exc:
        with container.session_factory() as session:
            account = session.query(CampusAccount).first()
            return {
                "ok": False,
                "status": "captcha_required",
                "message": str(exc),
                "account": _serialize_account(account) if account else None,
            }
    except LoginFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
