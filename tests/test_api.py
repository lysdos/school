from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.bus import EventBus
from app.config import Config
from app.db import Database
from app.service import SwipeService

EPC_LI = "E28011B0A50500756E79A1C9"
EPC_UNKNOWN = "FFFFFFFFFFFFFFFF"


class FakeReader:
    def __init__(self, connected=True):
        self.connected = connected


def _cfg(tmp_path: Path) -> Config:
    return Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=tmp_path / "学生记录.xlsx", db_path=tmp_path / "campus.db",
        use_device_beep=False, http_port=8000,
    )


@pytest.fixture()
def client(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    db.upsert_student(EPC_LI, "1424764951", "李弘毅", "2026-10-15T00:00:00")
    app = create_app(cfg, db, SwipeService(db, 10.0, 120.0), FakeReader(), EventBus())
    with TestClient(app) as c:
        yield c
    db.close()


def test_health_reports_reader_state(client):
    body = client.get("/api/health").json()
    assert body["reader_connected"] is True
    assert "时区=" in body["timezone"]
    assert body["now"]


def test_health_reports_disconnected_reader(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    app = create_app(cfg, db, SwipeService(db), FakeReader(connected=False), EventBus())
    with TestClient(app) as c:
        assert c.get("/api/health").json()["reader_connected"] is False
    db.close()


def test_stats_today_on_empty_db(client):
    body = client.get("/api/stats/today").json()
    assert body["in_count"] == 0
    assert body["out_count"] == 0
    assert body["in_campus_count"] == 0
    assert body["total_students"] == 1


def test_students_endpoint(client):
    rows = client.get("/api/students").json()
    assert len(rows) == 1
    assert rows[0] == {
        "epc": EPC_LI, "student_no": "1424764951", "name": "李弘毅",
        "in_campus": False, "last_swipe_at": None,
    }


def test_simulate_records_and_updates_stats(client):
    body = client.post("/api/simulate", params={"epc": EPC_LI}).json()
    assert body["accepted"] is True
    assert body["event"]["name"] == "李弘毅"
    assert body["event"]["direction"] == "in"
    assert body["event"]["status"] == "ok"

    stats = client.get("/api/stats/today").json()
    assert stats["in_count"] == 1
    assert stats["in_campus_count"] == 1


def test_simulate_deduped_returns_not_accepted(client):
    client.post("/api/simulate", params={"epc": EPC_LI})
    body = client.post("/api/simulate", params={"epc": EPC_LI}).json()
    assert body["accepted"] is False
    assert body["event"] is None


def test_simulate_unknown_card(client):
    body = client.post("/api/simulate", params={"epc": EPC_UNKNOWN}).json()
    assert body["event"]["status"] == "unknown_card"
    assert body["event"]["name"] is None
    assert body["event"]["direction"] == "unknown"


def test_swipes_endpoint_returns_paged_shape(client):
    client.post("/api/simulate", params={"epc": EPC_LI})
    body = client.get("/api/swipes", params={"page": 1, "size": 10}).json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["size"] == 10
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "李弘毅"


def test_swipes_filter_by_name(client):
    client.post("/api/simulate", params={"epc": EPC_LI})
    assert client.get("/api/swipes", params={"name": "李弘毅"}).json()["total"] == 1
    assert client.get("/api/swipes", params={"name": "张三"}).json()["total"] == 0


def test_swipes_rejects_bad_page(client):
    assert client.get("/api/swipes", params={"page": 0}).status_code == 422


def test_swipes_rejects_oversized_page(client):
    assert client.get("/api/swipes", params={"size": 5000}).status_code == 422


def test_simulate_requires_epc(client):
    assert client.post("/api/simulate").status_code == 422


def test_reset_clears_data_and_unblocks_new_swipes(client):
    client.post("/api/simulate", params={"epc": EPC_LI})
    client.post("/api/simulate", params={"epc": EPC_UNKNOWN})
    assert client.get("/api/swipes").json()["total"] == 2

    body = client.post("/api/reset").json()
    assert body["cleared_swipes"] == 2
    assert body["reset_students"] == 1

    assert client.get("/api/swipes").json()["total"] == 0
    stats = client.get("/api/stats/today").json()
    assert stats["in_count"] == 0
    assert stats["in_campus_count"] == 0
    assert stats["total_students"] == 1          # 名单保留

    # 关键：清空后同一张卡立刻再刷要能记下来，不该被去重窗口挡住
    again = client.post("/api/simulate", params={"epc": EPC_LI}).json()
    assert again["accepted"] is True
    assert again["event"]["direction"] == "in"


def test_reset_clears_unknown_tag_dedup_too(tmp_path):
    """内存里的未登记卡去重表也要清，否则「清空」是假清空。"""
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    service = SwipeService(db, 10.0, 120.0)
    app = create_app(cfg, db, service, FakeReader(), EventBus())
    with TestClient(app) as c:
        c.post("/api/simulate", params={"epc": EPC_UNKNOWN})
        assert service.unknown_tracked_count == 1

        c.post("/api/reset")
        assert service.unknown_tracked_count == 0

        body = c.post("/api/simulate", params={"epc": EPC_UNKNOWN}).json()
        assert body["accepted"] is True
    db.close()


def test_swipes_registered_only_filters_unknown_cards(client):
    client.post("/api/simulate", params={"epc": EPC_LI})
    client.post("/api/simulate", params={"epc": EPC_UNKNOWN})

    assert client.get("/api/swipes").json()["total"] == 2
    body = client.get("/api/swipes", params={"registered_only": True}).json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "李弘毅"


def test_simulate_publishes_event_to_bus(tmp_path):
    """刷卡结果必须推给事件总线，否则看板不会实时刷新。"""
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    db.upsert_student(EPC_LI, "1424764951", "李弘毅", "2026-10-15T00:00:00")
    bus = EventBus()
    published = []
    bus.publish_threadsafe = published.append
    app = create_app(cfg, db, SwipeService(db, 10.0, 120.0), FakeReader(), bus)
    with TestClient(app) as c:
        c.post("/api/simulate", params={"epc": EPC_LI})
        c.post("/api/simulate", params={"epc": EPC_LI})  # 去重窗口内，不应发布
    db.close()
    assert len(published) == 1
    assert published[0]["name"] == "李弘毅"
    assert published[0]["direction"] == "in"


# 注：/api/events/stream 不做自动化测试。
# 它的生成器靠 request.is_disconnected() 退出，而 starlette 的 TestClient 不模拟断连，
# 生成器永不返回，关闭流式响应时测试会死等（实测被 timeout 杀掉）。
# SSE 的帧格式、心跳与断连清理放到 Task 13 用真实浏览器手工验证。
