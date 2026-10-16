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
