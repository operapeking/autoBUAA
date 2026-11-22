from __future__ import annotations

from datetime import timedelta

from app.api.deps import (
    SESSION_COOKIE_NAME,
    check_admin_session,
    get_container,
    is_admin_initialized,
    require_admin,
)
from app.container import AppContainer
from app.core.crypto import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.db.models import AdminSession, SystemSetting
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/auth", tags=["auth"])


class PasswordPayload(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)


class ChangePasswordPayload(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


def _issue_session_cookie(response: Response, container: AppContainer) -> None:
    token = generate_session_token()
    token_hash = hash_session_token(token)
    now_utc = container.clock.now_utc()
    expires_at = now_utc + timedelta(hours=container.settings.admin_session_ttl_hours)
    with container.session_factory() as session:
        session.add(
            AdminSession(
                token_hash=token_hash, expires_at=expires_at, created_at=now_utc
            )
        )
        session.commit()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=container.settings.cookie_secure,
        max_age=container.settings.admin_session_ttl_hours * 3600,
        path="/",
    )


@router.get("/status")
def auth_status(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, bool]:
    initialized = is_admin_initialized(container)
    authenticated = initialized and check_admin_session(request, container)
    return {"initialized": initialized, "authenticated": authenticated}


@router.post("/setup")
def setup_admin(
    payload: PasswordPayload,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> dict[str, bool]:
    if is_admin_initialized(container):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="管理员密码已初始化"
        )
    try:
        pw_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    with container.session_factory() as session:
        session.add(SystemSetting(key="admin_password_hash", value_json=pw_hash))
        session.commit()
    _issue_session_cookie(response, container)
    return {"ok": True, "initialized": True, "authenticated": True}


@router.post("/login")
def login_admin(
    payload: PasswordPayload,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> dict[str, bool]:
    if not is_admin_initialized(container):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请先设置管理员密码"
        )
    with container.session_factory() as session:
        row = session.get(SystemSetting, "admin_password_hash")
        stored_hash = str(row.value_json) if row else ""
    if not verify_password(payload.password, stored_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员密码错误"
        )
    _issue_session_cookie(response, container)
    return {"ok": True, "authenticated": True}


@router.post("/logout")
def logout_admin(
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        token_hash = hash_session_token(token)
        with container.session_factory() as session:
            session.query(AdminSession).filter_by(token_hash=token_hash).delete()
            session.commit()
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordPayload,
    container: AppContainer = Depends(require_admin),
) -> dict[str, bool]:
    with container.session_factory() as session:
        row = session.get(SystemSetting, "admin_password_hash")
        if row is None or not verify_password(
            payload.old_password, str(row.value_json)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="原密码不正确"
            )
        row.value_json = hash_password(payload.new_password)
        session.commit()
    return {"ok": True}
