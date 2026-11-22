from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from app.api.v1 import account, auth, health, modules, network, notifications, tasks
from app.container import AppContainer
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


def create_app(
    container: AppContainer | None = None, start_scheduler: bool = True
) -> FastAPI:
    app_container = container or AppContainer()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = app_container
        if start_scheduler:
            app_container.scheduler.start()
        try:
            yield
        finally:
            if start_scheduler:
                await app_container.scheduler.stop()

    app = FastAPI(title="autoBUAA", version="0.1.0", lifespan=lifespan)
    app.state.container = app_container

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(account.router, prefix=api_prefix)
    app.include_router(network.router, prefix=api_prefix)
    app.include_router(modules.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(notifications.router, prefix=api_prefix)
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(health.router)  # Also expose /health for container healthchecks

    dist_dir = Path(app_container.settings.frontend_dist_dir)
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        def serve_spa(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)  # type: ignore[return-value]
            candidate = dist_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist_dir / "index.html")

    return app
