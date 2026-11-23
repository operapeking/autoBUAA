from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from app.buaa.exceptions import BuaaError, SessionExpiredError
from app.buaa.td import TD_ENTRY_URL, TD_OVERVIEW_URL, TdClient, parse_td_overview
from app.buaa.webvpn import route_url
from app.container import AppContainer
from app.core.config import Settings
from app.core.time import SHANGHAI_TZ, Clock
from app.db.models import (
    BusinessCacheSnapshot,
    CampusAccount,
    ModuleConfig,
    TaskSchedule,
)
from app.main import create_app
from app.modules.services import sync_td_progress
from fastapi.testclient import TestClient


PAGE = """
<script>var xn = "2026-2027"; var xq = "1";</script>
<table><tr><td>学年</td><td>学期</td><td>TD考勤机(802)/次</td>
<td>奔跑在北航(804)/次</td><td>TD合计(888)(TD旧+TD考勤机+活动转换+奔跑在北航)</td>
<td>活动转换(803)/次</td></tr>
<tr><td>2025-2026</td><td>2</td><td>40</td><td>0</td><td>40</td><td>0</td></tr>
<tr><td>2026-2027</td><td>1</td><td><span>30.0(<span>查看明细</span>)</span></td>
<td>(<span>查看明细</span>)</td><td>31.5(查看明细)</td><td>1.5</td></tr></table>
"""


@pytest.fixture
def container(tmp_path: Path):
    app = AppContainer(
        settings=Settings(data_dir=tmp_path, frontend_dist_dir=tmp_path / "dist"),
        clock=Clock(datetime(2026, 11, 16, 18, tzinfo=SHANGHAI_TZ)),
        run_migrations=False,
    )
    with app.session_factory() as session:
        session.query(CampusAccount).first().student_id = "24000001"
        session.commit()
    yield app
    app.engine.dispose()


def test_parse_current_semester_and_event_columns():
    result = parse_td_overview(PAGE)
    assert result.academic_year == "2026-2027"
    assert result.semester == 1
    assert result.term_count == 31.5
    assert result.attendance_count == 30
    assert result.running_count == 0
    assert result.activity_count == 1.5


def test_missing_current_semester_preserves_unknown_counts():
    result = parse_td_overview(PAGE.replace('var xq = "1"', 'var xq = "2"'))
    assert result.term_count is None
    assert result.attendance_count is None


@pytest.mark.parametrize("value", ["未知", "-1", "NaN", "Infinity"])
def test_reject_invalid_counts(value):
    with pytest.raises(BuaaError):
        parse_td_overview(PAGE.replace("31.5(查看明细)", value))


@pytest.mark.parametrize(
    "html", ["<html>维护中</html>", PAGE.replace("TD合计(888)", "TD合计")]
)
def test_reject_missing_overview_structure(html):
    with pytest.raises(BuaaError):
        parse_td_overview(html)


@pytest.mark.parametrize("mode", ["direct", "webvpn", "vpn_buaa"])
async def test_query_uses_read_requests_and_routes_both_pages(mode):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, text=PAGE)

    class Manager:
        def build_http_client(self, network_mode, follow_redirects):
            assert network_mode == mode and follow_redirects
            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    result = await TdClient(Manager(), mode).get_overview()
    assert result.term_count == 31.5
    assert [r.method for r in requests] == ["GET", "GET"]
    assert [str(r.url) for r in requests] == [
        route_url(TD_ENTRY_URL, mode),
        route_url(TD_OVERVIEW_URL, mode),
    ]


@pytest.mark.parametrize("recovers", [True, False])
async def test_session_expiry_refreshes_once(recovers):
    login = AsyncMock()

    def handle(request):
        if not login.await_count or not recovers:
            return httpx.Response(200, text="<html><title>CAS Login</title></html>")
        return httpx.Response(200, text=PAGE)

    class Manager:
        login_sso = login

        def build_http_client(self, *_args, **_kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    if recovers:
        assert (await TdClient(Manager()).get_overview()).term_count == 31.5
    else:
        with pytest.raises(SessionExpiredError):
            await TdClient(Manager()).get_overview()
    login.assert_awaited_once_with(network_mode="webvpn")


async def test_cache_api_failure_and_account_change(container, monkeypatch):
    client = TestClient(create_app(container, start_scheduler=False))
    assert client.get("/api/v1/td/overview").status_code == 401
    assert client.post("/api/v1/td/sync").status_code == 401
    client.post("/api/v1/auth/setup", json={"password": "admin-secret"})
    assert client.get("/api/v1/td/overview").json()["synced"] is False
    monkeypatch.setattr(
        TdClient, "get_overview", AsyncMock(return_value=parse_td_overview(PAGE))
    )
    await sync_td_progress(
        container.session_factory, container.session_mgr, container.clock
    )
    result = client.get("/api/v1/td/overview").json()
    assert result["overview"]["term_count"] == 31.5
    assert result["overview"]["term_target"] == 32
    assert "student_id" not in result["overview"]
    monkeypatch.setattr(
        TdClient, "get_overview", AsyncMock(side_effect=BuaaError("健康云网络请求失败"))
    )
    assert client.post("/api/v1/td/sync").status_code == 400
    assert client.get("/api/v1/td/overview").json() == result
    with container.session_factory() as session:
        assert session.get(ModuleConfig, "td").health_status == "error"
        assert (
            session.get(BusinessCacheSnapshot, "td_overview").payload_json["term_count"]
            == 31.5
        )
        session.query(CampusAccount).first().student_id = "24000002"
        session.commit()
    assert client.get("/api/v1/td/overview").json()["synced"] is False


def test_td_configuration_controls_hourly_schedule(container):
    client = TestClient(create_app(container, start_scheduler=False))
    client.post("/api/v1/auth/setup", json={"password": "admin-secret"})
    path = "/api/v1/modules/td"
    assert (
        client.put(
            path, json={"enabled": True, "config_json": {"term_target": 40}}
        ).status_code
        == 200
    )
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule).filter_by(business_key="td:sync_overview").one()
        )
        assert schedule.trigger_config_json == {"interval_seconds": 3600}
        assert schedule.status == "enabled"
        assert schedule.next_run_at == container.clock.now_utc()
    for invalid in ({"term_target": 0}, {"auto_sign": True}):
        assert (
            client.put(path, json={"enabled": True, "config_json": invalid}).status_code
            == 422
        )
    assert (
        client.put(path, json={"enabled": False, "config_json": {}}).status_code == 200
    )
    with container.session_factory() as session:
        schedule = (
            session.query(TaskSchedule).filter_by(business_key="td:sync_overview").one()
        )
        assert schedule.status == "paused"
        assert schedule.next_run_at is None
