from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.bus import EventBus
from app.config import Config
from app.db import Database
from app.service import SwipeService

REQUIRED_IDS = [
    "conn-dot", "conn-text", "beep-toggle", "reset-data",
    "stat-in", "stat-out", "stat-incampus", "stat-total",
    "live", "query", "q-name", "q-date", "q-reset",
    "history", "prev", "next", "page-info",
]

ADMIN_REQUIRED_IDS = [
    "login", "login-form", "login-pwd", "app",
    "stu-body", "stu-add", "stu-filter",
    "sw-body", "sw-prev", "sw-next", "sw-page",
    "upload-cfg", "reupload", "logout",
]


@pytest.fixture()
def client(tmp_path):
    cfg = Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=tmp_path / "r.xlsx", db_path=tmp_path / "campus.db",
        use_device_beep=False, http_port=8000,
        upload_enabled=False, upload_url="", upload_token="",
        upload_timeout=5.0, upload_interval=15.0, upload_batch_size=50,
        admin_password="",
    )
    db = Database(cfg.db_path)
    app = create_app(cfg, db, SwipeService(db), type("R", (), {"connected": True})(),
                     EventBus())
    with TestClient(app) as c:
        yield c
    db.close()


def test_index_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "校园出入看板" in res.text


def test_stylesheet_is_served(client):
    res = client.get("/static/style.css")
    assert res.status_code == 200
    assert ".live-item" in res.text


def test_script_is_served(client):
    res = client.get("/static/app.js")
    assert res.status_code == 200
    assert "/api/events/stream" in res.text


def test_index_contains_every_id_app_js_needs(client):
    """app.js 用 getElementById 取这些 id，缺一个前端就会静默报错。"""
    html = client.get("/").text
    missing = [i for i in REQUIRED_IDS if f'id="{i}"' not in html]
    assert missing == []


def test_api_still_reachable_after_static_mount(client):
    """静态目录挂在 / 上，不能把 /api/* 路由遮住。"""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/stats/today").status_code == 200


# ---------- 后台管理页面 ----------


def test_admin_page_is_served(client):
    """后台页面本身不鉴权 —— 里面只有空壳，真正数据全在 /api/admin/*。"""
    res = client.get("/admin")
    assert res.status_code == 200
    assert "后台管理" in res.text


def test_admin_page_with_trailing_slash(client):
    assert client.get("/admin/").status_code == 200


def test_admin_page_contains_every_id_admin_js_needs(client):
    html = client.get("/admin").text
    missing = [i for i in ADMIN_REQUIRED_IDS if f'id="{i}"' not in html]
    assert missing == []


def test_admin_stylesheet_and_script_are_served(client):
    assert client.get("/static/admin.css").status_code == 200
    js = client.get("/static/admin.js")
    assert js.status_code == 200
    assert "/api/admin/students" in js.text


def test_dashboard_has_admin_entry_point(client):
    """看板顶栏要有一个指向 /admin 的入口。"""
    html = client.get("/").text
    assert 'href="/admin"' in html


# ---------- hidden 属性兜底 ----------
#
# HTML 的 `hidden` 只等价于 UA 侧的 `display: none`，任何 author 侧的
# `display` 声明都能盖掉它。之前 .login-wrap / .modal 都写了 display，
# 于是登录框上盖了一层看不见但挡点击的弹窗遮罩 —— 这条测试钉住那条兜底规则。

def test_admin_css_has_hidden_fallback(client):
    css = client.get("/static/admin.css").text
    assert "[hidden] { display: none !important; }" in css


def test_dashboard_css_has_hidden_fallback(client):
    """看板顶栏的「管理」入口是 <a class="btn" hidden>，同样需要这条兜底。"""
    css = client.get("/static/style.css").text
    assert "[hidden] { display: none !important; }" in css
