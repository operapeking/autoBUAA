from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.container import AppContainer
from app.core.config import Settings
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock
from app.main import create_app
from fastapi.testclient import TestClient


def _make_test_client(tmp_path: Path) -> tuple[TestClient, AppContainer]:
    clock = Clock(
        datetime(2026, 11, 15, 10, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    settings = Settings(
        data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
    )
    container = AppContainer(settings=settings, clock=clock, run_migrations=True)
    app = create_app(container=container, start_scheduler=False)
    return TestClient(app), container


def test_frontend_dist_serving_and_health(tmp_path: Path) -> None:
    repo_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (repo_dist / "index.html").is_file():
        dist = tmp_path / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(
            "<html><body><div id='app'>autoBUAA</div></body></html>", encoding="utf-8"
        )
    else:
        dist = repo_dist

    clock = Clock(
        datetime(2026, 11, 15, 10, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    settings = Settings(data_dir=tmp_path / "data", frontend_dist_dir=dist)
    container = AppContainer(settings=settings, clock=clock, run_migrations=True)
    app = create_app(container=container, start_scheduler=False)
    client = TestClient(app)

    root_res = client.get("/")
    assert root_res.status_code == 200
    assert "text/html" in root_res.headers.get("content-type", "")
    assert "autoBUAA" in root_res.text

    health_res = client.get("/api/v1/health")
    assert health_res.status_code == 200
    assert health_res.json()["database"] == "connected"
    assert client.get("/health").status_code == 200
