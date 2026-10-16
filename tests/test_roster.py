from pathlib import Path

import pytest
from openpyxl import Workbook

from app.db import Database
from app.models import normalize_epc
from app.roster import import_roster

REAL_XLSX = Path(__file__).resolve().parents[1] / "data" / "学生记录.xlsx"


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "campus.db")
    yield d
    d.close()


def _write_xlsx(path: Path, rows) -> Path:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_normalize_epc_uppercases_and_strips():
    assert normalize_epc("  e28011b0a50500756e79a1c9 ") == "E28011B0A50500756E79A1C9"


def test_import_real_roster_file(db):
    assert REAL_XLSX.exists(), f"缺少名单文件 {REAL_XLSX}"
    count = import_roster(db, REAL_XLSX)
    assert count == 6

    students = db.list_students()
    assert len(students) == 6
    by_epc = {s.epc: s for s in students}

    assert "E28011B0A50500756E79A1C9" in by_epc
    li = by_epc["E28011B0A50500756E79A1C9"]
    assert li.name == "李弘毅"
    assert li.student_no == "1424764951"


def test_import_is_idempotent(db, tmp_path):
    p = _write_xlsx(tmp_path / "r.xlsx", [
        ("EPC", "学号", "姓名"),
        ("AABB", 1001, "张三"),
    ])
    assert import_roster(db, p) == 1
    assert import_roster(db, p) == 1
    assert len(db.list_students()) == 1


def test_import_normalizes_epc_case(db, tmp_path):
    p = _write_xlsx(tmp_path / "r.xlsx", [
        ("EPC", "学号", "姓名"),
        ("aabbcc", 1001, "张三"),
    ])
    import_roster(db, p)
    assert db.get_student("AABBCC") is not None


def test_import_handles_float_student_no(db, tmp_path):
    p = _write_xlsx(tmp_path / "r.xlsx", [
        ("EPC", "学号", "姓名"),
        ("AABB", 1001.0, "张三"),
    ])
    import_roster(db, p)
    assert db.get_student("AABB").student_no == "1001"


def test_import_skips_blank_rows(db, tmp_path):
    p = _write_xlsx(tmp_path / "r.xlsx", [
        ("EPC", "学号", "姓名"),
        ("AABB", 1001, "张三"),
        (None, None, None),
        ("", "", ""),
        ("CCDD", 1002, "李四"),
    ])
    assert import_roster(db, p) == 2


def test_import_raises_on_bad_header(db, tmp_path):
    p = _write_xlsx(tmp_path / "r.xlsx", [("ID", "学号", "姓名"), ("AABB", 1, "张三")])
    with pytest.raises(ValueError, match="表头不符"):
        import_roster(db, p)


def test_import_raises_on_missing_file(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_roster(db, tmp_path / "nope.xlsx")
