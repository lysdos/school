import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from simulate import build_parser, pick_epc  # noqa: E402

from app.db import Database  # noqa: E402

EPC_LI = "E28011B0A50500756E79A1C9"


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "campus.db")
    d.upsert_student(EPC_LI, "1424764951", "李弘毅", "2026-10-15T00:00:00")
    d.upsert_student("BBBB", "1213311379", "单钰辰", "2026-10-15T00:00:00")
    yield d
    d.close()


def test_pick_epc_returns_known_student(db):
    assert pick_epc(db) in {EPC_LI, "BBBB"}


def test_pick_epc_unknown_returns_unlisted_epc(db):
    picked = pick_epc(db, want_unknown=True)
    assert db.get_student(picked) is None


def test_pick_epc_with_empty_db_returns_unknown(db, tmp_path):
    empty = Database(tmp_path / "empty.db")
    assert empty.get_student(pick_epc(empty)) is None
    empty.close()


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.count == 1
    assert args.delay == 1.0
    assert args.direct is False
    assert args.unknown is False


def test_parser_reads_epc_and_count():
    args = build_parser().parse_args(["--epc", "AABB", "--count", "3", "--delay", "0.5"])
    assert args.epc == "AABB"
    assert args.count == 3
    assert args.delay == 0.5


def test_http_mode_with_explicit_epc_does_not_open_db(monkeypatch):
    """指定 --epc 走 HTTP 时不该碰本地数据库。

    否则会踩到「容器和宿主机同时打开同一个 SQLite 文件」这个坑 ——
    Docker Desktop 的 bind mount 处理不好锁，容器会报 disk I/O error。
    """
    import simulate

    opened = []

    class SpyDb:
        def __init__(self, path):
            opened.append(path)

        def close(self):
            pass

    monkeypatch.setattr(simulate, "Database", SpyDb)
    monkeypatch.setattr(simulate, "_post", lambda url, epc: {
        "accepted": True,
        "event": {"name": "李弘毅", "direction": "in", "status": "ok",
                  "swiped_at": "2026-10-15T08:00:00"},
    })

    assert simulate.main(["--epc", "AABB"]) == 0
    assert opened == []


def test_http_mode_without_epc_opens_db_to_pick_student(monkeypatch):
    """不指定 --epc 时要读名单来随机挑人，这时必须开库。"""
    import simulate

    opened = []

    class SpyDb:
        def __init__(self, path):
            opened.append(path)

        def list_students(self):
            from app.models import Student
            return [Student(epc="AABB", student_no="1001", name="张三")]

        def close(self):
            pass

    monkeypatch.setattr(simulate, "Database", SpyDb)
    monkeypatch.setattr(simulate, "_post", lambda url, epc: {
        "accepted": True,
        "event": {"name": "张三", "direction": "in", "status": "ok",
                  "swiped_at": "2026-10-15T08:00:00"},
    })

    assert simulate.main([]) == 0
    assert len(opened) == 1
