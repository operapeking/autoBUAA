from __future__ import annotations

from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator, TypeVar

from app.buaa.exceptions import BuaaError, CaptchaRequiredError
from app.container import AppContainer
from app.core.crypto import hash_session_token
from app.db.models import AdminSession, SystemSetting
from app.modules.services import mark_module_error
from fastapi import Depends, HTTPException, Request, status

T = TypeVar("T")

SESSION_COOKIE_NAME = "autobuaa_session"


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def is_admin_initialized(container: AppContainer) -> bool:
    with container.session_factory() as session:
        row = session.get(SystemSetting, "admin_password_hash")
        return row is not None and bool(row.value_json)


def check_admin_session(request: Request, container: AppContainer) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    token_hash = hash_session_token(token)
    now_utc = container.clock.now_utc()
    with container.session_factory() as session:
        sess = session.query(AdminSession).filter_by(token_hash=token_hash).first()
        if sess is None:
            return False
        if sess.expires_at <= now_utc:
            session.delete(sess)
            session.commit()
            return False
        return True


def require_admin(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> AppContainer:
    if not is_admin_initialized(container):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理员密码尚未初始化",
        )
    if not check_admin_session(request, container):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或会话已过期",
        )
    return container


async def run_module_sync(
    container: AppContainer, module: str, sync: Callable[[], Awaitable[T]]
) -> T:
    """Run a manual refresh under the module lock, mapping upstream errors to HTTP 400."""
    async with container.scheduler.module_lock(module):
        try:
            return await sync()
        except CaptchaRequiredError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except BuaaError as exc:
            mark_module_error(
                container.session_factory, module, str(exc), container.clock
            )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@contextmanager
def upstream_errors() -> Iterator[None]:
    try:
        yield
    except BuaaError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
