import pytest

from app.db import Database
from app.models import Direction, SwipeEvent, SwipeStatus


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "campus.db")
    yield d
    d.close()


def _event(**over):
    base = dict(
        id=None, epc="E28011B0A50500756E79A1C9", student_no="1424764951",
        name="李弘毅", direction=Direction.IN, status=SwipeStatus.OK,
        rssi=-52, ant_id=1, reader_addr="192.168.1.168:8160",
        swiped_at="2026-10-15T08:23:11",
    )
    base.update(over)
    return SwipeEvent(**base)


def test_student_roundtrip(db):
    db.upsert_student("E28011B0A50500756E79A1C9", "1424764951", "李弘毅", "2026-10-15T00:00:00")
    s = db.get_student("E28011B0A50500756E79A1C9")
    assert s is not None
    assert s.name == "李弘毅"
    assert s.in_campus is False
    assert s.last_swipe_at is None
    assert s.state_date is None


def test_get_missing_student_returns_none(db):
    assert db.get_student("DEADBEEF") is None


def test_upsert_student_is_idempotent(db):
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")
    assert len(db.list_students()) == 1


def test_upsert_does_not_clobber_state(db):
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")
    db.set_student_state("A1", True, "2026-10-15T08:00:00", "2026-10-15")
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")  # 重新导入名单
    s = db.get_student("A1")
    assert s.in_campus is True
    assert s.state_date == "2026-10-15"


def test_set_student_state(db):
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")
    db.set_student_state("A1", True, "2026-10-15T08:00:00", "2026-10-15")
    s = db.get_student("A1")
    assert s.in_campus is True
    assert s.last_swipe_at == "2026-10-15T08:00:00"
    assert s.state_date == "2026-10-15"


def test_insert_swipe_returns_event_with_id(db):
    saved = db.insert_swipe(_event())
    assert saved.id == 1
    assert saved.name == "李弘毅"
    assert saved.direction is Direction.IN
    assert saved.status is SwipeStatus.OK


def test_insert_swipe_allows_null_student(db):
    saved = db.insert_swipe(_event(epc="FFFFFFFF", student_no=None, name=None,
                                   direction=Direction.UNKNOWN,
                                   status=SwipeStatus.UNKNOWN_CARD))
    assert saved.student_no is None
    assert saved.name is None
    assert saved.status is SwipeStatus.UNKNOWN_CARD


def test_list_swipes_filter_by_name(db):
    db.insert_swipe(_event(name="李弘毅"))
    db.insert_swipe(_event(name="单钰辰", student_no="1213311379", epc="B2"))
    rows = db.list_swipes(name="李弘毅")
    assert len(rows) == 1
    assert rows[0].name == "李弘毅"


def test_list_swipes_filter_by_date(db):
    db.insert_swipe(_event(swiped_at="2026-10-15T08:23:11"))
    db.insert_swipe(_event(swiped_at="2026-10-14T08:23:11"))
    assert len(db.list_swipes(date="2026-10-15")) == 1


def test_list_swipes_newest_first(db):
    db.insert_swipe(_event(swiped_at="2026-10-15T08:00:00"))
    db.insert_swipe(_event(swiped_at="2026-10-15T09:00:00"))
    rows = db.list_swipes()
    assert rows[0].swiped_at == "2026-10-15T09:00:00"


def test_list_swipes_pagination(db):
    for i in range(5):
        db.insert_swipe(_event(swiped_at=f"2026-10-15T08:0{i}:00"))
    assert len(db.list_swipes(page=1, size=2)) == 2
    assert len(db.list_swipes(page=3, size=2)) == 1
    assert db.count_swipes() == 5


def test_today_stats(db):
    db.upsert_student("A1", "1001", "张三", "2026-10-15T00:00:00")
    db.upsert_student("A2", "1002", "李四", "2026-10-15T00:00:00")
    db.set_student_state("A1", True, "2026-10-15T08:00:00", "2026-10-15")

    db.insert_swipe(_event(direction=Direction.IN, status=SwipeStatus.OK))
    db.insert_swipe(_event(direction=Direction.IN, status=SwipeStatus.OK))
    db.insert_swipe(_event(direction=Direction.OUT, status=SwipeStatus.OK))
    db.insert_swipe(_event(direction=Direction.IN, status=SwipeStatus.DUPLICATE))
    db.insert_swipe(_event(direction=Direction.UNKNOWN, status=SwipeStatus.UNKNOWN_CARD))
    db.insert_swipe(_event(direction=Direction.IN, status=SwipeStatus.OK,
                           swiped_at="2026-10-14T08:00:00"))  # 昨天，不计入

    stats = db.today_stats("2026-10-15")
    assert stats["in_count"] == 2      # 只算 status=ok
    assert stats["out_count"] == 1
    assert stats["in_campus_count"] == 1
    assert stats["total_students"] == 2
    assert stats["date"] == "2026-10-15"


def test_indexes_exist(db):
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_swipes_swiped_at" in names
    assert "idx_swipes_epc" in names
    assert "idx_swipes_no_date" in names
