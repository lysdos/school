from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.main import _Workers, build

EPC_LI = "E28011B0A50500756E79A1C9"
REAL_XLSX = Path(__file__).resolve().parents[1] / "data" / "学生记录.xlsx"


class FakeReader:
    def __init__(self):
        self.connected = False
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.connected = True

    def stop(self, timeout=5.0):
        self.stopped = True
        self.connected = False


def _cfg(tmp_path: Path) -> Config:
    return Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=REAL_XLSX, db_path=tmp_path / "campus.db",
        use_device_beep=False, http_port=8000,
        upload_enabled=False, upload_url="", upload_token="",
        upload_timeout=5.0, upload_interval=15.0, upload_batch_size=50,
        admin_password="",
    )


@pytest.fixture()
def reader():
    return FakeReader()


def test_build_imports_real_roster(tmp_path, reader):
    app = build(_cfg(tmp_path), reader=reader)
    with TestClient(app) as client:
        rows = client.get("/api/students").json()
    assert len(rows) == 6
    names = {r["name"] for r in rows}
    assert "李弘毅" in names


def test_workers_start_on_startup_and_stop_on_shutdown(tmp_path, reader):
    app = build(_cfg(tmp_path), reader=reader)
    with TestClient(app):
        assert reader.started is True
        assert reader.connected is True
    assert reader.stopped is True
    assert reader.connected is False


def test_health_reflects_reader_lifecycle(tmp_path, reader):
    app = build(_cfg(tmp_path), reader=reader)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["reader_connected"] is True


def test_simulated_swipe_updates_stats(tmp_path, reader):
    app = build(_cfg(tmp_path), reader=reader)
    with TestClient(app) as client:
        client.post("/api/simulate", params={"epc": EPC_LI})
        stats = client.get("/api/stats/today").json()
    assert stats["in_count"] == 1
    assert stats["in_campus_count"] == 1


def test_data_persists_across_restart(tmp_path, reader):
    cfg = _cfg(tmp_path)
    with TestClient(build(cfg, reader=reader)) as client:
        client.post("/api/simulate", params={"epc": EPC_LI})

    with TestClient(build(cfg, reader=FakeReader())) as client:
        body = client.get("/api/swipes").json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "李弘毅"


def test_missing_roster_file_does_not_crash(tmp_path, reader):
    cfg = Config(**{**_cfg(tmp_path).__dict__, "roster_file": tmp_path / "nope.xlsx"})
    app = build(cfg, reader=reader)
    with TestClient(app) as client:
        assert client.get("/api/students").json() == []
        assert client.get("/api/health").status_code == 200


def test_workers_stop_consumer_before_reader(tmp_path):
    """关闭顺序必须是 消费线程先停、读卡器后停。

    反过来会让读卡器还在往队列里塞事件时消费端已经没了，事件被静默丢弃。
    """
    order = []

    class Recorder:
        def __init__(self, name):
            self._name = name

        def start(self):
            order.append(f"start:{self._name}")

        def stop(self, timeout=5.0):
            order.append(f"stop:{self._name}")

    db = Database(tmp_path / "x.db")
    workers = _Workers(Recorder("reader"), Recorder("consumer"),
                       Recorder("upload"), db)
    workers.start()
    workers.stop()

    assert order == ["start:reader", "start:consumer", "start:upload",
                     "stop:consumer", "stop:upload", "stop:reader"]
