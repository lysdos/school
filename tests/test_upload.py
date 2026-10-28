"""UploadWorker 的契约测试。

主要钉住:
1. enabled = false 时完全不启动线程,也不应被外部影响
2. enabled = true 时,只拉 uploaded_at IS NULL 的行
3. POST 成功就把这一批标记成 uploaded_at
4. POST 失败时不更新,让 next tick 再试
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Config
from app.db import Database
from app.models import Direction, RawTagEvent, SwipeEvent, SwipeStatus
from app.upload import UploadWorker


EPC = "E28011B0A50500756E79A1C9"


def _cfg(**over) -> Config:
    base = dict(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=Path("data/学生记录.xlsx"),
        db_path=Path("data/campus.db"),
        use_device_beep=False, http_port=8000,
        upload_enabled=True, upload_url="http://example.test/ingest",
        upload_token="secret", upload_timeout=5.0,
        upload_interval=15.0, upload_batch_size=50,
        admin_password="",
    )
    base.update(over)
    return Config(**base)


def _make_pending(db: Database, n: int = 2) -> list[int]:
    db.upsert_student(EPC, "1424764951", "李弘毅", "2026-10-15T00:00:00",
                      class_name="三年二班")
    ids = []
    for i in range(n):
        ev = db.insert_swipe(SwipeEvent(
            id=None, epc=EPC, student_no="1424764951", name="李弘毅",
            class_name="三年二班", direction=Direction.IN,
            status=SwipeStatus.OK, rssi=-50, ant_id=1,
            reader_addr="192.168.1.168:8160",
            swiped_at=f"2026-10-15T08:0{i}:00",
        ))
        assert ev.id is not None
        ids.append(ev.id)
    return ids


def test_disabled_does_not_start(monkeypatch, tmp_path):
    db = Database(tmp_path / "c.db")
    w = UploadWorker(_cfg(upload_enabled=False, upload_url=""), db)
    assert w.enabled is False
    w.start()
    assert w._thread is None
    db.close()


def test_enabled_but_no_url_does_not_start(tmp_path):
    db = Database(tmp_path / "c.db")
    w = UploadWorker(_cfg(upload_enabled=True, upload_url=""), db)
    assert w.enabled is False
    w.start()
    assert w._thread is None
    db.close()


def test_posts_pending_and_marks_uploaded(tmp_path, monkeypatch):
    db = Database(tmp_path / "c.db")
    ids = _make_pending(db, 2)
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        body = req.data
        captured["body"] = json.loads(body.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    w = UploadWorker(_cfg(), db, clock=lambda: datetime(2026, 10, 15, 9, 0, 0))
    result = w.flush_once()

    db.close()
    assert result["ok"] is True
    assert result["sent"] == 2
    assert captured["url"] == "http://example.test/ingest"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["events"][0]["epc"] == EPC
    assert captured["body"]["events"][0]["class_name"] == "三年二班"

    # 重新打开读,验证 uploaded_at 已被戳上
    db2 = Database(tmp_path / "c.db")
    pending = db2.list_pending_uploads()
    db2.close()
    assert pending == []


def test_post_failure_leaves_pending_for_retry(tmp_path, monkeypatch):
    db = Database(tmp_path / "c.db")
    _make_pending(db, 1)

    def boom(*_a, **_kw): raise RuntimeError("500")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    w = UploadWorker(_cfg(), db)
    result = w.flush_once()
    db.close()

    assert result["ok"] is False
    assert result["sent"] == 1

    db2 = Database(tmp_path / "c.db")
    pending = db2.list_pending_uploads()
    db2.close()
    assert len(pending) == 1
    assert pending[0].uploaded_at is None


def test_empty_pending_is_noop(tmp_path, monkeypatch):
    db = Database(tmp_path / "c.db")
    calls = []

    def fake(req, timeout):
        calls.append(True); raise AssertionError("不该走到 http")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    w = UploadWorker(_cfg(), db)
    result = w.flush_once()
    db.close()
    assert result == {"ok": True, "sent": 0, "ok_count": 0}
    assert calls == []
