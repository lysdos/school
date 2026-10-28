"""后台管理路由的契约测试。

不验证页面渲染,只验证接口契约:
- admin_password 空时所有管理类接口 404(已禁用)
- 正确口令 Set-Cookie,带 cookie 才能调其他接口
- 学生 CRUD 闭环
- 立即重传调用 flush_once 而不是发 http
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.bus import EventBus
from app.config import Config
from app.db import Database
from app.models import Direction, RawTagEvent, SwipeStatus
from app.service import SwipeService
from app.upload import UploadWorker

EPC_LI = "E28011B0A50500756E79A1C9"


class FakeReader:
    def __init__(self, connected=True):
        self.connected = connected


def _cfg(tmp_path: Path, *, admin_password="letmein",
         upload_enabled=False) -> Config:
    return Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=tmp_path / "r.xlsx", db_path=tmp_path / "campus.db",
        use_device_beep=False, http_port=8000,
        upload_enabled=upload_enabled,
        upload_url="http://example.test/ingest" if upload_enabled else "",
        upload_token="",
        upload_timeout=5.0, upload_interval=15.0, upload_batch_size=50,
        admin_password=admin_password,
    )


@pytest.fixture()
def client(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    upload = UploadWorker(cfg, db)
    app = create_app(cfg, db, SwipeService(db, 10.0, 120.0),
                     FakeReader(), EventBus(), upload=upload)
    with TestClient(app) as c:
        c.cfg = cfg; c.db = db; c.upload = upload
        yield c
    db.close()


# ---------- 启用/禁用 ----------


def test_admin_disabled_when_password_empty(tmp_path):
    cfg = _cfg(tmp_path, admin_password="")
    db = Database(cfg.db_path)
    upload = UploadWorker(cfg, db)
    app = create_app(cfg, db, SwipeService(db), FakeReader(),
                     EventBus(), upload=upload)
    with TestClient(app) as c:
        # 整个 admin 路由表都不存在 —— 既不 401 也不 200,直接 404
        for path in ("/api/admin/login", "/api/admin/students",
                     "/api/admin/swipes", "/api/admin/reupload"):
            r = c.post(path) if path != "/api/admin/students" else c.get(path)
            assert r.status_code == 404, (path, r.status_code)
    db.close()


# ---------- 鉴权 ----------


def test_login_with_wrong_password_rejected(client):
    r = client.post("/api/admin/login", json={"password": "wrong"})
    assert r.status_code == 403


def test_login_with_right_password_sets_cookie(client):
    r = client.post("/api/admin/login", json={"password": "letmein"})
    assert r.status_code == 200
    assert "admin_sid" in r.cookies


def test_students_requires_auth(client):
    r = client.get("/api/admin/students")
    assert r.status_code == 401


def test_whoami_reports_logged_in(client):
    client.post("/api/admin/login", json={"password": "letmein"})
    r = client.get("/api/admin/whoami")
    assert r.status_code == 200
    assert r.json()["authenticated"] is True


# ---------- 学生 CRUD ----------


def test_students_crud(client):
    client.post("/api/admin/login", json={"password": "letmein"})

    # 初始空
    assert client.get("/api/admin/students").json() == []

    # 新增
    r = client.post("/api/admin/students",
                    json={"epc": EPC_LI, "student_no": "1001",
                          "name": "李弘毅", "class_name": "三年二班"})
    assert r.status_code == 200

    # 列表
    rows = client.get("/api/admin/students").json()
    assert len(rows) == 1
    assert rows[0]["class_name"] == "三年二班"

    # 改
    r = client.patch(f"/api/admin/students/{EPC_LI}",
                     json={"student_no": "1001",
                           "name": "李弘毅-改名", "class_name": "三年三班"})
    assert r.status_code == 200
    rows = client.get("/api/admin/students").json()
    assert rows[0]["name"] == "李弘毅-改名"
    assert rows[0]["class_name"] == "三年三班"

    # 删
    r = client.delete(f"/api/admin/students/{EPC_LI}")
    assert r.status_code == 200
    assert client.get("/api/admin/students").json() == []


def test_patch_missing_student_returns_404(client):
    client.post("/api/admin/login", json={"password": "letmein"})
    r = client.patch(f"/api/admin/students/NOSUCH",
                     json={"student_no": "1", "name": "x"})
    assert r.status_code == 404


# ---------- swipes 记录管理 ----------


def test_swipes_admin_edit_and_delete(client):
    client.db.upsert_student(EPC_LI, "1001", "李弘毅", "2026-10-15T00:00:00",
                             class_name="三年二班")
    service = SwipeService(client.db, 10.0, 120.0)
    tag = RawTagEvent(epc=EPC_LI, rssi=-50, ant_id=1,
                      reader_addr="192.168.1.168:8160",
                      read_at="2026-10-15T08:00:00")
    ev = service.process(tag, __import__("datetime").datetime(2026, 10, 15, 8, 0))
    assert ev is not None
    assert ev.id is not None

    client.post("/api/admin/login", json={"password": "letmein"})

    # 查询未上传
    body = client.get("/api/admin/swipes", params={"uploaded": "no"}).json()
    assert body["total"] == 1
    assert body["items"][0]["class_name"] == "三年二班"

    # 编辑
    r = client.patch(f"/api/admin/swipes/{ev.id}",
                     json={"status": "duplicate"})
    assert r.status_code == 200

    edited = client.get("/api/admin/swipes").json()["items"][0]
    assert edited["status"] == "duplicate"

    # 删
    r = client.delete(f"/api/admin/swipes/{ev.id}")
    assert r.status_code == 200
    assert client.get("/api/admin/swipes").json()["total"] == 0


def test_swipes_rejects_invalid_direction(client):
    client.post("/api/admin/login", json={"password": "letmein"})
    r = client.patch("/api/admin/swipes/1", json={"direction": "sideways"})
    assert r.status_code == 422


# ---------- 重传 ----------


def test_reupload_delegates_to_worker(client, monkeypatch):
    client.post("/api/admin/login", json={"password": "letmein"})
    captured = []

    def fake_flush():
        captured.append(True)
        return {"ok": True, "sent": 0, "ok_count": 0}

    monkeypatch.setattr(client.upload, "flush_once", fake_flush)
    r = client.post("/api/admin/reupload")
    assert r.status_code == 200
    assert captured == [True]
