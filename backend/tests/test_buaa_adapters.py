from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from app.buaa.boya import (
    BykcCrypto,
    course_matches_campus,
    infer_course_campus,
    parse_course,
    parse_statistics,
)
from app.buaa.classroom import parse_today_courses
from app.buaa.exceptions import BuaaError, CaptchaRequiredError
from app.buaa.sso import encrypt_sangfor_password
from app.buaa.webvpn import (
    from_sangfor_webvpn_url,
    from_webvpn_url,
    is_vpn_gateway_url,
    route_url,
    to_sangfor_webvpn_url,
    to_webvpn_url,
)
from app.buaa.ygdk import YgdkClient
from app.container import AppContainer
from app.core.config import Settings
from app.core.network import set_network_mode
from app.core.time import SHANGHAI_TZ, UTC_TZ, Clock
from app.db.models import (
    BoyaCourseCache,
    CampusAccount,
    ModuleConfig,
    NotificationEvent,
    TaskExecution,
    TaskSchedule,
)
from app.modules.services import sync_boya_all, sync_classroom_today


def test_webvpn_url_roundtrip() -> None:
    urls = [
        "https://sso.buaa.edu.cn/login",
        "https://iclass.buaa.edu.cn:8347/app/user/login.action?phone=123",
        "http://iclass.buaa.edu.cn:8081/eschool/app/course/stu_scan_sign.action",
        "https://bykc.buaa.edu.cn/sscv/cas/login",
    ]
    for url in urls:
        vpn_url = to_webvpn_url(url)
        assert vpn_url.startswith("https://d.buaa.edu.cn/")
        assert from_webvpn_url(vpn_url) == url


def test_sangfor_webvpn_url_roundtrip_and_gateway_detection() -> None:
    cases = [
        (
            "https://sso.buaa.edu.cn/login",
            "https://sso-buaa-edu-cn-s.vpn.buaa.edu.cn/login",
        ),
        (
            "https://iclass.buaa.edu.cn:8347/app/user/login.action?phone=123",
            "https://iclass-buaa-edu-cn-8347-p-s.vpn.buaa.edu.cn/app/user/login.action?phone=123",
        ),
        (
            "http://iclass.buaa.edu.cn:8081/eschool/app/course/stu_scan_sign.action",
            "https://iclass-buaa-edu-cn-8081-p.vpn.buaa.edu.cn/eschool/app/course/stu_scan_sign.action",
        ),
        (
            "https://my-service.buaa.edu.cn/path?a=1#frag",
            "https://my--service-buaa-edu-cn-s.vpn.buaa.edu.cn/path?a=1#frag",
        ),
    ]
    for direct_url, expected_vpn in cases:
        wrapped = route_url(direct_url, "vpn_buaa")
        assert wrapped == expected_vpn
        assert to_sangfor_webvpn_url(wrapped) == wrapped
        assert from_sangfor_webvpn_url(wrapped) == direct_url
        assert from_webvpn_url(wrapped) == direct_url

    # Also unwrap Sangfor /web/<type>/<scheme>/0/<host> path-proxy URLs
    assert (
        from_webvpn_url(
            "https://vpn.buaa.edu.cn/web/1/https/0/bykc.buaa.edu.cn/sscv/cas/login?k=v"
        )
        == "https://bykc.buaa.edu.cn/sscv/cas/login?k=v"
    )
    assert is_vpn_gateway_url("https://vpn.buaa.edu.cn/portal/#!/login") is True
    assert (
        is_vpn_gateway_url(
            "https://vpn.buaa.edu.cn?redirect_uri=https://sso-buaa-edu-cn-s.vpn.buaa.edu.cn/"
        )
        is True
    )
    assert (
        is_vpn_gateway_url("https://sso-buaa-edu-cn-s.vpn.buaa.edu.cn/login") is False
    )
    assert (
        is_vpn_gateway_url(
            "https://vpn.buaa.edu.cn/web/1/https/0/sso.buaa.edu.cn/login"
        )
        is False
    )


def test_bykc_crypto_roundtrip_and_statistics() -> None:
    crypto = BykcCrypto(b"1234567890abcdef")
    req = crypto.encrypt_request({"pageNumber": 1, "pageSize": 20})
    assert "Ak" in req.headers and "Sk" in req.headers and "Ts" in req.headers

    # Decrypt own encrypted ciphertext using same AES ECB key (simulated server echo)
    decoded = crypto.decrypt_response(req.body)
    assert decoded == {"pageNumber": 1, "pageSize": 20}

    stats = parse_statistics(
        {
            "validCount": 3,
            "statistical": {
                "1|博雅课程": {
                    "55|德育": {"assessmentCount": 2, "completeAssessmentCount": 2},
                    "56|美育": {"assessmentCount": 1, "completeAssessmentCount": 0},
                    "57|劳动教育": {"assessmentCount": 2, "completeAssessmentCount": 1},
                    "58|安全健康": {"assessmentCount": 1, "completeAssessmentCount": 0},
                }
            },
        }
    )
    assert stats.valid_count == 3
    counts = {c.sub_category: c.passed_count for c in stats.categories}
    assert counts["德育"] == 2
    assert counts["美育"] == 0


def test_classroom_parse_empty_and_courses() -> None:
    assert parse_today_courses({"STATUS": "2"}) == []
    courses = parse_today_courses(
        {
            "STATUS": "0",
            "result": [
                {
                    "id": "sched-1",
                    "courseName": "高等数学",
                    "classBeginTime": "2026-11-15 10:00:00",
                    "classEndTime": "2026-11-15 11:40:00",
                    "signStatus": 0,
                }
            ],
        }
    )
    assert len(courses) == 1
    assert courses[0].signed is False


async def test_sso_captcha_challenge_and_resolution(tmp_path: Path) -> None:

    def sso_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "captcha" in url:
            return httpx.Response(
                200, content=b"FAKEJPEGDATA", headers={"content-type": "image/jpeg"}
            )
        if "login" in url and request.method == "GET":
            return httpx.Response(
                200,
                text='<form id="fm1"><input type="hidden" name="execution" value="exec-1"></form>'
                "<script>config.captcha = { type: 'image', id: 'cap-123' }</script>",
            )
        if "login" in url and request.method == "POST":
            body = request.content.decode()
            if "captcha=8888" in body:
                return httpx.Response(200, text="<html>Welcome</html>")
            return httpx.Response(200, text='<div class="tip-text">验证码错误</div>')
        if "userinfo" in url:
            return httpx.Response(200, json={"data": {"name": "张三"}})
        return httpx.Response(200, text="ok")

    clock = Clock(
        datetime(2026, 11, 15, 9, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(sso_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        session.commit()

    with pytest.raises(CaptchaRequiredError):
        await container.session_mgr.login_sso(network_mode="direct")

    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None and acc.auth_status == "captcha_required"
        assert acc.captcha_id == "cap-123"
        ev = (
            session.query(NotificationEvent)
            .filter_by(event_type="auth_captcha_required")
            .first()
        )
        assert ev is not None

    info = await container.session_mgr.login_sso(
        network_mode="direct", captcha_code="8888"
    )
    assert info["name"] == "张三"
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None and acc.auth_status == "authenticated"


@pytest.mark.parametrize("network_mode", ["direct", "webvpn"])
async def test_sso_captcha_resubmission_reuses_issued_challenge(
    tmp_path: Path, network_mode: str
) -> None:
    # The SSO server issues a new captchaId on every /login render and checks
    # the submitted answer against the latest one, so re-fetching the login
    # page before posting the captcha makes every answer unverifiable.
    issued: list[str] = []

    def sso_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/captcha" in url:
            return httpx.Response(
                200,
                content=b"FAKEJPEGDATA",
                headers={"content-type": "image/jpeg"},
            )
        if "/login" in url and request.method == "GET":
            captcha_id = f"cap-{len(issued) + 1}"
            issued.append(captcha_id)
            return httpx.Response(
                200,
                text=(
                    '<form id="fm1"><input type="hidden" name="execution" '
                    f'value="exec-{len(issued)}"></form>'
                    "<script>config.captcha = { type: 'image', "
                    f"id: '{captcha_id}' }}</script>"
                ),
            )
        if "/login" in url and request.method == "POST":
            params = parse_qs(request.content.decode())
            expected = f"code{issued[-1].rsplit('-', 1)[-1]}"
            if params.get("captcha") == [expected]:
                return httpx.Response(200, text="<html>Welcome</html>")
            captcha_id = f"cap-{len(issued) + 1}"
            issued.append(captcha_id)
            return httpx.Response(
                200,
                text='<div class="tip-text">验证码错误</div>'
                "<script>config.captcha = { type: 'image', "
                f"id: '{captcha_id}' }}</script>",
            )
        if "userinfo" in url:
            return httpx.Response(200, json={"data": {"name": "张三"}})
        return httpx.Response(200, text="ok")

    clock = Clock(
        datetime(2026, 11, 15, 9, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(sso_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        session.commit()

    with pytest.raises(CaptchaRequiredError):
        await container.session_mgr.login_sso(network_mode=network_mode)
    assert issued == ["cap-1"]

    info = await container.session_mgr.login_sso(
        network_mode=network_mode, captcha_code="code1"
    )
    assert info["name"] == "张三"


async def test_classroom_timeout_enters_pending_confirm_and_recovers(
    tmp_path: Path,
) -> None:
    sign_state = {"signed": False, "timeout_next": True}

    def iclass_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "jumpMyCenter" in url:
            return httpx.Response(
                302,
                headers={
                    "location": "https://iclass.buaa.edu.cn:8347/index?loginName=23000001"
                },
            )
        if "login.action" in url:
            return httpx.Response(
                200, json={"STATUS": "0", "result": {"id": "u1", "sessionId": "s1"}}
            )
        if "get_stu_course_sched.action" in url:
            return httpx.Response(
                200,
                json={
                    "STATUS": "0",
                    "result": [
                        {
                            "id": "cs-100",
                            "courseName": "计算机组成",
                            "classBeginTime": "2026-11-15 10:00:00",
                            "classEndTime": "2026-11-15 11:40:00",
                            "signStatus": 1 if sign_state["signed"] else 0,
                        }
                    ],
                },
            )
        if "stu_auto_sign.action" in url:
            if sign_state["timeout_next"]:
                sign_state["timeout_next"] = False
                sign_state["signed"] = (
                    True  # Server processed it, but response timed out!
                )
                raise httpx.ReadTimeout("upstream timeout")
            return httpx.Response(
                200, json={"STATUS": "0", "result": {"stuSignStatus": 1}}
            )
        return httpx.Response(200, text="ok")

    # 09:56 is inside the sign-in window [09:55, 10:30] for the 10:00 class
    now_sh = datetime(2026, 11, 15, 9, 56, 0, tzinfo=SHANGHAI_TZ)
    clock = Clock(now_sh.astimezone(UTC_TZ))
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(iclass_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        mod = session.get(ModuleConfig, "classroom")
        assert mod is not None
        mod.enabled = True
        set_network_mode(session, "direct")
        session.commit()

    await sync_classroom_today(
        container.session_factory, container.session_mgr, container.registry, clock
    )

    # First scheduler tick -> triggers window signin -> hits timeout -> pending_confirm
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = (
            session.query(TaskExecution)
            .filter_by(task_type="classroom.auto_signin")
            .first()
        )
        assert ex is not None
        assert ex.status == "pending_confirm"

    # Advance 10s -> scheduler re-checks platform state and confirms signStatus == 1!
    clock.advance(10)
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = (
            session.query(TaskExecution)
            .filter_by(task_type="classroom.auto_signin")
            .first()
        )
        sched = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261115:cs-100")
            .one()
        )
        assert ex is not None and ex.status == "succeeded"
        assert "确认" in (ex.result_summary or "")
        assert sched.status == "completed"


async def test_ygdk_oauth_http_error_and_sso_refresh(tmp_path: Path) -> None:
    state = {"sso_authed": False, "return_502": True}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if (
            "sso.buaa.edu.cn/login" in url
            and request.method == "GET"
            and "service=" not in url
        ):
            return httpx.Response(
                200,
                text='<form id="fm1"><input type="hidden" name="execution" value="e1"></form>',
            )
        if "sso.buaa.edu.cn/login" in url and request.method == "POST":
            state["sso_authed"] = True
            return httpx.Response(200, text="<html>Welcome</html>")
        if "uc/api/login" in url or "uc/userinfo" in url:
            return httpx.Response(200, json={"data": {"name": "测试用户"}})
        if "oauth/index" in url:
            if state["return_502"]:
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://sso.buaa.edu.cn/login?service=https%3A%2F%2Fapp.buaa.edu.cn%2Fuc"
                    },
                )
            if not state["sso_authed"]:
                return httpx.Response(200, text='<form id="fm1">CAS Login</form>')
            return httpx.Response(
                302,
                headers={"location": "https://ygdk.buaa.edu.cn/#/home?code=freshcode"},
            )
        if "sso.buaa.edu.cn/login" in url and "service=" in url:
            return httpx.Response(502, text="Proxy Error")
        if "campusAppLogin" in url:
            return httpx.Response(
                200, json={"code": 1, "result": {"data": {"uid": 99, "token": "tok99"}}}
            )
        if "Classify/getList" in url:
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "result": {"list": [{"classify_id": 1, "name": "阳光体育"}]},
                },
            )
        if "Item/getList" in url:
            return httpx.Response(
                200,
                json={"code": 1, "result": {"list": [{"item_id": 2, "name": "跑步"}]}},
            )
        if "Clockin/getCount" in url:
            return httpx.Response(
                200,
                json={"code": 1, "result": {"term_good_count_show": 3, "term_num": 16}},
            )
        if "Term/get" in url:
            return httpx.Response(
                200, json={"code": 1, "result": {"term_id": 20261, "name": "2026秋"}}
            )
        return httpx.Response(200, json={"code": 1, "result": {}})

    clock = Clock(
        datetime(2026, 11, 16, 13, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        session.commit()

    client = YgdkClient(container.session_mgr, network_mode="direct")
    with pytest.raises(BuaaError, match="HTTP 502"):
        await client.get_overview()

    # Now OAuth returns SSO login page -> get_overview must auto-refresh SSO and succeed
    state["return_502"] = False
    ov = await client.get_overview()
    assert ov.term_count == 3
    assert ov.term_target == 16


async def test_boya_sync_schedules_sign_windows_and_reminders(tmp_path: Path) -> None:
    def _enc_resp(payload: dict) -> httpx.Response:
        # In test we return plain JSON since BoyaClient falls back to resp.json() cleanly
        return httpx.Response(200, json=payload)

    def boya_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cas/login" in url:
            return httpx.Response(
                302,
                headers={
                    "location": "https://bykc.buaa.edu.cn/cas-login?token=bykc-tok"
                },
            )
        if "getAllConfig" in url:
            return _enc_resp(
                {
                    "status": "0",
                    "data": {
                        "semester": [
                            {
                                "semesterStartDate": "2026-09-01 00:00:00",
                                "semesterEndDate": "2027-01-31 23:59:59",
                            }
                        ]
                    },
                }
            )
        if "queryStudentSemesterCourseByPage" in url:
            return _enc_resp(
                {
                    "status": "0",
                    "data": {
                        "last": True,
                        "content": [
                            {
                                "id": 2001,
                                "courseName": "自主美育欣赏",
                                "coursePosition": "学院路音乐厅",
                                "courseNewKind2": {"kindName": "美育"},
                                "courseSelectStartDate": "2026-11-15 09:00:00",
                                "courseSelectEndDate": "2026-11-15 18:00:00",
                                "courseCurrentCount": 10,
                                "courseMaxCount": 50,
                                "courseSignConfig": json.dumps(
                                    {
                                        "signPointList": [
                                            {"lat": 39.98, "lng": 116.34, "radius": 30}
                                        ]
                                    }
                                ),
                            },
                            {
                                "id": 2002,
                                "courseName": "常规德育讲座",
                                "coursePosition": "学院路主M",
                                "courseNewKind2": {"kindName": "德育"},
                                "courseSelectStartDate": "2026-11-15 10:05:00",
                                "courseSelectEndDate": "2026-11-15 18:00:00",
                                "courseCurrentCount": 10,
                                "courseMaxCount": 50,
                                "courseSignConfig": "{}",
                            },
                        ],
                    },
                }
            )
        if "queryChosenCourse" in url:
            return _enc_resp(
                {
                    "status": "0",
                    "data": {
                        "courseList": [
                            {
                                "id": 9001,
                                "checkin": 0,
                                "pass": 0,
                                "courseInfo": {
                                    "id": 2003,
                                    "courseName": "已选自主劳育课",
                                    "coursePosition": "学院路操场",
                                    "courseNewKind2": {"kindName": "劳动教育"},
                                    "courseSignConfig": json.dumps(
                                        {
                                            "signStartDate": "2026-11-15 10:00:00",
                                            "signEndDate": "2026-11-15 10:30:00",
                                            "signOutStartDate": "2026-11-15 11:30:00",
                                            "signOutEndDate": "2026-11-15 12:00:00",
                                            "signPointList": [
                                                {
                                                    "lat": 39.98,
                                                    "lng": 116.34,
                                                    "radius": 30,
                                                }
                                            ],
                                        }
                                    ),
                                },
                            }
                        ]
                    },
                }
            )
        if "queryStatisticByUserId" in url:
            return _enc_resp(
                {
                    "status": "0",
                    "data": {
                        "validCount": 2,
                        "statistical": {
                            "1|博雅课程": {
                                "55|德育": {
                                    "assessmentCount": 2,
                                    "completeAssessmentCount": 2,
                                },
                                "56|美育": {
                                    "assessmentCount": 1,
                                    "completeAssessmentCount": 0,
                                },
                                "57|劳动教育": {
                                    "assessmentCount": 2,
                                    "completeAssessmentCount": 1,
                                },
                                "58|安全健康": {
                                    "assessmentCount": 1,
                                    "completeAssessmentCount": 0,
                                },
                            }
                        },
                    },
                }
            )
        return _enc_resp({"status": "0", "data": {}})

    now_sh = datetime(2026, 11, 15, 10, 0, 0, tzinfo=SHANGHAI_TZ)
    clock = Clock(now_sh.astimezone(UTC_TZ))
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(boya_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        mod = session.get(ModuleConfig, "boya")
        assert mod is not None
        mod.enabled = True
        set_network_mode(session, "direct")
        session.commit()

    await sync_boya_all(
        container.session_factory, container.session_mgr, container.registry, clock
    )

    with container.session_factory() as session:
        c2001 = session.get(BoyaCourseCache, 2001)
        c2002 = session.get(BoyaCourseCache, 2002)
        assert (
            c2001 is not None
            and c2001.sub_category == "美育"
            and c2001.sign_method == "自主签到"
        )
        assert (
            c2002 is not None
            and c2002.sub_category == "德育"
            and c2002.sign_method == "常规签到"
        )

        # Auto signin & signout scheduled for selected course 2003
        assert (
            session.query(TaskSchedule).filter_by(business_key="boya:sign:2003").first()
            is not None
        )
        assert (
            session.query(TaskSchedule)
            .filter_by(business_key="boya:signout:2003")
            .first()
            is not None
        )

        assert (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:2002:2026-11-15 10:05:00")
            .first()
            is not None
        )


async def test_classroom_signin_offset_and_window_bounds(tmp_path: Path) -> None:
    def iclass_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "jumpMyCenter" in url:
            return httpx.Response(
                302,
                headers={
                    "location": "https://iclass.buaa.edu.cn:8347/index?loginName=23000001"
                },
            )
        if "login.action" in url:
            return httpx.Response(
                200, json={"STATUS": "0", "result": {"id": "u1", "sessionId": "s1"}}
            )
        if "get_stu_course_sched.action" in url:
            return httpx.Response(
                200,
                json={
                    "STATUS": "0",
                    "result": [
                        {
                            "id": "cs-200",
                            "courseName": "高等数学",
                            "classBeginTime": "2026-11-15 10:00:00",
                            "classEndTime": "2026-11-15 11:40:00",
                            "signStatus": 0,
                        }
                    ],
                },
            )
        if "stu_auto_sign.action" in url:
            return httpx.Response(
                200, json={"STATUS": "0", "result": {"stuSignStatus": 1}}
            )
        return httpx.Response(200, text="ok")

    now_sh = datetime(2026, 11, 15, 9, 40, 0, tzinfo=SHANGHAI_TZ)
    clock = Clock(now_sh.astimezone(UTC_TZ))
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(iclass_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        mod = session.get(ModuleConfig, "classroom")
        assert mod is not None
        mod.enabled = True
        assert mod.config_json["signin_offset_minutes"] == -5
        set_network_mode(session, "direct")
        session.commit()

    # 1. Default -5 -> [09:55:00, 10:30:00]
    await sync_classroom_today(
        container.session_factory, container.session_mgr, container.registry, clock
    )
    with container.session_factory() as session:
        sched = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261115:cs-200")
            .one()
        )
        assert sched.trigger_config_json["start_at"] == "2026-11-15 09:55:00"
        assert sched.trigger_config_json["end_at"] == "2026-11-15 10:30:00"

        # 2. Offset -15 -> clamped to -5 min (09:55:00)
        mod = session.get(ModuleConfig, "classroom")
        assert mod is not None
        mod.config_json = {"signin_offset_minutes": -15, "poll_interval_seconds": 30}
        session.commit()

    await sync_classroom_today(
        container.session_factory, container.session_mgr, container.registry, clock
    )
    with container.session_factory() as session:
        sched = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261115:cs-200")
            .one()
        )
        assert sched.trigger_config_json["start_at"] == "2026-11-15 09:55:00"
        assert sched.trigger_config_json["end_at"] == "2026-11-15 10:30:00"

        # 3. Positive offset +5 -> 10:05:00 .. 10:30:00
        mod = session.get(ModuleConfig, "classroom")
        assert mod is not None
        mod.config_json = {"signin_offset_minutes": 5, "poll_interval_seconds": 30}
        session.commit()

    await sync_classroom_today(
        container.session_factory, container.session_mgr, container.registry, clock
    )
    with container.session_factory() as session:
        sched = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:signin:20261115:cs-200")
            .one()
        )
        assert sched.trigger_config_json["start_at"] == "2026-11-15 10:05:00"
        assert sched.trigger_config_json["end_at"] == "2026-11-15 10:30:00"

        # Verify sync_classroom_today kept classroom:sync_today at 3600s
        sync_sched = (
            session.query(TaskSchedule)
            .filter_by(business_key="classroom:sync_today")
            .one()
        )
        assert sync_sched.trigger_config_json["interval_seconds"] == 3600

        # 4. Guardrail in handle_classroom_signin prevents running outside [begin - 5m, min(begin + 30m, end))
        from app.framework.tasks import create_schedule

        manual_sched = create_schedule(
            session,
            container.registry,
            name="Early Signin Check",
            task_type="classroom.auto_signin",
            parameters={
                "course_sched_id": "cs-200",
                "course_name": "高等数学",
                "course_date": "20261115",
            },
            trigger_type="once",
            trigger_config={
                "run_at": "2026-11-15 09:40:00",
                "expires_at": "2026-11-15 12:00:00",
            },
            clock=clock,
        )
        session.commit()
        manual_sched_id = manual_sched.id

    # At 09:40 -> before 09:55 -> waiting until 09:55
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=manual_sched_id).one()
        assert ex.status == "waiting"
        assert ex.wait_reason == "before_classroom_signin_window"

    # At 10:31 -> at begin + 30m -> failed terminal
    clock.set(datetime(2026, 11, 15, 10, 31, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ))
    await container.scheduler.tick(wait_for_completion=True)
    with container.session_factory() as session:
        ex = session.query(TaskExecution).filter_by(schedule_id=manual_sched_id).one()
        assert ex.status == "failed"
        assert ex.error_message == "after_classroom_signin_window"


async def test_classroom_sign_in_posts_stu_auto_sign_form(tmp_path: Path) -> None:
    from app.buaa.classroom import ClassroomClient

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "get_timestamp" not in url
        if "get_stu_course_sched.action" in url:
            return httpx.Response(
                200,
                json={
                    "STATUS": "0",
                    "result": [
                        {
                            "id": "cs-300",
                            "courseName": "概率论",
                            "classBeginTime": "2026-11-15 10:00:00",
                            "classEndTime": "2026-11-15 11:40:00",
                            "signStatus": 0,
                        }
                    ],
                },
            )
        if "stu_auto_sign.action" in url:
            captured["url"] = url
            captured["form"] = parse_qs(request.content.decode("utf-8"))
            captured["session_id"] = request.headers.get("sessionId")
            captured["content_type"] = request.headers.get("content-type")
            return httpx.Response(
                200, json={"STATUS": "0", "result": {"stuSignStatus": 1}}
            )
        return httpx.Response(404, text=f"unexpected {url}")

    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        buaa_transport=httpx.MockTransport(handler),
        run_migrations=True,
    )
    container.session_mgr.save_domain_state(
        "iclass:direct", {"user_id": "u-1", "session_id": "s-1"}
    )
    client = ClassroomClient(container.session_mgr, network_mode="direct")
    res = await client.sign_in("cs-300", "20261115")

    assert res.success and not res.already_signed
    assert captured["url"] == (
        "https://iclass.buaa.edu.cn:8181/app/course/stu_auto_sign.action"
    )
    assert captured["form"] == {
        "id": ["u-1"],
        "courseSchedId": ["cs-300"],
        "routerInfo": ["02:00:00:00:00:00"],
        "longitude": ["0"],
        "latitude": ["0"],
        "machineInfo": ["Android"],
    }
    assert captured["session_id"] == "s-1"
    assert captured["content_type"] == "application/x-www-form-urlencoded"


async def test_sangfor_vpn_buaa_auth_and_business_routing(tmp_path: Path) -> None:
    from urllib.parse import parse_qs

    from app.buaa.classroom import ClassroomClient
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_nums = priv_key.public_key().public_numbers()
    rsa_key_hex = format(pub_nums.n, "x")
    csrf_code = "355169547"

    # Verify encrypt_sangfor_password produces valid PKCS1v15 ciphertext decryptable by private key
    sample_hex = encrypt_sangfor_password("pw123456", csrf_code, rsa_key_hex, 65537)
    decrypted = priv_key.decrypt(
        bytes.fromhex(sample_hex), asym_padding.PKCS1v15()
    ).decode("utf-8")
    assert decrypted == f"pw123456_{csrf_code}"

    seen_hosts_with_twfid: list[tuple[str, str]] = []

    def sangfor_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host or ""
        cookie_hdr = request.headers.get("cookie", "")
        if "TWFID=" in cookie_hdr:
            seen_hosts_with_twfid.append((host, request.url.path))

        if host == "vpn.buaa.edu.cn":
            if "login_auth.csp" in url:
                return httpx.Response(
                    200,
                    text=(
                        '<?xml version="1.0" encoding="utf-8"?><Auth>'
                        "<ErrorCode>1</ErrorCode>"
                        "<TwfID>pre-twfid-111</TwfID>"
                        f"<CSRF_RAND_CODE>{csrf_code}</CSRF_RAND_CODE>"
                        f"<RSA_ENCRYPT_KEY>{rsa_key_hex}</RSA_ENCRYPT_KEY>"
                        "<RSA_ENCRYPT_EXP>65537</RSA_ENCRYPT_EXP>"
                        "<RndImg>1</RndImg>"
                        "</Auth>"
                    ),
                    headers={
                        "Set-Cookie": "TWFID=pre-twfid-111; path=/; domain=.vpn.buaa.edu.cn; secure"
                    },
                )
            if "psw_config" in url:
                return httpx.Response(
                    200,
                    text=(
                        '<?xml version="1.0" encoding="utf-8"?><Auth>'
                        "<ErrorCode>1</ErrorCode>"
                        "<USE_RAND_CODE>1</USE_RAND_CODE>"
                        f"<CSRF_RAND_CODE>{csrf_code}</CSRF_RAND_CODE>"
                        f"<RSA_ENCRYPT_KEY>{rsa_key_hex}</RSA_ENCRYPT_KEY>"
                        "<RSA_ENCRYPT_EXP>65537</RSA_ENCRYPT_EXP>"
                        "</Auth>"
                    ),
                )
            if "rand_code.csp" in url:
                return httpx.Response(
                    200, content=b"GIF89aFAKE", headers={"content-type": "image/gif"}
                )
            if "login_psw.csp" in url:
                form = parse_qs(request.content.decode("utf-8"))
                assert form["svpn_name"] == ["23000001"]
                assert form["svpn_req_randcode"] == [csrf_code]
                assert form["svpn_rand_code"] == ["ABCD"]
                pw_plain = priv_key.decrypt(
                    bytes.fromhex(form["svpn_password"][0]), asym_padding.PKCS1v15()
                ).decode("utf-8")
                assert pw_plain == f"pw123456_{csrf_code}"
                return httpx.Response(
                    200,
                    text='<?xml version="1.0" encoding="utf-8"?><Auth><ErrorCode>1</ErrorCode><TwfID>auth-twfid-999</TwfID></Auth>',
                    # Deliberately omit leading dot like real Sangfor login_psw.csp to test cookie sync
                    headers={
                        "Set-Cookie": "TWFID=auth-twfid-999; path=/; domain=vpn.buaa.edu.cn; secure"
                    },
                )
            if "timequery.csp" in url:
                return httpx.Response(
                    200,
                    text='<?xml version="1.0" encoding="utf-8"?><Auth><ErrorCode>1</ErrorCode><data>1800</data><twfID>auth-twfid-999</twfID></Auth>',
                )

        if host == "sso-buaa-edu-cn-s.vpn.buaa.edu.cn":
            assert "TWFID=auth-twfid-999" in cookie_hdr
            if request.method == "GET":
                return httpx.Response(
                    200,
                    text='<form id="fm1"><input type="hidden" name="execution" value="exec-vpn-1"></form>',
                )
            return httpx.Response(200, text="<html>Welcome</html>")

        if host == "uc-buaa-edu-cn-s.vpn.buaa.edu.cn":
            assert "TWFID=auth-twfid-999" in cookie_hdr
            if "userinfo" in url:
                return httpx.Response(200, json={"data": {"name": "李四"}})
            return httpx.Response(200, text="ok")

        if host == "iclass-buaa-edu-cn-8346-p-s.vpn.buaa.edu.cn":
            assert "TWFID=auth-twfid-999" in cookie_hdr
            return httpx.Response(
                302,
                headers={
                    "location": "https://iclass-buaa-edu-cn-8347-p-s.vpn.buaa.edu.cn/index?loginName=23000001"
                },
            )

        if host == "iclass-buaa-edu-cn-8347-p-s.vpn.buaa.edu.cn":
            assert "TWFID=auth-twfid-999" in cookie_hdr
            if "login.action" in url:
                return httpx.Response(
                    200, json={"STATUS": "0", "result": {"id": "u1", "sessionId": "s1"}}
                )
            if "get_stu_course_sched.action" in url:
                return httpx.Response(
                    200,
                    json={
                        "STATUS": "0",
                        "result": [
                            {
                                "id": "cs-vpn-1",
                                "courseName": "编译原理",
                                "classBeginTime": "2026-11-15 10:00:00",
                                "classEndTime": "2026-11-15 11:40:00",
                                "signStatus": 0,
                            }
                        ],
                    },
                )

        return httpx.Response(404, text=f"unexpected {url}")

    clock = Clock(
        datetime(2026, 11, 15, 9, 0, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ)
    )
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(sangfor_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        set_network_mode(session, "vpn_buaa")
        session.commit()

    # Step 1: Sangfor VPN triggers captcha challenge
    with pytest.raises(CaptchaRequiredError):
        await container.session_mgr.login_sso(network_mode="vpn_buaa")

    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None and acc.auth_status == "captcha_required"
        assert acc.captcha_type == "sangfor_vpn"

    # Step 2: Submit Sangfor captcha -> completes VPN login + SSO login + UC userinfo
    info = await container.session_mgr.login_sso(
        network_mode="vpn_buaa", captcha_code="ABCD"
    )
    assert info["name"] == "李四"

    # Step 3: ClassroomClient routes through *.vpn.buaa.edu.cn with synced TWFID cookie
    cls_client = ClassroomClient(container.session_mgr, network_mode="vpn_buaa")
    courses = await cls_client.get_today_courses("20261115")
    assert len(courses) == 1
    assert courses[0].course_name == "编译原理"
    assert any(
        h == "iclass-buaa-edu-cn-8347-p-s.vpn.buaa.edu.cn"
        for h, _ in seen_hosts_with_twfid
    )


def test_boya_campus_inference_and_matching() -> None:
    cases = [
        (["沙河校区"], "校医院", "沙河校区"),
        (["学院路校区"], "校医院", "学院路校区"),
        (["杭州校区"], "杭州国际校园", "杭州校区"),
        (["全部校区"], "沙河主楼A415", "沙河校区"),
        (["全部校区"], "J3-101", "沙河校区"),
        (["全部校区"], "J0-004", "沙河校区"),
        (["全部校区"], "SH3-101", "沙河校区"),
        (["全部校区"], "咏曼剧场", "沙河校区"),
        (["全部校区"], "晨兴音乐厅", "学院路校区"),
        (["全部校区"], "主121", "学院路校区"),
        (["全部校区"], "主M202教室", "学院路校区"),
        (["全部校区"], "主南106", "学院路校区"),
        (["全部校区"], "学院路37号", "学院路校区"),
        (["全部校区"], "新主楼B324", "学院路校区"),
        (["全部校区"], "B332", "学院路校区"),
        (["学院路校区", "沙河校区"], "沙河校区J3-201", "沙河校区"),
        (["学院路校区", "沙河校区"], "新主楼会议中心第二报告厅", "学院路校区"),
        (["学院路校区", "沙河校区", "杭州校区"], "线上", "线上"),
        (["全部校区"], "四川自贡", "全部校区"),
    ]
    for idx, (campuses, loc, expected) in enumerate(cases, start=1):
        c = parse_course(
            {
                "id": idx,
                "courseName": f"课程{idx}",
                "courseCampusList": campuses,
                "coursePosition": loc,
            }
        )
        assert c.resolved_campus == expected
        assert infer_course_campus(c) == expected

    shahe_c = parse_course(
        {
            "id": 10,
            "courseName": "沙河讲座",
            "courseCampusList": ["全部校区"],
            "coursePosition": "J3-211",
        }
    )
    xyl_c = parse_course(
        {
            "id": 11,
            "courseName": "学院路讲座",
            "courseCampusList": ["全部校区"],
            "coursePosition": "晨兴音乐厅",
        }
    )
    hz_c = parse_course(
        {
            "id": 12,
            "courseName": "杭州讲座",
            "courseCampusList": ["杭州校区"],
            "coursePosition": "杭州",
        }
    )
    online_c = parse_course(
        {
            "id": 13,
            "courseName": "线上讲座",
            "courseCampusList": ["学院路校区", "沙河校区", "杭州校区"],
            "coursePosition": "线上",
        }
    )

    assert course_matches_campus(shahe_c, "北京") is True
    assert course_matches_campus(shahe_c, "沙河") is True
    assert course_matches_campus(shahe_c, "学院路") is False
    assert course_matches_campus(shahe_c, "杭州") is False

    assert course_matches_campus(xyl_c, "北京") is True
    assert course_matches_campus(xyl_c, "学院路") is True
    assert course_matches_campus(xyl_c, "沙河") is False
    assert course_matches_campus(xyl_c, "杭州") is False

    assert course_matches_campus(hz_c, "杭州") is True
    assert course_matches_campus(hz_c, "北京") is False
    assert course_matches_campus(hz_c, "沙河") is False

    assert course_matches_campus(online_c, "北京") is True
    assert course_matches_campus(online_c, "沙河") is True
    assert course_matches_campus(online_c, "学院路") is True
    assert course_matches_campus(online_c, "杭州") is True


async def test_boya_preview_course_scheduled_5_minutes_before_select_start(
    tmp_path: Path,
) -> None:
    from app.db.models import NotificationChannel, NotificationRule

    bark_calls: list[dict] = []

    def bark_handler(request: httpx.Request) -> httpx.Response:
        bark_calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"code": 200, "message": "success"})

    state = {"selected_preview": False}

    def boya_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cas/login" in url:
            return httpx.Response(
                302,
                headers={
                    "location": "https://bykc.buaa.edu.cn/cas-login?token=bykc-tok"
                },
            )
        if "getAllConfig" in url:
            return httpx.Response(
                200,
                json={
                    "status": "0",
                    "data": {
                        "semester": [
                            {
                                "semesterStartDate": "2026-09-01 00:00:00",
                                "semesterEndDate": "2027-01-31 23:59:59",
                            }
                        ]
                    },
                },
            )
        if "queryStudentSemesterCourseByPage" in url:
            return httpx.Response(
                200,
                json={
                    "status": "0",
                    "data": {
                        "last": True,
                        "content": [
                            {
                                "id": 4001,
                                "courseName": "预告沙河讲座",
                                "coursePosition": "SH3-101",
                                "courseCampusList": ["全部校区"],
                                "courseNewKind2": {"kindName": "德育"},
                                "courseSelectStartDate": "2026-11-16 14:00:00",
                                "courseSelectEndDate": "2026-11-18 14:00:00",
                                "courseCurrentCount": 0,
                                "courseMaxCount": 100,
                                "courseSignConfig": "{}",
                                "selected": state["selected_preview"],
                            },
                            {
                                "id": 4002,
                                "courseName": "预告学院路音乐会",
                                "coursePosition": "晨兴音乐厅",
                                "courseCampusList": ["全部校区"],
                                "courseNewKind2": {"kindName": "美育"},
                                "courseSelectStartDate": "2026-11-16 15:00:00",
                                "courseSelectEndDate": "2026-11-18 15:00:00",
                                "courseCurrentCount": 0,
                                "courseMaxCount": 100,
                                "courseSignConfig": "{}",
                            },
                        ],
                    },
                },
            )
        if "queryChosenCourse" in url:
            return httpx.Response(200, json={"status": "0", "data": {"courseList": []}})
        if "queryStatisticByUserId" in url:
            return httpx.Response(
                200, json={"status": "0", "data": {"validCount": 0, "statistical": {}}}
            )
        return httpx.Response(200, json={"status": "0", "data": {}})

    now_sh = datetime(2026, 11, 16, 10, 0, 0, tzinfo=SHANGHAI_TZ)
    clock = Clock(now_sh.astimezone(UTC_TZ))
    container = AppContainer(
        settings=Settings(
            data_dir=tmp_path / "data", frontend_dist_dir=tmp_path / "no_dist"
        ),
        clock=clock,
        buaa_transport=httpx.MockTransport(boya_handler),
        bark_transport=httpx.MockTransport(bark_handler),
        run_migrations=True,
    )
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.student_id = "23000001"
        acc.encrypted_password = container.cipher.encrypt("pw123456")
        acc.campus = "沙河"
        mod = session.get(ModuleConfig, "boya")
        assert mod is not None
        mod.enabled = True
        ch = session.query(NotificationChannel).first()
        assert ch is not None
        ch.enabled = True
        ch.encrypted_device_key = container.cipher.encrypt("bark-key-123")
        set_network_mode(session, "direct")
        rule = (
            session.query(NotificationRule)
            .filter_by(event_type="signup_reminder", module="boya")
            .first()
        )
        assert rule is not None and rule.advance_minutes == 5
        session.commit()

    await sync_boya_all(
        container.session_factory, container.session_mgr, container.registry, clock
    )

    expected_send_utc = datetime(
        2026, 11, 16, 13, 55, 0, tzinfo=SHANGHAI_TZ
    ).astimezone(UTC_TZ)
    with container.session_factory() as session:
        # Shahe preview course 4001 is discovered & queued for 13:55 (5 min before 14:00)
        rem_4001 = (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:4001:2026-11-16 14:00:00")
            .first()
        )
        assert rem_4001 is not None
        assert rem_4001.status == "pending"
        assert rem_4001.next_retry_at == expected_send_utc
        assert "沙河校区" in rem_4001.body

        # Xueyuanlu preview course 4002 is filtered out for Shahe user
        assert (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:4002:2026-11-16 15:00:00")
            .first()
            is None
        )

    await container.notification_dispatcher.dispatch_due_events()
    assert len(bark_calls) == 0

    # At 13:54:59 -> still pending, not sent yet
    clock.set(datetime(2026, 11, 16, 13, 54, 59, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ))
    await container.notification_dispatcher.dispatch_due_events()
    assert len(bark_calls) == 0

    # At 13:55:00 -> sent automatically
    clock.set(datetime(2026, 11, 16, 13, 55, 0, tzinfo=SHANGHAI_TZ).astimezone(UTC_TZ))
    await container.notification_dispatcher.dispatch_due_events()
    assert len(bark_calls) == 1
    assert "博雅选课提醒" in bark_calls[0]["title"]
    with container.session_factory() as session:
        rem_4001 = (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:4001:2026-11-16 14:00:00")
            .one()
        )
        assert rem_4001.status == "sent"

    # Also test: if user switches to 学院路 and syncs again before 14:55, 4002 gets queued,
    # and if 4002 becomes selected before 14:55, its pending reminder is expired.
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.campus = "学院路"
        session.commit()

    await sync_boya_all(
        container.session_factory, container.session_mgr, container.registry, clock
    )
    with container.session_factory() as session:
        rem_4002 = (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:4002:2026-11-16 15:00:00")
            .first()
        )
        assert rem_4002 is not None
        assert rem_4002.status == "pending"
        assert rem_4002.next_retry_at == datetime(
            2026, 11, 16, 14, 55, 0, tzinfo=SHANGHAI_TZ
        ).astimezone(UTC_TZ)

    # Switching back to 沙河 expires the pending 4002 reminder so it won't fire
    with container.session_factory() as session:
        acc = session.query(CampusAccount).first()
        assert acc is not None
        acc.campus = "沙河"
        session.commit()

    await sync_boya_all(
        container.session_factory, container.session_mgr, container.registry, clock
    )
    with container.session_factory() as session:
        rem_4002 = (
            session.query(NotificationEvent)
            .filter_by(event_key="boya:signup_rem:4002:2026-11-16 15:00:00")
            .one()
        )
        assert rem_4002.status == "expired"
        assert rem_4002.next_retry_at is None
