from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.bus import EventBus
from app.config import Config
from app.db import Database
from app.service import SwipeService

REQUIRED_IDS = [
    "conn-dot", "conn-text", "beep-toggle",
    "stat-in", "stat-out", "stat-incampus", "stat-total",
    "live", "query", "q-name", "q-date", "q-reset",
    "history", "prev", "next", "page-info",
]


@pytest.fixture()
def client(tmp_path):
    cfg = Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=tmp_path / "r.xlsx", db_path=tmp_path / "campus.db",
        use_device_beep=False, http_port=8000,
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
    res = client.get("/style.css")
    assert res.status_code == 200
    assert ".live-item" in res.text


def test_script_is_served(client):
    res = client.get("/app.js")
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
