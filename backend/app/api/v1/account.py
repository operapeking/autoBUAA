from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from app.api.deps import require_admin
from app.buaa.exceptions import CaptchaRequiredError, LoginFailedError
from app.container import AppContainer
from app.core.network import read_network_mode
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


@router.get("")
def get_account(container: AppContainer = Depends(require_admin)) -> dict[str, Any]:
    with container.session_factory() as session:
        return _serialize_account(session.query(CampusAccount).one())


@router.put("")
def update_account(
    payload: AccountUpdatePayload, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    student_id = payload.student_id.strip()
    with container.session_factory() as session:
        account = session.query(CampusAccount).one()
        credentials_changed = account.student_id != student_id or bool(payload.password)
        account.student_id = student_id
        if payload.password:
            account.encrypted_password = container.cipher.encrypt(payload.password)
        account.campus = payload.campus
        account.enabled = payload.enabled
        if credentials_changed:
            account.auth_status = (
                "configured" if account.encrypted_password else "unconfigured"
            )
            account.auth_error = None
        session.commit()
        result = _serialize_account(account)
    if credentials_changed:
        container.session_mgr.clear_all_states()
    return result


async def _login(
    container: AppContainer, captcha_code: str | None = None
) -> dict[str, Any]:
    try:
        info = await container.session_mgr.login_sso(
            network_mode=read_network_mode(container.session_factory),
            captcha_code=captcha_code,
        )
    except CaptchaRequiredError as exc:
        with container.session_factory() as session:
            return {
                "ok": False,
                "status": "captcha_required",
                "message": str(exc),
                "account": _serialize_account(session.query(CampusAccount).one()),
            }
    except LoginFailedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"认证请求异常: {exc}"
        ) from exc
    with container.session_factory() as session:
        refresh_enabled_modules(session, container.registry, container.clock)
        session.commit()
        account = session.query(CampusAccount).one()
        return {
            "ok": True,
            "status": "authenticated",
            "display_name": info.get("name") or account.display_name or "",
            "account": _serialize_account(account),
        }


@router.post("/login")
async def login_account(
    container: AppContainer = Depends(require_admin),
) -> dict[str, Any]:
    return await _login(container)


@router.get("/captcha/image")
def get_captcha_image(container: AppContainer = Depends(require_admin)) -> FileResponse:
    with container.session_factory() as session:
        image_path = session.query(CampusAccount).one().captcha_image_path
    if not image_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "当前没有待处理的验证码")
    path = Path(image_path)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "验证码图片文件不存在")
    return FileResponse(path)


@router.post("/captcha")
async def submit_captcha(
    payload: CaptchaSubmitPayload, container: AppContainer = Depends(require_admin)
) -> dict[str, Any]:
    return await _login(container, payload.code.strip())
