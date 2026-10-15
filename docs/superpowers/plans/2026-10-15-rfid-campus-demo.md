# 学生出入校园 RFID Demo 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python + FastAPI + SQLite 做一个网页看板，把 UHF RFID 读卡器读到的校园卡 EPC 实时映射成学生的进校/出校记录，并用 Docker 一条命令启动。

**Architecture:** 单进程三个并发单元——读卡线程持有 `GClient` 连续盘点，标签回调只把 `RawTagEvent` 塞进 `queue.Queue`；消费线程从队列取事件、跑业务逻辑（EPC→学生、方向翻转、去重、异常判定）、写 SQLite 并通过 `EventBus` 广播；uvicorn 提供 REST + SSE + 静态前端。`reader.py` 只负责"把 EPC 拿出来"，`service.py` 只负责"这个 EPC 意味着什么"，两者通过队列解耦，使业务逻辑可在无硬件环境下单元测试。

**Tech Stack:** Python 3.12、`uhfReaderApi-Cykeo==1.0.9`、FastAPI、uvicorn、SQLite（标准库 `sqlite3`）、openpyxl、原生 HTML/CSS/JS、Docker + docker-compose

**设计文档：** `docs/superpowers/specs/2026-10-15-rfid-campus-demo-design.md`

## Global Constraints

- Python 版本：**3.12**（本机 3.12.10）
- 读卡器 SDK：**`uhfReaderApi-Cykeo==1.0.9`**，导入方式 `from uhf.reader import *`，包名 `uhf.reader`
- 所有文件必须 **UTF-8** 编码
- EPC 一律 **转大写**后存储与比对
- 时间戳一律用**本地时间** ISO8601 字符串（`datetime.now().isoformat(timespec="seconds")`），容器内必须 `TZ=Asia/Shanghai`
- 数据库字段名、dataclass 字段名一律 **snake_case**；JSON API 响应也用 snake_case
- **回调函数内绝对不能调用 `sendSynMsg`**（SDK 文档明确禁止，见 `python-api/example/TcpServer.py` 注释）
- **配置消息必须在设备空闲态发送**：启动顺序是先 `MsgBaseSetTagLog` 再 `MsgBaseInventoryEpc`；退出时先 `MsgBaseStop` 再 `close()`
- 前端**不引入任何 npm 依赖、不做构建**，纯静态文件由 FastAPI 的 `StaticFiles` 托管
- 默认参数：`dedup_window=10` 秒、`duplicate_window=120` 秒、`rdr_host=192.168.1.168`、`rdr_port=8160`、`rdr_antenna=1`、`rdr_power=30`、`rdr_repeated_time=50`（单位 10ms）、`use_device_beep=False`
- 测试用 `pytest`；开发依赖写在 `requirements-dev.txt`

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `requirements.txt` | 运行时依赖 |
| `requirements-dev.txt` | 测试依赖（含 `-r requirements.txt`） |
| `pytest.ini` | pytest 配置 |
| `config.yaml` | 默认配置 |
| `app/config.py` | 配置加载：env > yaml > 内置默认；时区自检 |
| `app/models.py` | 枚举与 dataclass |
| `app/db.py` | SQLite schema + DAO |
| `app/roster.py` | xlsx → students 表（幂等） |
| `app/service.py` | 业务核心 + 消费线程 |
| `app/reader.py` | GClient 封装：连接/重连/盘点 |
| `app/bus.py` | 线程安全的 SSE 事件广播 |
| `app/api.py` | FastAPI 路由 + SSE + 静态托管 |
| `app/main.py` | 组装与生命周期 |
| `app/web/index.html` | 看板页面 |
| `app/web/app.js` | 看板逻辑 |
| `app/web/style.css` | 样式 |
| `tools/simulate.py` | 无硬件时模拟刷卡 |
| `Dockerfile` / `docker-compose.yml` / `.dockerignore` | 容器化 |
| `data/学生记录.xlsx` | 名单源文件（从 `E:\Code\school\学生记录.xlsx` 复制） |
| `tests/` | 测试 |

---

## Task 1: 项目骨架与配置加载

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `config.yaml`
- Create: `app/__init__.py`, `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.config.Config` — frozen dataclass，字段：`rdr_host: str`, `rdr_port: int`, `rdr_antenna: int`, `rdr_power: int | None`, `rdr_repeated_time: int`, `dedup_window: float`, `duplicate_window: float`, `roster_file: Path`, `db_path: Path`, `use_device_beep: bool`, `http_host: str`, `http_port: int`
  - `app.config.load_config(config_path: Path | None = None) -> Config`
  - `app.config.describe_timezone() -> str`

- [ ] **Step 1: 创建依赖与配置文件**

`requirements.txt`：
```
uhfReaderApi-Cykeo==1.0.9
fastapi
uvicorn[standard]
openpyxl
PyYAML
```

`requirements-dev.txt`：
```
-r requirements.txt
pytest
httpx
```

`pytest.ini`：
```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

`config.yaml`：
```yaml
rdr_host: "192.168.1.168"
rdr_port: 8160
rdr_antenna: 1
rdr_power: 30
rdr_repeated_time: 50
dedup_window: 10
duplicate_window: 120
roster_file: "data/学生记录.xlsx"
db_path: "data/campus.db"
use_device_beep: false
http_host: "0.0.0.0"
http_port: 8000
```

`app/__init__.py`：空文件。

- [ ] **Step 2: 写失败的测试**

`tests/test_config.py`：
```python
from pathlib import Path

from app.config import DEFAULTS, ENV_MAP, describe_timezone, load_config


def test_defaults_when_no_file():
    cfg = load_config(None)
    assert cfg.rdr_host == "192.168.1.168"
    assert cfg.rdr_port == 8160
    assert cfg.rdr_antenna == 1
    assert cfg.dedup_window == 10.0
    assert cfg.duplicate_window == 120.0
    assert cfg.use_device_beep is False
    assert isinstance(cfg.db_path, Path)
    assert isinstance(cfg.roster_file, Path)


def test_yaml_overrides_default(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: 10.0.0.9\nrdr_port: 9000\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.rdr_host == "10.0.0.9"
    assert cfg.rdr_port == 9000
    assert cfg.rdr_antenna == 1  # 未覆盖的用默认


def test_env_overrides_yaml(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: 10.0.0.9\n", encoding="utf-8")
    monkeypatch.setenv("RDR_HOST", "172.16.0.1")
    cfg = load_config(p)
    assert cfg.rdr_host == "172.16.0.1"


def test_type_coercion_from_env(monkeypatch):
    monkeypatch.setenv("RDR_PORT", "9100")
    monkeypatch.setenv("DEDUP_WINDOW", "3.5")
    monkeypatch.setenv("USE_DEVICE_BEEP", "true")
    cfg = load_config(None)
    assert cfg.rdr_port == 9100
    assert cfg.dedup_window == 3.5
    assert cfg.use_device_beep is True


def test_bool_coercion_false_values(monkeypatch):
    for raw in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("USE_DEVICE_BEEP", raw)
        assert load_config(None).use_device_beep is False


def test_path_fields_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    assert load_config(None).db_path == tmp_path / "x.db"


def test_none_valued_yaml_key_does_not_wipe_default(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("rdr_host: null\n", encoding="utf-8")
    assert load_config(p).rdr_host == "192.168.1.168"


def test_env_map_covers_every_default_key():
    assert set(ENV_MAP) == set(DEFAULTS)


def test_describe_timezone_mentions_current_time():
    text = describe_timezone()
    assert "时区=" in text
    assert "当前本地时间=" in text
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 4: 实现 `app/config.py`**

```python
"""配置加载：环境变量 > config.yaml > 内置默认。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    rdr_host: str
    rdr_port: int
    rdr_antenna: int
    rdr_power: int | None
    rdr_repeated_time: int
    dedup_window: float
    duplicate_window: float
    roster_file: Path
    db_path: Path
    use_device_beep: bool
    http_host: str
    http_port: int


DEFAULTS: dict[str, object] = {
    "rdr_host": "192.168.1.168",
    "rdr_port": 8160,
    "rdr_antenna": 1,
    "rdr_power": 30,
    "rdr_repeated_time": 50,
    "dedup_window": 10.0,
    "duplicate_window": 120.0,
    "roster_file": "data/学生记录.xlsx",
    "db_path": "data/campus.db",
    "use_device_beep": False,
    "http_host": "0.0.0.0",
    "http_port": 8000,
}

ENV_MAP: dict[str, str] = {
    "rdr_host": "RDR_HOST",
    "rdr_port": "RDR_PORT",
    "rdr_antenna": "RDR_ANTENNA",
    "rdr_power": "RDR_POWER",
    "rdr_repeated_time": "RDR_REPEATED_TIME",
    "dedup_window": "DEDUP_WINDOW",
    "duplicate_window": "DUPLICATE_WINDOW",
    "roster_file": "ROSTER_FILE",
    "db_path": "DB_PATH",
    "use_device_beep": "USE_DEVICE_BEEP",
    "http_host": "HTTP_HOST",
    "http_port": "HTTP_PORT",
}

_PATH_KEYS = ("roster_file", "db_path")
_FALSEY = {"", "0", "false", "no", "off"}


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() not in _FALSEY


def _coerce(raw: str, default: object) -> object:
    """按默认值的类型把环境变量字符串转成目标类型。"""
    if isinstance(default, bool):
        return _coerce_bool(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_config(config_path: Path | None = None) -> Config:
    raw: dict[str, object] = dict(DEFAULTS)

    if config_path is not None and Path(config_path).exists():
        with open(config_path, encoding="utf-8") as fh:
            from_file = yaml.safe_load(fh) or {}
        # 值为 None 的键不覆盖默认，避免 yaml 里写了 `key:` 就把默认值抹掉
        raw.update({k: v for k, v in from_file.items() if v is not None and k in DEFAULTS})

    for key, env_name in ENV_MAP.items():
        if env_name in os.environ:
            raw[key] = _coerce(os.environ[env_name], DEFAULTS[key])

    kwargs: dict[str, object] = {}
    for key, value in raw.items():
        kwargs[key] = Path(value) if key in _PATH_KEYS else value
    return Config(**kwargs)  # type: ignore[arg-type]


def describe_timezone() -> str:
    """启动时自检用：打印当前时区与本地时间，便于排查 TZ 没设导致的日期错位。"""
    now = datetime.now()
    return f"时区={now.astimezone().tzname()} 当前本地时间={now.isoformat(timespec='seconds')}"
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_config.py -v`
Expected: 9 passed

- [ ] **Step 6: 提交**

```bash
git add requirements.txt requirements-dev.txt pytest.ini config.yaml app/__init__.py app/config.py tests/test_config.py
git commit -m "feat: 项目骨架与配置加载"
```

---

## Task 2: 数据模型与数据库层

**Files:**
- Create: `app/models.py`, `app/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.models.Direction`（`str` 枚举）：`IN="in"`, `OUT="out"`, `UNKNOWN="unknown"`
  - `app.models.SwipeStatus`（`str` 枚举）：`OK="ok"`, `UNKNOWN_CARD="unknown_card"`, `DUPLICATE="duplicate"`
  - `app.models.Student` — frozen dataclass：`epc: str`, `student_no: str`, `name: str`, `in_campus: bool`, `last_swipe_at: str | None`, `state_date: str | None`
  - `app.models.RawTagEvent` — frozen dataclass：`epc: str`, `rssi: int | None`, `ant_id: int | None`, `reader_addr: str`, `read_at: str`
  - `app.models.SwipeEvent` — frozen dataclass：`id: int | None`, `epc: str`, `student_no: str | None`, `name: str | None`, `direction: Direction`, `status: SwipeStatus`, `rssi: int | None`, `ant_id: int | None`, `reader_addr: str`, `swiped_at: str`
  - `app.db.Database(path: Path)`，方法：
    - `.close() -> None`
    - `.upsert_student(epc: str, student_no: str, name: str, created_at: str) -> None`
    - `.get_student(epc: str) -> Student | None`
    - `.list_students() -> list[Student]`
    - `.set_student_state(epc: str, in_campus: bool, last_swipe_at: str, state_date: str) -> None`
    - `.insert_swipe(event: SwipeEvent) -> SwipeEvent`（返回填好 `id` 的新对象）
    - `.list_swipes(name=None, student_no=None, date=None, page=1, size=20) -> list[SwipeEvent]`
    - `.count_swipes(name=None, student_no=None, date=None) -> int`
    - `.today_stats(today: str) -> dict`

- [ ] **Step 1: 写失败的测试**

`tests/test_db.py`：
```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: 实现 `app/models.py`**

```python
"""领域模型：枚举与 dataclass。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    IN = "in"
    OUT = "out"
    UNKNOWN = "unknown"


class SwipeStatus(str, Enum):
    OK = "ok"
    UNKNOWN_CARD = "unknown_card"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class Student:
    epc: str
    student_no: str
    name: str
    in_campus: bool = False
    last_swipe_at: str | None = None
    state_date: str | None = None


@dataclass(frozen=True)
class RawTagEvent:
    """读卡器回调产出、塞进队列的原始事件。"""

    epc: str
    rssi: int | None
    ant_id: int | None
    reader_addr: str
    read_at: str


@dataclass(frozen=True)
class SwipeEvent:
    """落库后的刷卡记录。id 为 None 表示尚未写入。"""

    id: int | None
    epc: str
    student_no: str | None
    name: str | None
    direction: Direction
    status: SwipeStatus
    rssi: int | None
    ant_id: int | None
    reader_addr: str
    swiped_at: str
```

- [ ] **Step 4: 实现 `app/db.py`**

```python
"""SQLite schema 与 DAO。多线程共用一条连接，用锁串行化。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Direction, Student, SwipeEvent, SwipeStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    epc           TEXT PRIMARY KEY,
    student_no    TEXT NOT NULL,
    name          TEXT NOT NULL,
    in_campus     INTEGER NOT NULL DEFAULT 0,
    last_swipe_at TEXT,
    state_date    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    epc         TEXT NOT NULL,
    student_no  TEXT,
    name        TEXT,
    direction   TEXT NOT NULL,
    status      TEXT NOT NULL,
    rssi        INTEGER,
    ant_id      INTEGER,
    reader_addr TEXT,
    swiped_at   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_swipes_swiped_at ON swipes(swiped_at);
CREATE INDEX IF NOT EXISTS idx_swipes_epc       ON swipes(epc);
CREATE INDEX IF NOT EXISTS idx_swipes_no_date   ON swipes(student_no, swiped_at);
"""

_SWIPE_COLS = (
    "id, epc, student_no, name, direction, status, "
    "rssi, ant_id, reader_addr, swiped_at"
)


def _row_to_student(row: sqlite3.Row) -> Student:
    return Student(
        epc=row["epc"],
        student_no=row["student_no"],
        name=row["name"],
        in_campus=bool(row["in_campus"]),
        last_swipe_at=row["last_swipe_at"],
        state_date=row["state_date"],
    )


def _row_to_swipe(row: sqlite3.Row) -> SwipeEvent:
    return SwipeEvent(
        id=row["id"],
        epc=row["epc"],
        student_no=row["student_no"],
        name=row["name"],
        direction=Direction(row["direction"]),
        status=SwipeStatus(row["status"]),
        rssi=row["rssi"],
        ant_id=row["ant_id"],
        reader_addr=row["reader_addr"],
        swiped_at=row["swiped_at"],
    )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- students ----------

    def upsert_student(self, epc: str, student_no: str, name: str, created_at: str) -> None:
        """幂等导入：已存在则只更新学号/姓名，绝不动 in_campus 等运行状态。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO students (epc, student_no, name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(epc) DO UPDATE SET
                    student_no = excluded.student_no,
                    name       = excluded.name
                """,
                (epc, student_no, name, created_at),
            )
            self._conn.commit()

    def get_student(self, epc: str) -> Student | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM students WHERE epc = ?", (epc,)
            ).fetchone()
        return _row_to_student(row) if row else None

    def list_students(self) -> list[Student]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM students ORDER BY student_no"
            ).fetchall()
        return [_row_to_student(r) for r in rows]

    def set_student_state(
        self, epc: str, in_campus: bool, last_swipe_at: str, state_date: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE students
                   SET in_campus = ?, last_swipe_at = ?, state_date = ?
                 WHERE epc = ?
                """,
                (1 if in_campus else 0, last_swipe_at, state_date, epc),
            )
            self._conn.commit()

    # ---------- swipes ----------

    def insert_swipe(self, event: SwipeEvent) -> SwipeEvent:
        from datetime import datetime

        created_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO swipes
                    (epc, student_no, name, direction, status,
                     rssi, ant_id, reader_addr, swiped_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.epc, event.student_no, event.name,
                    event.direction.value, event.status.value,
                    event.rssi, event.ant_id, event.reader_addr,
                    event.swiped_at, created_at,
                ),
            )
            self._conn.commit()
            new_id = cur.lastrowid
        return SwipeEvent(**{**event.__dict__, "id": new_id})

    def _swipe_where(self, name, student_no, date) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if name:
            clauses.append("name LIKE ?")
            params.append(f"%{name}%")
        if student_no:
            clauses.append("student_no = ?")
            params.append(student_no)
        if date:
            clauses.append("swiped_at LIKE ?")
            params.append(f"{date}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_swipes(self, name=None, student_no=None, date=None,
                    page: int = 1, size: int = 20) -> list[SwipeEvent]:
        where, params = self._swipe_where(name, student_no, date)
        offset = max(page - 1, 0) * size
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SWIPE_COLS} FROM swipes {where} "
                f"ORDER BY swiped_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, size, offset),
            ).fetchall()
        return [_row_to_swipe(r) for r in rows]

    def count_swipes(self, name=None, student_no=None, date=None) -> int:
        where, params = self._swipe_where(name, student_no, date)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM swipes {where}", tuple(params)
            ).fetchone()
        return int(row["n"])

    def today_stats(self, today: str) -> dict:
        prefix = f"{today}%"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'in'
                                      AND status = 'ok' THEN 1 ELSE 0 END), 0) AS in_count,
                    COALESCE(SUM(CASE WHEN direction = 'out'
                                      AND status = 'ok' THEN 1 ELSE 0 END), 0) AS out_count
                  FROM swipes
                 WHERE swiped_at LIKE ?
                """,
                (prefix,),
            ).fetchone()
            in_campus = self._conn.execute(
                "SELECT COUNT(*) AS n FROM students WHERE in_campus = 1 AND state_date = ?",
                (today,),
            ).fetchone()
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM students"
            ).fetchone()
        return {
            "date": today,
            "in_count": int(row["in_count"]),
            "out_count": int(row["out_count"]),
            "in_campus_count": int(in_campus["n"]),
            "total_students": int(total["n"]),
        }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_db.py -v`
Expected: 13 passed

- [ ] **Step 6: 提交**

```bash
git add app/models.py app/db.py tests/test_db.py
git commit -m "feat: 数据模型与 SQLite 数据访问层"
```

---

## Task 3: 学生名单导入

**Files:**
- Modify: `app/models.py`（追加 `normalize_epc`）
- Create: `app/roster.py`
- Test: `tests/test_roster.py`

**Interfaces:**
- Consumes: `app.db.Database.upsert_student(epc, student_no, name, created_at) -> None`（Task 2）
- Produces:
  - `app.models.normalize_epc(epc: str) -> str` — 去空白 + 转大写
  - `app.roster.import_roster(db: Database, xlsx_path: Path) -> int` — 返回导入条数；文件缺失抛 `FileNotFoundError`，表头不符抛 `ValueError`

- [ ] **Step 1: 写失败的测试**

`tests/test_roster.py`：
```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_roster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.roster'`

- [ ] **Step 3: 在 `app/models.py` 末尾追加 `normalize_epc`**

```python
def normalize_epc(epc: str) -> str:
    """EPC 一律转大写后比对，避免读写器上报与名单大小写不一致导致匹配失败。"""
    return str(epc).strip().upper()
```

- [ ] **Step 4: 实现 `app/roster.py`**

```python
"""从 xlsx 导入学生名单。幂等：重复导入不会产生重复行，也不会清掉运行状态。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from .db import Database
from .models import normalize_epc

EXPECTED_HEADERS = ("EPC", "学号", "姓名")


def _cell_text(value: Any) -> str:
    """Excel 里学号常被存成浮点（1424764951.0），这里还原成整数文本。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_roster(db: Database, xlsx_path: Path) -> int:
    path = Path(xlsx_path)
    if not path.exists():
        raise FileNotFoundError(f"名单文件不存在: {path}")

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook.active
        rows: Iterable[tuple] = sheet.iter_rows(values_only=True)

        try:
            header = tuple(_cell_text(c) for c in next(rows)[:3])
        except StopIteration:
            raise ValueError(f"名单文件为空: {path}") from None

        if header != EXPECTED_HEADERS:
            raise ValueError(f"表头不符，期望 {EXPECTED_HEADERS}，实际 {header}")

        created_at = datetime.now().isoformat(timespec="seconds")
        count = 0
        for row in rows:
            cells = list(row[:3]) + [None] * (3 - len(row[:3]))
            epc = normalize_epc(_cell_text(cells[0]))
            student_no = _cell_text(cells[1])
            name = _cell_text(cells[2])
            if not epc or not student_no or not name:
                continue
            db.upsert_student(epc, student_no, name, created_at)
            count += 1
        return count
    finally:
        workbook.close()
```

- [ ] **Step 5: 把真实名单文件复制进来**

```bash
mkdir -p data
cp "E:/Code/school/学生记录.xlsx" "data/学生记录.xlsx"
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_roster.py -v`
Expected: 8 passed

- [ ] **Step 7: 提交**

```bash
git add app/models.py app/roster.py tests/test_roster.py data/学生记录.xlsx
git commit -m "feat: 从 xlsx 幂等导入学生名单"
```

---

## Task 4: 业务核心 — 方向翻转与未登记卡

**Files:**
- Create: `app/service.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `Database`（Task 2）、`Student` / `RawTagEvent` / `SwipeEvent` / `Direction` / `SwipeStatus` / `normalize_epc`（Task 2、3）
- Produces:
  - `app.service.decide_direction(student: Student) -> Direction` — 单天线状态翻转的唯一实现点
  - `app.service.SwipeService(db: Database, dedup_window: float = 10.0, duplicate_window: float = 120.0)`
    - `.process(tag: RawTagEvent, now: datetime) -> SwipeEvent | None`

- [ ] **Step 1: 写失败的测试**

`tests/test_service.py`：
```python
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.models import Direction, RawTagEvent, SwipeStatus
from app.service import SwipeService, decide_direction

EPC_LI = "E28011B0A50500756E79A1C9"
EPC_UNKNOWN = "FFFFFFFFFFFFFFFF"


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "campus.db")
    d.upsert_student(EPC_LI, "1424764951", "李弘毅", "2026-10-15T00:00:00")
    yield d
    d.close()


@pytest.fixture()
def service(db):
    return SwipeService(db, dedup_window=10.0, duplicate_window=120.0)


def _tag(epc=EPC_LI, rssi=-52, ant_id=1):
    return RawTagEvent(
        epc=epc, rssi=rssi, ant_id=ant_id,
        reader_addr="192.168.1.168:8160",
        read_at="2026-10-15T08:23:11",
    )


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_decide_direction_when_off_campus_is_in(db):
    student = db.get_student(EPC_LI)
    assert decide_direction(student) is Direction.IN


def test_decide_direction_when_on_campus_is_out(db):
    db.set_student_state(EPC_LI, True, "2026-10-15T08:00:00", "2026-10-15")
    student = db.get_student(EPC_LI)
    assert decide_direction(student) is Direction.OUT


def test_first_swipe_marks_in_campus(service, db):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event is not None
    assert event.direction is Direction.IN
    assert event.status is SwipeStatus.OK
    assert event.name == "李弘毅"
    assert event.student_no == "1424764951"
    assert event.rssi == -52
    assert event.ant_id == 1
    assert event.id == 1

    assert db.get_student(EPC_LI).in_campus is True
    assert db.get_student(EPC_LI).state_date == "2026-10-15"


def test_second_swipe_same_day_marks_out(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    event = service.process(_tag(), _at("2026-10-15T12:00:00"))
    assert event.direction is Direction.OUT
    assert event.status is SwipeStatus.OK
    assert db.get_student(EPC_LI).in_campus is False


def test_unknown_card_is_recorded_without_state_change(service, db):
    event = service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    assert event.status is SwipeStatus.UNKNOWN_CARD
    assert event.direction is Direction.UNKNOWN
    assert event.student_no is None
    assert event.name is None
    assert event.epc == EPC_UNKNOWN


def test_unknown_card_does_not_create_student_row(service, db):
    service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    assert db.get_student(EPC_UNKNOWN) is None
    assert len(db.list_students()) == 1


def test_epc_is_normalized_before_lookup(service):
    event = service.process(_tag(epc=EPC_LI.lower()), _at("2026-10-15T08:23:11"))
    assert event.epc == EPC_LI
    assert event.status is SwipeStatus.OK


def test_state_resets_across_day(service, db):
    # 昨天进校，今天第一次刷卡应该判为「进」而不是「出」
    db.set_student_state(EPC_LI, True, "2026-10-14T17:00:00", "2026-10-14")
    event = service.process(_tag(), _at("2026-10-15T08:00:00"))
    assert event.direction is Direction.IN
    assert event.status is SwipeStatus.OK
    student = db.get_student(EPC_LI)
    assert student.in_campus is True
    assert student.state_date == "2026-10-15"


def test_swiped_at_uses_local_isoformat(service):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event.swiped_at == "2026-10-15T08:23:11"


def test_reader_addr_is_recorded(service):
    event = service.process(_tag(), _at("2026-10-15T08:23:11"))
    assert event.reader_addr == "192.168.1.168:8160"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.service'`

- [ ] **Step 3: 实现 `app/service.py`（本任务只做方向翻转，去重窗口在 Task 5 加）**

```python
"""业务核心：把读到的 EPC 变成有意义的进出记录。"""

from __future__ import annotations

from datetime import datetime

from .db import Database
from .models import (
    Direction,
    RawTagEvent,
    Student,
    SwipeEvent,
    SwipeStatus,
    normalize_epc,
)


def decide_direction(student: Student) -> Direction:
    """单天线状态翻转：离校状态下刷卡判为进校，在校状态下判为出校。

    这是方向判定的唯一实现点。将来换成双天线判先后或 GPI 传感器真方向判定，
    只需替换这个函数，其余业务逻辑不受影响。
    """
    return Direction.OUT if student.in_campus else Direction.IN


def _elapsed_seconds(last_iso: str | None, now: datetime) -> float | None:
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return None
    return (now - last).total_seconds()


class SwipeService:
    def __init__(self, db: Database, dedup_window: float = 10.0,
                 duplicate_window: float = 120.0):
        self._db = db
        self._dedup_window = dedup_window
        self._duplicate_window = duplicate_window

    def process(self, tag: RawTagEvent, now: datetime) -> SwipeEvent | None:
        epc = normalize_epc(tag.epc)
        today = now.date().isoformat()
        now_iso = now.isoformat(timespec="seconds")

        student = self._db.get_student(epc)
        if student is None:
            return self._db.insert_swipe(SwipeEvent(
                id=None, epc=epc, student_no=None, name=None,
                direction=Direction.UNKNOWN, status=SwipeStatus.UNKNOWN_CARD,
                rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
                swiped_at=now_iso,
            ))

        # 跨天重置：状态不属于今天就视为离校，省掉一个定时任务
        in_campus = student.in_campus if student.state_date == today else False

        direction = Direction.OUT if in_campus else Direction.IN
        saved = self._db.insert_swipe(SwipeEvent(
            id=None, epc=epc, student_no=student.student_no, name=student.name,
            direction=direction, status=SwipeStatus.OK,
            rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
            swiped_at=now_iso,
        ))
        self._db.set_student_state(epc, not in_campus, now_iso, today)
        return saved
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_service.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat: 业务核心的方向翻转与未登记卡处理"
```

---

## Task 5: 业务核心 — 两级去重窗口

**Files:**
- Modify: `app/service.py`（`SwipeService.process`）
- Test: `tests/test_service.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `SwipeService`
- Produces: 行为增强 —— `process()` 现在可能在去重窗口内返回 `None`；新增 `status=SwipeStatus.DUPLICATE` 的记录路径

**行为规格：**
- 距上次刷卡 `< dedup_window`（默认 10s）：返回 `None`，不写库、不改状态
- 距上次刷卡 `< duplicate_window`（默认 120s）：写库，`status=DUPLICATE`，`direction` 按当前状态推算但**不翻转**；**仍然刷新 `last_swipe_at`**
- 刷新 `last_swipe_at` 的理由：学生在闸机前久留时标签会被反复读到，若不刷新，窗口会从"上一次生效事件"起算而过期，导致状态误翻转

- [ ] **Step 1: 追加失败的测试**

在 `tests/test_service.py` 末尾追加：

```python
# ---------- Task 5: 两级去重窗口 ----------

def test_swipe_within_dedup_window_is_silently_dropped(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    result = service.process(_tag(), _at("2026-10-15T08:23:19"))  # +8s
    assert result is None
    assert db.count_swipes() == 1
    assert db.get_student(EPC_LI).in_campus is True  # 状态未被翻转


def test_swipe_exactly_at_dedup_window_is_not_dropped(service):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    result = service.process(_tag(), _at("2026-10-15T08:23:21"))  # 恰好 +10s
    assert result is not None
    assert result.status is SwipeStatus.DUPLICATE


def test_swipe_in_duplicate_window_is_flagged_and_not_toggled(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))          # 进校
    result = service.process(_tag(), _at("2026-10-15T08:23:41"))  # +30s
    assert result is not None
    assert result.status is SwipeStatus.DUPLICATE
    assert result.direction is Direction.OUT  # 按当时状态推算的方向
    assert db.get_student(EPC_LI).in_campus is True  # 未翻转


def test_duplicate_refreshes_last_swipe_at(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    service.process(_tag(), _at("2026-10-15T08:23:41"))
    assert db.get_student(EPC_LI).last_swipe_at == "2026-10-15T08:23:41"


def test_lingering_student_never_flips(service, db):
    """学生在闸机前逗留 5 分钟，每 30 秒被读到一次，状态应始终为「在校」。"""
    t = _at("2026-10-15T08:00:00")
    service.process(_tag(), t)
    assert db.get_student(EPC_LI).in_campus is True
    for i in range(1, 11):
        service.process(_tag(), t + timedelta(seconds=30 * i))
    assert db.get_student(EPC_LI).in_campus is True
    # 10 次逗留刷卡全部标记为重复
    assert db.count_swipes(date="2026-10-15") == 11
    assert all(e.status is SwipeStatus.DUPLICATE for e in db.list_swipes(size=100)[:-1])


def test_swipe_after_duplicate_window_toggles_normally(service, db):
    service.process(_tag(), _at("2026-10-15T08:23:11"))          # 进校
    result = service.process(_tag(), _at("2026-10-15T08:25:12"))  # +121s
    assert result.status is SwipeStatus.OK
    assert result.direction is Direction.OUT
    assert db.get_student(EPC_LI).in_campus is False


def test_dedup_window_is_per_student(service, db):
    db.upsert_student("BBBB", "1213311379", "单钰辰", "2026-10-15T00:00:00")
    service.process(_tag(), _at("2026-10-15T08:23:11"))
    other = service.process(_tag(epc="BBBB"), _at("2026-10-15T08:23:12"))
    assert other is not None
    assert other.status is SwipeStatus.OK


def test_unknown_card_is_not_subject_to_dedup(service, db):
    service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:11"))
    again = service.process(_tag(EPC_UNKNOWN), _at("2026-10-15T08:23:12"))
    assert again is not None
    assert again.status is SwipeStatus.UNKNOWN_CARD
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_service.py -v`
Expected: 新增的 8 个用例中 `test_swipe_within_dedup_window_is_silently_dropped` 等 FAIL（当前实现会返回事件而不是 `None`）

- [ ] **Step 3: 修改 `app/service.py` 的 `process` 方法**

把 `process` 中"跨天重置"之后到方法结尾的代码整段替换为：

```python
        # 跨天重置：状态不属于今天就视为离校，省掉一个定时任务
        in_campus = student.in_campus if student.state_date == today else False
        elapsed = _elapsed_seconds(student.last_swipe_at, now)

        # 第一级：软件去重窗口内静默丢弃，不写库、不改状态
        if elapsed is not None and elapsed < self._dedup_window:
            return None

        direction = Direction.OUT if in_campus else Direction.IN

        # 第二级：重复刷卡窗口内记录并告警，但不翻转状态。
        # 同时刷新 last_swipe_at —— 否则学生在闸机前久留时窗口会从旧事件起算而过期，
        # 导致"人还在门口却被判为出校"。
        if elapsed is not None and elapsed < self._duplicate_window:
            saved = self._db.insert_swipe(SwipeEvent(
                id=None, epc=epc, student_no=student.student_no, name=student.name,
                direction=direction, status=SwipeStatus.DUPLICATE,
                rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
                swiped_at=now_iso,
            ))
            self._db.set_student_state(epc, in_campus, now_iso, today)
            return saved

        # 正常翻转
        saved = self._db.insert_swipe(SwipeEvent(
            id=None, epc=epc, student_no=student.student_no, name=student.name,
            direction=direction, status=SwipeStatus.OK,
            rssi=tag.rssi, ant_id=tag.ant_id, reader_addr=tag.reader_addr,
            swiped_at=now_iso,
        ))
        self._db.set_student_state(epc, not in_campus, now_iso, today)
        return saved
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_service.py -v`
Expected: 18 passed

- [ ] **Step 5: 提交**

```bash
git add app/service.py tests/test_service.py
git commit -m "feat: 两级去重窗口与重复刷卡告警"
```

---

## Task 6: 读卡器封装

**Files:**
- Create: `app/reader.py`
- Test: `tests/test_reader.py`

**Interfaces:**
- Consumes: `Config`（Task 1）、`RawTagEvent` / `normalize_epc`（Task 2、3）
- Produces:
  - `app.reader.Sdk` — 真实 SDK 命名空间，构造时 `from uhf.reader import ...`，暴露 `GClient` / `EnumG` / `MsgBaseInventoryEpc` / `MsgBaseSetPower` / `MsgBaseSetTagLog` / `MsgBaseStop`
  - `app.reader.ReaderClient(cfg: Config, out_queue: queue.Queue, sdk=None, sleep=time.sleep)`
    - `.connected: bool`
    - `.start() -> None` / `.stop(timeout: float = 5.0) -> None`
    - `._try_connect() -> bool` / `._run() -> None` / `._cleanup() -> None`（可测试的内部方法）

**必须遵守的 SDK 约束：**
- 回调（`callEpcInfo` / `callEpcOver` / `callTcpDisconnect`）内**只做入队和置标志**，绝不调用 `sendSynMsg`
- 配置消息（`MsgBaseSetTagLog` / `MsgBaseSetPower`）必须在 `MsgBaseInventoryEpc` **之前**发送（设备空闲态才能改配置）
- 退出时先 `MsgBaseStop` 再 `close()`
- `callTcpDisconnect` 上报后必须由上位机释放连接对象，否则事件循环上报

- [ ] **Step 1: 写失败的测试**

`tests/test_reader.py`：
```python
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Config
from app.reader import ReaderClient


@pytest.fixture()
def cfg():
    return Config(
        rdr_host="192.168.1.168", rdr_port=8160, rdr_antenna=1, rdr_power=30,
        rdr_repeated_time=50, dedup_window=10.0, duplicate_window=120.0,
        roster_file=Path("data/学生记录.xlsx"), db_path=Path("data/campus.db"),
        use_device_beep=False, http_host="0.0.0.0", http_port=8000,
    )


# ---------- 假 SDK ----------

class FakeMsgSetTagLog:
    def __init__(self, RepeatedTime=None):
        self.RepeatedTime = RepeatedTime


class FakeMsgSetPower:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeMsgInventory:
    def __init__(self, antennaEnable=None, inventoryMode=None):
        self.antennaEnable = antennaEnable
        self.inventoryMode = inventoryMode


class FakeMsgStop:
    pass


class FakeEnumG:
    AntennaNo_1 = SimpleNamespace(value=1)
    AntennaNo_2 = SimpleNamespace(value=2)
    InventoryMode_Inventory = SimpleNamespace(value=1)


class FakeClient:
    def __init__(self, open_ok=True, inventory_rc=0):
        self._open_ok = open_ok
        self._inventory_rc = inventory_rc
        self.callEpcInfo = None
        self.callEpcOver = None
        self.callTcpDisconnect = None
        self.opened_with = []
        self.sent = []
        self.closed = False

    def openTcp(self, addr):
        self.opened_with.append(addr)
        return self._open_ok

    def sendSynMsg(self, msg):
        self.sent.append(msg)
        if isinstance(msg, FakeMsgInventory):
            return self._inventory_rc
        return 0

    def close(self):
        self.closed = True


class FakeSdk:
    def __init__(self, clients):
        self._clients = list(clients)
        self.created = 0
        self.EnumG = FakeEnumG
        self.MsgBaseSetTagLog = FakeMsgSetTagLog
        self.MsgBaseSetPower = FakeMsgSetPower
        self.MsgBaseInventoryEpc = FakeMsgInventory
        self.MsgBaseStop = FakeMsgStop

    def GClient(self):
        client = self._clients[min(self.created, len(self._clients) - 1)]
        self.created += 1
        return client


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- 回调行为 ----------

def test_callback_enqueues_normalized_event(cfg):
    q = queue.Queue()
    rc = ReaderClient(cfg, q, sdk=FakeSdk([FakeClient()]))
    rc._on_epc(SimpleNamespace(result=0, epc="e28011b0a50500756e79a1c9",
                              rssi=-52, antId=1))
    tag = q.get_nowait()
    assert tag.epc == "E28011B0A50500756E79A1C9"
    assert tag.rssi == -52
    assert tag.ant_id == 1
    assert tag.reader_addr == "192.168.1.168:8160"
    assert tag.read_at


def test_callback_supports_capitalized_attributes(cfg):
    """SDK 文档写 Epc/AntId，示例代码用 .epc/.antId，两种都要兼容。"""
    q = queue.Queue()
    rc = ReaderClient(cfg, q, sdk=FakeSdk([FakeClient()]))
    rc._on_epc(SimpleNamespace(Result=0, Epc="AABBCC", Rssi=-60, AntId=2))
    tag = q.get_nowait()
    assert tag.epc == "AABBCC"
    assert tag.rssi == -60
    assert tag.ant_id == 2


def test_callback_ignores_failed_result(cfg):
    q = queue.Queue()
    rc = ReaderClient(cfg, q, sdk=FakeSdk([FakeClient()]))
    rc._on_epc(SimpleNamespace(result=1, epc="AABB", rssi=-52, antId=1))
    assert q.empty()


def test_callback_ignores_empty_epc(cfg):
    q = queue.Queue()
    rc = ReaderClient(cfg, q, sdk=FakeSdk([FakeClient()]))
    rc._on_epc(SimpleNamespace(result=0, epc="", rssi=-52, antId=1))
    assert q.empty()


def test_disconnect_callback_only_sets_flag(cfg):
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([FakeClient()]))
    assert not rc._disconnected.is_set()
    rc._on_disconnect("192.168.1.168:8160")
    assert rc._disconnected.is_set()


# ---------- 连接序列 ----------

def test_connect_sends_config_before_inventory(cfg):
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    assert rc._try_connect() is True
    assert rc.connected is True
    assert client.opened_with == [("192.168.1.168", 8160)]
    kinds = [type(m).__name__ for m in client.sent]
    assert kinds == ["FakeMsgSetTagLog", "FakeMsgSetPower", "FakeMsgInventory"]
    assert client.sent[0].RepeatedTime == 50
    assert client.sent[1].kwargs == {"1": 30}
    assert client.sent[2].antennaEnable == 1
    assert client.sent[2].inventoryMode == 1


def test_connect_registers_callbacks(cfg):
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    assert client.callEpcInfo == rc._on_epc
    assert client.callEpcOver == rc._on_epc_over
    assert client.callTcpDisconnect == rc._on_disconnect


def test_connect_skips_power_when_not_configured(cfg):
    cfg_no_power = Config(**{**cfg.__dict__, "rdr_power": None})
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg_no_power, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    kinds = [type(m).__name__ for m in client.sent]
    assert kinds == ["FakeMsgSetTagLog", "FakeMsgInventory"]


def test_connect_fails_when_open_tcp_fails(cfg):
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([FakeClient(open_ok=False)]))
    assert rc._try_connect() is False
    assert rc.connected is False


def test_connect_fails_when_inventory_rejected(cfg):
    client = FakeClient(open_ok=True, inventory_rc=5)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    assert rc._try_connect() is False
    assert rc.connected is False
    assert client.closed is True  # 失败路径也要释放连接


def test_second_antenna_maps_to_correct_enum(cfg):
    cfg_ant2 = Config(**{**cfg.__dict__, "rdr_antenna": 2})
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg_ant2, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    assert client.sent[-1].antennaEnable == 2
    assert client.sent[1].kwargs == {"2": 30}


# ---------- 重连循环 ----------

def test_retries_until_success(cfg):
    sdk = FakeSdk([FakeClient(False), FakeClient(False), FakeClient(True)])
    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    assert _wait_until(lambda: rc.connected)
    assert sdk.created == 3
    rc.stop()
    assert not rc._thread.is_alive()


def test_disconnect_triggers_reconnect(cfg):
    first, second = FakeClient(True), FakeClient(True)
    sdk = FakeSdk([first, second])
    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    assert _wait_until(lambda: rc.connected)

    first.callTcpDisconnect("192.168.1.168:8160")
    assert _wait_until(lambda: sdk.created == 2 and rc.connected)
    assert first.closed is True

    rc.stop()
    assert not rc._thread.is_alive()


def test_stop_releases_connection(cfg):
    client = FakeClient(True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]), sleep=lambda _s: None)
    rc.start()
    assert _wait_until(lambda: rc.connected)
    rc.stop()
    assert client.closed is True
    assert rc.connected is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.reader'`

- [ ] **Step 3: 实现 `app/reader.py`**

```python
"""读卡器封装：连接、配置、连续盘点、断线重连。

SDK 约束（来自《RFID读写器开发指南-Python》与 python-api/example/TcpServer.py）：
- 回调内绝对不能调用 sendSynMsg，否则会死锁；回调只入队、只置标志。
- 设备处于读卡状态时无法发送配置消息，所以 SetTagLog 必须在 Inventory 之前。
- 退出时必须先 MsgBaseStop 再 close()，否则设备停在读卡状态，下次连上发不了配置。
- callTcpDisconnect 上报后必须由上位机释放连接对象，否则事件会循环上报。
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .config import Config
from .models import RawTagEvent, normalize_epc

MAX_BACKOFF_SECONDS = 30.0
INITIAL_BACKOFF_SECONDS = 1.0


class Sdk:
    """真实 SDK 的命名空间。构造时才导入，便于无硬件环境跑测试。"""

    def __init__(self) -> None:
        from uhf.reader import (  # type: ignore[import-not-found]
            EnumG,
            GClient,
            MsgBaseInventoryEpc,
            MsgBaseSetPower,
            MsgBaseSetTagLog,
            MsgBaseStop,
        )

        self.GClient = GClient
        self.EnumG = EnumG
        self.MsgBaseInventoryEpc = MsgBaseInventoryEpc
        self.MsgBaseSetPower = MsgBaseSetPower
        self.MsgBaseSetTagLog = MsgBaseSetTagLog
        self.MsgBaseStop = MsgBaseStop


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """SDK 文档写 Epc/Rssi/AntId，示例代码用 .epc/.result/.tid，两种都兼容。"""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


class ReaderClient:
    def __init__(self, cfg: Config, out_queue: "queue.Queue[RawTagEvent]",
                 sdk: Sdk | None = None, sleep: Callable[[float], None] = time.sleep):
        self._cfg = cfg
        self._out = out_queue
        self._sdk = sdk if sdk is not None else Sdk()
        self._sleep = sleep
        self._client: Any = None
        self._stop = threading.Event()
        self._disconnected = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rfid-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._disconnected.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---------- 回调：运行在 SDK 自己的线程里，只入队 / 只置标志 ----------

    def _on_epc(self, epc_info: Any) -> None:
        if _attr(epc_info, "result", "Result", default=0) != 0:
            return
        epc = _attr(epc_info, "epc", "Epc", default="")
        if not epc:
            return
        self._out.put(RawTagEvent(
            epc=normalize_epc(str(epc)),
            rssi=_attr(epc_info, "rssi", "Rssi"),
            ant_id=_attr(epc_info, "antId", "AntId", "ant_id"),
            reader_addr=f"{self._cfg.rdr_host}:{self._cfg.rdr_port}",
            read_at=datetime.now().isoformat(timespec="seconds"),
        ))

    def _on_epc_over(self, _epc_over: Any) -> None:
        pass

    def _on_disconnect(self, *_args: Any) -> None:
        self._disconnected.set()

    # ---------- 主循环 ----------

    def _run(self) -> None:
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stop.is_set():
            if self._try_connect():
                backoff = INITIAL_BACKOFF_SECONDS
                while not self._stop.is_set() and not self._disconnected.wait(timeout=0.5):
                    pass
                self._cleanup()
                self._disconnected.clear()
            if self._stop.is_set():
                break
            self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _antenna_enable(self) -> int:
        return getattr(self._sdk.EnumG, f"AntennaNo_{self._cfg.rdr_antenna}").value

    def _try_connect(self) -> bool:
        sdk = self._sdk
        try:
            client = sdk.GClient()
            if not client.openTcp((self._cfg.rdr_host, self._cfg.rdr_port)):
                return False

            client.callEpcInfo = self._on_epc
            client.callEpcOver = self._on_epc_over
            client.callTcpDisconnect = self._on_disconnect
            self._client = client

            # 配置消息必须在设备空闲态发送，因此先于 Inventory
            client.sendSynMsg(sdk.MsgBaseSetTagLog(
                RepeatedTime=self._cfg.rdr_repeated_time
            ))
            if self._cfg.rdr_power is not None:
                client.sendSynMsg(sdk.MsgBaseSetPower(
                    **{str(self._cfg.rdr_antenna): self._cfg.rdr_power}
                ))

            inventory = sdk.MsgBaseInventoryEpc(
                antennaEnable=self._antenna_enable(),
                inventoryMode=sdk.EnumG.InventoryMode_Inventory.value,
            )
            if client.sendSynMsg(inventory) != 0:
                self._cleanup()
                return False

            self.connected = True
            return True
        except Exception:
            self._cleanup()
            return False

    def _cleanup(self) -> None:
        client, self._client = self._client, None
        self.connected = False
        if client is None:
            return
        try:
            client.sendSynMsg(self._sdk.MsgBaseStop())
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_reader.py -v`
Expected: 14 passed

- [ ] **Step 5: 提交**

```bash
git add app/reader.py tests/test_reader.py
git commit -m "feat: 读卡器封装与断线重连"
```

---

## Task 7: 事件总线与刷卡消费线程

**Files:**
- Create: `app/bus.py`
- Modify: `app/service.py`（追加 `SwipeConsumer`）
- Test: `tests/test_bus.py`, `tests/test_service.py`（追加）

**Interfaces:**
- Consumes: `SwipeService`（Task 4、5）
- Produces:
  - `app.bus.EventBus(maxsize: int = 100)`
    - `.bind_loop(loop: asyncio.AbstractEventLoop) -> None`
    - `.subscribe() -> asyncio.Queue` / `.unsubscribe(q) -> None`
    - `.publish_threadsafe(item) -> None` — 可从任意线程调用；未绑定 loop 时静默丢弃
    - `.subscriber_count -> int`
  - `app.service.SwipeConsumer(service, in_queue, on_event, poll_timeout=0.5, clock=datetime.now)`
    - `.start() -> None` / `.stop(timeout: float = 5.0) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_bus.py`：
```python
import asyncio
import threading

from app.bus import EventBus


def test_publish_reaches_subscriber():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        bus._publish({"a": 1})
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert asyncio.run(main()) == {"a": 1}


def test_unsubscribe_stops_delivery():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus._publish({"a": 1})
        assert q.empty()
        return bus.subscriber_count

    assert asyncio.run(main()) == 0


def test_publish_without_bound_loop_is_noop():
    EventBus().publish_threadsafe({"a": 1})  # 不应抛异常


def test_slow_subscriber_is_dropped_not_blocking():
    async def main():
        bus = EventBus(maxsize=2)
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        for i in range(10):
            bus._publish(i)
        return q.qsize()

    assert asyncio.run(main()) == 2


def test_publish_threadsafe_from_worker_thread():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        worker = threading.Thread(target=lambda: bus.publish_threadsafe("hello"))
        worker.start()
        worker.join()
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert asyncio.run(main()) == "hello"


def test_multiple_subscribers_all_receive():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        a, b = bus.subscribe(), bus.subscribe()
        bus._publish("x")
        return (await asyncio.wait_for(a.get(), 1.0),
                await asyncio.wait_for(b.get(), 1.0))

    assert asyncio.run(main()) == ("x", "x")
```

在 `tests/test_service.py` 末尾追加（顶部补 `import queue`、`import time`）：

```python
# ---------- Task 7: 消费线程 ----------

def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_consumer_delivers_processed_events(db):
    service = SwipeService(db, 10.0, 120.0)
    tag_queue: queue.Queue = queue.Queue()
    seen = []
    consumer = SwipeConsumer(service, tag_queue, seen.append, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        assert _wait_until(lambda: len(seen) == 1)
        assert seen[0].name == "李弘毅"
    finally:
        consumer.stop()


def test_consumer_drops_deduped_events(db):
    service = SwipeService(db, 10.0, 120.0)
    tag_queue: queue.Queue = queue.Queue()
    seen = []
    consumer = SwipeConsumer(service, tag_queue, seen.append, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        assert _wait_until(lambda: len(seen) == 1)
        tag_queue.put(_tag())          # 去重窗口内 → process 返回 None
        time.sleep(0.3)
        assert len(seen) == 1
    finally:
        consumer.stop()


def test_consumer_survives_service_exception(db):
    class Boom:
        def process(self, tag, now):
            raise RuntimeError("boom")

    tag_queue: queue.Queue = queue.Queue()
    consumer = SwipeConsumer(Boom(), tag_queue, lambda _e: None, poll_timeout=0.05)
    consumer.start()
    try:
        tag_queue.put(_tag())
        time.sleep(0.2)
        assert consumer._thread.is_alive()
    finally:
        consumer.stop()


def test_consumer_stop_joins_thread(db):
    service = SwipeService(db, 10.0, 120.0)
    consumer = SwipeConsumer(service, queue.Queue(), lambda _e: None, poll_timeout=0.05)
    consumer.start()
    consumer.stop()
    assert not consumer._thread.is_alive()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_bus.py tests/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.bus'`；`ImportError: cannot import name 'SwipeConsumer'`

- [ ] **Step 3: 实现 `app/bus.py`**

```python
"""线程安全的 SSE 事件广播。

消费线程通过 publish_threadsafe() 投递，事件在 asyncio 事件循环里分发给各订阅者。
订阅者队列满时丢弃最旧的行为是刻意的：慢客户端不应该拖住读卡链路。
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 100):
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish_threadsafe(self, item: Any) -> None:
        """可从任意线程调用。未绑定事件循环时静默丢弃（例如纯 CLI 场景）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._publish, item)
        except RuntimeError:
            pass

    def _publish(self, item: Any) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass
```

- [ ] **Step 4: 在 `app/service.py` 追加 `SwipeConsumer`**

文件顶部的 import 区改为：

```python
import queue
import threading
from datetime import datetime
from typing import Callable
```

文件末尾追加：

```python
class SwipeConsumer:
    """从队列取 RawTagEvent、跑业务逻辑、把非 None 的结果交给 on_event。

    单条事件处理失败不能杀死线程 —— 否则一次脏数据就会让整个看板停止更新。
    """

    def __init__(self, service: SwipeService,
                 in_queue: "queue.Queue[RawTagEvent]",
                 on_event: Callable[[SwipeEvent], None],
                 poll_timeout: float = 0.5,
                 clock: Callable[[], datetime] = datetime.now):
        self._service = service
        self._in = in_queue
        self._on_event = on_event
        self._poll_timeout = poll_timeout
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="swipe-consumer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tag = self._in.get(timeout=self._poll_timeout)
            except queue.Empty:
                continue
            try:
                event = self._service.process(tag, self._clock())
            except Exception:
                continue
            if event is not None:
                self._on_event(event)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_bus.py tests/test_service.py -v`
Expected: `test_bus.py` 6 passed；`test_service.py` 22 passed

- [ ] **Step 6: 提交**

```bash
git add app/bus.py app/service.py tests/test_bus.py tests/test_service.py
git commit -m "feat: SSE 事件总线与刷卡消费线程"
```

---

## Task 8: Web API 与 SSE

**Files:**
- Modify: `app/models.py`（追加 `swipe_to_dict` / `student_to_dict`）
- Create: `app/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `Config`（1）、`Database`（2）、`SwipeService`（4、5）、`EventBus`（7）
- Produces:
  - `app.models.swipe_to_dict(event: SwipeEvent) -> dict` — 键：`id/epc/student_no/name/direction/status/rssi/ant_id/reader_addr/swiped_at`，枚举转 `.value`
  - `app.models.student_to_dict(student: Student) -> dict` — 键：`epc/student_no/name/in_campus/last_swipe_at`
  - `app.api.create_app(cfg: Config, db: Database, service: SwipeService, reader, bus: EventBus) -> FastAPI`
  - `reader` 只要求有 `.connected: bool` 属性（鸭子类型），便于测试注入假对象

**端点：** `GET /api/health`、`GET /api/stats/today`、`GET /api/students`、`GET /api/swipes`、`GET /api/events/stream`、`POST /api/simulate`，以及把 `app/web/` 挂到 `/` 的 `StaticFiles`

- [ ] **Step 1: 写失败的测试**

`tests/test_api.py`：
```python
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
        use_device_beep=False, http_host="0.0.0.0", http_port=8000,
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
```

> 注意：本任务**不**测试首页静态文件 —— `app/web/` 要到 Task 10 才创建。
> `create_app` 里 `if WEB_DIR.exists()` 的判断保证了此时不会挂载静态目录，
> 首页相关测试放在 Task 10 的 `tests/test_web.py`。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 3: 在 `app/models.py` 末尾追加序列化函数**

```python
def swipe_to_dict(event: "SwipeEvent") -> dict:
    return {
        "id": event.id,
        "epc": event.epc,
        "student_no": event.student_no,
        "name": event.name,
        "direction": event.direction.value,
        "status": event.status.value,
        "rssi": event.rssi,
        "ant_id": event.ant_id,
        "reader_addr": event.reader_addr,
        "swiped_at": event.swiped_at,
    }


def student_to_dict(student: "Student") -> dict:
    return {
        "epc": student.epc,
        "student_no": student.student_no,
        "name": student.name,
        "in_campus": student.in_campus,
        "last_swipe_at": student.last_swipe_at,
    }
```

- [ ] **Step 4: 实现 `app/api.py`**

```python
"""FastAPI 路由：REST + SSE + 静态前端。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .bus import EventBus
from .config import Config, describe_timezone
from .db import Database
from .models import RawTagEvent, student_to_dict, swipe_to_dict
from .service import SwipeService

WEB_DIR = Path(__file__).parent / "web"

SSE_KEEPALIVE_SECONDS = 15.0


def create_app(cfg: Config, db: Database, service: SwipeService,
               reader: Any, bus: EventBus) -> FastAPI:
    app = FastAPI(title="校园出入看板", version="1.0.0")

    @app.on_event("startup")
    async def _bind_bus_loop() -> None:
        bus.bind_loop(asyncio.get_running_loop())

    @app.get("/api/health")
    def health() -> dict:
        return {
            "reader_connected": bool(getattr(reader, "connected", False)),
            "timezone": describe_timezone(),
            "now": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/stats/today")
    def stats_today() -> dict:
        return db.today_stats(datetime.now().date().isoformat())

    @app.get("/api/students")
    def students() -> list[dict]:
        return [student_to_dict(s) for s in db.list_students()]

    @app.get("/api/swipes")
    def swipes(name: str | None = None,
               student_no: str | None = None,
               date: str | None = None,
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=200)) -> dict:
        rows = db.list_swipes(name=name, student_no=student_no, date=date,
                              page=page, size=size)
        return {
            "total": db.count_swipes(name=name, student_no=student_no, date=date),
            "page": page,
            "size": size,
            "items": [swipe_to_dict(e) for e in rows],
        }

    @app.get("/api/events/stream")
    async def events_stream(request: Request) -> StreamingResponse:
        q = bus.subscribe()

        async def generator():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"   # 心跳，防止反向代理掐断长连接
                        continue
                    yield f"event: swipe\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/simulate")
    def simulate(epc: str = Query(..., description="要模拟刷卡的 EPC")) -> dict:
        tag = RawTagEvent(
            epc=epc, rssi=None, ant_id=None, reader_addr="simulator",
            read_at=datetime.now().isoformat(timespec="seconds"),
        )
        event = service.process(tag, datetime.now())
        if event is None:
            return {"accepted": False, "event": None}
        payload = swipe_to_dict(event)
        bus.publish_threadsafe(payload)
        return {"accepted": True, "event": payload}

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_api.py -v`
Expected: 11 passed

- [ ] **Step 6: 提交**

```bash
git add app/models.py app/api.py tests/test_api.py
git commit -m "feat: REST API 与 SSE 实时推送"
```

---

## Task 9: 应用组装

**Files:**
- Create: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: 全部前置任务
- Produces:
  - `app.main.build(cfg: Config | None = None, reader=None) -> FastAPI` — `reader` 为 `None` 时构造真实 `ReaderClient`；测试注入假对象
  - `app.main.create_app() -> FastAPI` — uvicorn 工厂入口
  - **模块级不定义 `app`**，避免 import 时就尝试连接读卡器

**生命周期：** startup 时 `reader.start()` + `consumer.start()`；shutdown 时 `consumer.stop()` → `reader.stop()` → `db.close()`

- [ ] **Step 1: 写失败的测试**

`tests/test_main.py`：
```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import build

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
        use_device_beep=False, http_host="0.0.0.0", http_port=8000,
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: 实现 `app/main.py`**

```python
"""组装：读卡线程 + 消费线程 + FastAPI。

注意：本模块**不在导入时创建应用**，否则 `import app.main` 就会尝试连接读卡器，
测试和无硬件环境都会挂。uvicorn 用工厂模式启动：
    uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .api import create_app
from .bus import EventBus
from .config import Config, describe_timezone, load_config
from .db import Database
from .models import RawTagEvent, swipe_to_dict
from .reader import ReaderClient
from .roster import import_roster
from .service import SwipeConsumer, SwipeService

CONFIG_PATH = Path("config.yaml")


def build(cfg: Config | None = None, reader: Any = None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config(CONFIG_PATH)

    db = Database(cfg.db_path)
    try:
        imported = import_roster(db, cfg.roster_file)
        print(f"[roster] 导入 {imported} 名学生 | {describe_timezone()}")
    except FileNotFoundError:
        print(f"[roster] 警告：名单文件不存在 {cfg.roster_file}，跳过导入")
    except ValueError as exc:
        print(f"[roster] 警告：{exc}，跳过导入")

    bus = EventBus()
    service = SwipeService(db, cfg.dedup_window, cfg.duplicate_window)

    tag_queue: "queue.Queue[RawTagEvent]" = queue.Queue()
    if reader is None:
        reader = ReaderClient(cfg, tag_queue)
    consumer = SwipeConsumer(
        service, tag_queue,
        on_event=lambda event: bus.publish_threadsafe(swipe_to_dict(event)),
    )

    app = create_app(cfg, db, service, reader, bus)

    @app.on_event("startup")
    def _start_workers() -> None:
        reader.start()
        consumer.start()

    @app.on_event("shutdown")
    def _stop_workers() -> None:
        consumer.stop()
        reader.stop()
        db.close()

    return app


def create_app() -> FastAPI:
    """uvicorn 工厂入口。"""
    return build(load_config(CONFIG_PATH))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_main.py -v`
Expected: 6 passed

- [ ] **Step 5: 跑一次全量测试**

Run: `pytest -v`
Expected: 全部通过（99 个用例：config 9 / db 13 / roster 8 / service 22 / reader 14 /
bus 6 / api 11 / web 5 / main 6 / simulate 5）

- [ ] **Step 6: 提交**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: 应用组装与生命周期管理"
```

---

## Task 10: 前端看板

**Files:**
- Create: `app/web/index.html`, `app/web/app.js`, `app/web/style.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `GET /api/health`、`GET /api/stats/today`、`GET /api/swipes`、`GET /api/events/stream`（Task 8）
- Produces: 无（叶子节点）

**测试范围**：静态文件是否正确挂载、页面骨架是否包含 `app.js` 依赖的 DOM id。
交互行为（SSE 推送、提示音、分页）无法用 pytest 覆盖，靠 Task 13 手工验证。

三个文件必须严格按下面内容创建 —— `app.js` 用 `getElementById` 取的那些 id 必须与
`index.html` 中的一致，改任何一个都要同步改另一个。

- [ ] **Step 1: 创建 `app/web/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>校园出入看板</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header class="topbar">
  <h1>校园出入看板</h1>
  <div class="conn">
    <span id="conn-dot" class="dot dot-unknown"></span>
    <span id="conn-text">连接中…</span>
    <button id="beep-toggle" class="btn" type="button">开启提示音</button>
  </div>
</header>

<section class="stats">
  <div class="card"><div class="num" id="stat-in">0</div><div class="label">今日进校</div></div>
  <div class="card"><div class="num" id="stat-out">0</div><div class="label">今日出校</div></div>
  <div class="card"><div class="num" id="stat-incampus">0</div><div class="label">当前在校</div></div>
  <div class="card"><div class="num" id="stat-total">0</div><div class="label">学生总数</div></div>
</section>

<main class="panels">
  <section class="panel">
    <h2>实时刷卡</h2>
    <ul id="live" class="live"><li class="empty">等待刷卡…</li></ul>
  </section>

  <section class="panel">
    <h2>历史记录查询</h2>
    <form id="query" class="query">
      <input id="q-name" placeholder="姓名" autocomplete="off">
      <input id="q-date" type="date">
      <button type="submit" class="btn">查询</button>
      <button type="button" id="q-reset" class="btn btn-ghost">重置</button>
    </form>
    <table class="history">
      <thead><tr><th>时间</th><th>姓名</th><th>学号</th><th>方向</th></tr></thead>
      <tbody id="history"></tbody>
    </table>
    <div class="pager">
      <button id="prev" class="btn btn-ghost" type="button">上一页</button>
      <span id="page-info">第 1 页</span>
      <button id="next" class="btn btn-ghost" type="button">下一页</button>
    </div>
  </section>
</main>

<script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 创建 `app/web/app.js`**

```javascript
'use strict';

const state = { page: 1, size: 20, total: 0, beepOn: false, audioCtx: null };

const DIRECTION_LABEL = { in: '入校', out: '出校', unknown: '—' };
const STATUS_LABEL = { ok: '', unknown_card: '未登记卡', duplicate: '重复刷卡' };
const MAX_LIVE_ITEMS = 50;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + ' → ' + res.status);
  return res.json();
}

function fmtTime(iso) {
  return (iso || '').slice(11, 19);
}

function rowClass(event) {
  if (event.status === 'unknown_card') return 'row-error';
  if (event.status === 'duplicate') return 'row-warn';
  return event.direction === 'in' ? 'row-in' : 'row-out';
}

function labelFor(event) {
  return STATUS_LABEL[event.status] || DIRECTION_LABEL[event.direction];
}

function beep(ok) {
  if (!state.beepOn || !state.audioCtx) return;
  const ctx = state.audioCtx;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = ok ? 880 : 320;
  gain.gain.value = 0.15;
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  osc.stop(ctx.currentTime + (ok ? 0.09 : 0.3));
}

function renderStats(stats) {
  document.getElementById('stat-in').textContent = stats.in_count;
  document.getElementById('stat-out').textContent = stats.out_count;
  document.getElementById('stat-incampus').textContent = stats.in_campus_count;
  document.getElementById('stat-total').textContent = stats.total_students;
}

function pushLive(event) {
  const list = document.getElementById('live');
  const empty = list.querySelector('.empty');
  if (empty) empty.remove();

  const li = document.createElement('li');
  li.className = 'live-item ' + rowClass(event);
  const who = event.name || ('未登记卡 …' + event.epc.slice(-6));
  li.innerHTML = '<span class="t">' + fmtTime(event.swiped_at) + '</span>' +
                 '<span class="who">' + who + '</span>' +
                 '<span class="what">' + labelFor(event) + '</span>';
  list.prepend(li);

  while (list.children.length > MAX_LIVE_ITEMS) {
    list.lastElementChild.remove();
  }
  beep(event.status === 'ok');
}

function connectStream() {
  const es = new EventSource('/api/events/stream');
  es.addEventListener('swipe', (e) => {
    const event = JSON.parse(e.data);
    pushLive(event);
    refreshStats();
    if (state.page === 1) loadHistory();
  });
  return es;
}

async function refreshStats() {
  try {
    renderStats(await getJSON('/api/stats/today'));
  } catch (err) {
    /* 统计刷新失败不打断看板，健康轮询会反映服务状态 */
  }
}

async function pollHealth() {
  const dot = document.getElementById('conn-dot');
  const text = document.getElementById('conn-text');
  try {
    const h = await getJSON('/api/health');
    dot.className = 'dot ' + (h.reader_connected ? 'dot-ok' : 'dot-bad');
    text.textContent = h.reader_connected ? '读卡器已连接' : '读卡器未连接';
  } catch (err) {
    dot.className = 'dot dot-bad';
    text.textContent = '服务不可用';
  }
}

async function loadHistory() {
  const name = document.getElementById('q-name').value.trim();
  const date = document.getElementById('q-date').value;
  const params = new URLSearchParams({ page: state.page, size: state.size });
  if (name) params.set('name', name);
  if (date) params.set('date', date);

  let data;
  try {
    data = await getJSON('/api/swipes?' + params.toString());
  } catch (err) {
    return;
  }
  state.total = data.total;

  const tbody = document.getElementById('history');
  tbody.innerHTML = '';
  for (const e of data.items) {
    const tr = document.createElement('tr');
    tr.className = rowClass(e);
    tr.innerHTML = '<td>' + fmtTime(e.swiped_at) + '</td>' +
                   '<td>' + (e.name || '—') + '</td>' +
                   '<td>' + (e.student_no || '—') + '</td>' +
                   '<td>' + labelFor(e) + '</td>';
    tbody.appendChild(tr);
  }

  const pages = Math.max(1, Math.ceil(state.total / state.size));
  document.getElementById('page-info').textContent =
    '第 ' + state.page + ' / ' + pages + ' 页（共 ' + state.total + ' 条）';
  document.getElementById('prev').disabled = state.page <= 1;
  document.getElementById('next').disabled = state.page >= pages;
}

function initBeep() {
  const btn = document.getElementById('beep-toggle');
  btn.addEventListener('click', () => {
    if (!state.audioCtx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      state.audioCtx = new Ctor();
    }
    state.audioCtx.resume();
    state.beepOn = !state.beepOn;
    btn.textContent = state.beepOn ? '关闭提示音' : '开启提示音';
    if (state.beepOn) beep(true);
  });
}

function init() {
  initBeep();
  connectStream();
  refreshStats();
  loadHistory();
  pollHealth();
  setInterval(pollHealth, 5000);

  document.getElementById('query').addEventListener('submit', (e) => {
    e.preventDefault();
    state.page = 1;
    loadHistory();
  });
  document.getElementById('q-reset').addEventListener('click', () => {
    document.getElementById('q-name').value = '';
    document.getElementById('q-date').value = '';
    state.page = 1;
    loadHistory();
  });
  document.getElementById('prev').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; loadHistory(); }
  });
  document.getElementById('next').addEventListener('click', () => {
    state.page += 1;
    loadHistory();
  });
}

document.addEventListener('DOMContentLoaded', init);
```

- [ ] **Step 3: 创建 `app/web/style.css`**

```css
:root {
  --bg: #0f172a;
  --panel: #1e293b;
  --line: #334155;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --in: #22c55e;
  --out: #38bdf8;
  --warn: #f59e0b;
  --error: #ef4444;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.6 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  border-bottom: 1px solid var(--line);
}

.topbar h1 { margin: 0; font-size: 20px; font-weight: 600; }

.conn { display: flex; align-items: center; gap: 10px; }

.dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--muted); display: inline-block;
}
.dot-ok { background: var(--in); box-shadow: 0 0 8px var(--in); }
.dot-bad { background: var(--error); box-shadow: 0 0 8px var(--error); }

.btn {
  background: #2563eb; color: #fff; border: 0; border-radius: 6px;
  padding: 6px 14px; cursor: pointer; font-size: 13px;
}
.btn:hover { background: #1d4ed8; }
.btn:disabled { opacity: .4; cursor: default; }
.btn-ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
.btn-ghost:hover { background: var(--panel); }

.stats {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 16px; padding: 20px 24px;
}

.card {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 20px;
}
.card .num { font-size: 34px; font-weight: 700; line-height: 1.2; }
.card .label { color: var(--muted); font-size: 13px; }

.panels {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 16px; padding: 0 24px 24px;
}

@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .panels { grid-template-columns: 1fr; }
}

.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 20px; min-height: 320px;
}
.panel h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; color: var(--muted); }

.live { list-style: none; margin: 0; padding: 0; max-height: 420px; overflow-y: auto; }
.live .empty { color: var(--muted); padding: 12px 0; }

.live-item {
  display: grid; grid-template-columns: 72px 1fr auto;
  gap: 12px; align-items: center;
  padding: 10px 12px; border-radius: 8px; margin-bottom: 6px;
  background: rgba(148, 163, 184, .08);
  border-left: 3px solid var(--muted);
  animation: slide-in .25s ease-out;
}
@keyframes slide-in { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; } }

.live-item .t { color: var(--muted); font-variant-numeric: tabular-nums; }
.live-item .who { font-weight: 600; }
.live-item .what { font-size: 13px; color: var(--muted); }

.row-in { border-left-color: var(--in); }
.row-out { border-left-color: var(--out); }
.row-warn { border-left-color: var(--warn); }
.row-error { border-left-color: var(--error); }

.query { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.query input {
  background: var(--bg); border: 1px solid var(--line); color: var(--text);
  border-radius: 6px; padding: 6px 10px; font-size: 13px;
}

.history { width: 100%; border-collapse: collapse; font-size: 13px; }
.history th {
  text-align: left; color: var(--muted); font-weight: 500;
  border-bottom: 1px solid var(--line); padding: 8px 6px;
}
.history td { padding: 8px 6px; border-bottom: 1px solid rgba(51, 65, 85, .5); }
.history tr.row-in td:last-child { color: var(--in); }
.history tr.row-out td:last-child { color: var(--out); }
.history tr.row-warn td:last-child { color: var(--warn); }
.history tr.row-error td:last-child { color: var(--error); }

.pager {
  display: flex; align-items: center; gap: 12px;
  margin-top: 12px; color: var(--muted); font-size: 13px;
}
```

- [ ] **Step 4: 写测试验证静态资源挂载**

`tests/test_web.py`：
```python
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
        use_device_beep=False, http_host="0.0.0.0", http_port=8000,
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_web.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add app/web/index.html app/web/app.js app/web/style.css tests/test_web.py
git commit -m "feat: 网页实时看板前端"
```

---

## Task 11: 模拟器

**Files:**
- Create: `tools/simulate.py`
- Test: `tests/test_simulate.py`

**Interfaces:**
- Consumes: `load_config`（1）、`Database`（2）、`SwipeService`（4、5）、`POST /api/simulate`（8）
- Produces:
  - `tools.simulate.pick_epc(db: Database, want_unknown: bool = False) -> str`
  - `tools.simulate.main(argv: list[str] | None = None) -> int`

**两种模式：**
- 默认 `--http`：POST 到 `http://HOST:PORT/api/simulate`，驱动完整链路（含 SSE 推送）
- `--direct`：绕过 HTTP，直接开库跑 `SwipeService`，用于服务没起来时排查

- [ ] **Step 1: 写失败的测试**

`tests/test_simulate.py`：
```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_simulate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'simulate'`

- [ ] **Step 3: 实现 `tools/simulate.py`**

```python
"""无硬件时模拟刷卡，驱动整条链路。

用法：
    python tools/simulate.py                          # 随机挑一个已登记学生刷一次
    python tools/simulate.py --epc E28011B0...A1C9    # 指定 EPC
    python tools/simulate.py --count 5 --delay 2      # 连刷 5 次，间隔 2 秒
    python tools/simulate.py --unknown                # 模拟未登记卡
    python tools/simulate.py --direct                 # 绕过 HTTP 直接开库（服务没起时用）
    python tools/simulate.py --list                   # 打印名单
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config          # noqa: E402
from app.db import Database                 # noqa: E402
from app.models import RawTagEvent          # noqa: E402
from app.service import SwipeService        # noqa: E402

DEFAULT_CONFIG = Path("config.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="模拟 RFID 刷卡")
    parser.add_argument("--epc", help="指定 EPC；不填则随机挑一个已登记学生")
    parser.add_argument("--count", type=int, default=1, help="刷卡次数，默认 1")
    parser.add_argument("--delay", type=float, default=1.0, help="两次刷卡间隔秒数，默认 1.0")
    parser.add_argument("--unknown", action="store_true", help="模拟未登记卡")
    parser.add_argument("--direct", action="store_true", help="绕过 HTTP，直接读写数据库")
    parser.add_argument("--list", action="store_true", help="打印名单后退出")
    parser.add_argument("--base-url", default=None, help="看板地址，默认从配置拼")
    return parser


def pick_epc(db: Database, want_unknown: bool = False) -> str:
    students = db.list_students()
    if want_unknown or not students:
        return "FFFFFFFFFFFFFFFF"
    return random.choice(students).epc


def _post(base_url: str, epc: str) -> dict:
    url = f"{base_url}/api/simulate?{urllib.parse.urlencode({'epc': epc})}"
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _direct(db: Database, service: SwipeService, epc: str) -> dict:
    tag = RawTagEvent(epc=epc, rssi=None, ant_id=None, reader_addr="simulator",
                      read_at=datetime.now().isoformat(timespec="seconds"))
    event = service.process(tag, datetime.now())
    if event is None:
        return {"accepted": False, "event": None}
    return {
        "accepted": True,
        "event": {"name": event.name, "direction": event.direction.value,
                  "status": event.status.value, "swiped_at": event.swiped_at},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(DEFAULT_CONFIG)
    db = Database(cfg.db_path)

    try:
        if args.list:
            for s in db.list_students():
                print(f"{s.epc}  {s.student_no}  {s.name}  "
                      f"{'在校' if s.in_campus else '离校'}")
            return 0

        service = SwipeService(db, cfg.dedup_window, cfg.duplicate_window)
        base_url = args.base_url or f"http://127.0.0.1:{cfg.http_port}"

        for i in range(args.count):
            epc = args.epc or pick_epc(db, want_unknown=args.unknown)
            try:
                if args.direct:
                    result = _direct(db, service, epc)
                else:
                    result = _post(base_url, epc)
            except urllib.error.URLError as exc:
                print(f"请求失败（看板没启动？加 --direct 可绕过 HTTP）：{exc}", file=sys.stderr)
                return 1

            if result.get("accepted"):
                event = result["event"]
                print(f"[{i + 1}/{args.count}] {epc} → {event.get('name') or '未登记卡'} "
                      f"{event.get('direction')} {event.get('status')} {event.get('swiped_at')}")
            else:
                print(f"[{i + 1}/{args.count}] {epc} → 已忽略（去重窗口内）")

            if i + 1 < args.count:
                time.sleep(args.delay)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_simulate.py -v`
Expected: 5 passed

- [ ] **Step 5: 手工验证 direct 模式**

```bash
python tools/simulate.py --list
python tools/simulate.py --direct
python tools/simulate.py --direct
```
Expected: 第一次 `--direct` 输出 `in`，第二次输出 `out`（间隔 > 10 秒的话；若间隔小于 10 秒会显示"已忽略"）

- [ ] **Step 6: 提交**

```bash
git add tools/simulate.py tests/test_simulate.py
git commit -m "feat: 无硬件时的刷卡模拟器"
```

---

## Task 12: Docker 打包

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `README.md`

**Interfaces:**
- Consumes: `app.main:create_app`（Task 9）
- Produces: 可运行的容器镜像

- [ ] **Step 1: 创建 `.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
.pytest_cache
data/*.db
data/*.db-wal
data/*.db-shm
docs
tests
```

注意：**不要**排除 `tools/` —— 设计文档要求模拟器能在容器内跑，用于演示兜底。
`data/` 不整体排除，因为 `data/学生记录.xlsx` 需要进镜像；只排除运行期生成的 `.db` 文件。

- [ ] **Step 2: 创建 `Dockerfile`**

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，改代码时不必重装依赖层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/
COPY config.yaml .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: 创建 `docker-compose.yml`**

```yaml
services:
  campus:
    build: .
    container_name: rfid-campus-demo
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      TZ: Asia/Shanghai
      RDR_HOST: "192.168.1.168"
      RDR_PORT: "8160"
      RDR_ANTENNA: "1"
      RDR_POWER: "30"
      RDR_REPEATED_TIME: "50"
      DEDUP_WINDOW: "10"
      DUPLICATE_WINDOW: "120"
      USE_DEVICE_BEEP: "0"
    restart: unless-stopped
```

- [ ] **Step 4: 创建 `README.md`**

````markdown
# 校园出入 RFID 看板

基于 UHF RFID 读卡器的学生出入校园 Demo。刷卡后网页看板实时显示"姓名 + 进/出 + 时间"，
并更新今日统计。

## 快速开始

```bash
# 1. 准备名单（首次）
mkdir -p data && cp "E:/Code/school/学生记录.xlsx" data/

# 2. 启动
docker compose up --build -d

# 3. 打开看板
#    http://localhost:8000
#    接口文档 http://localhost:8000/docs
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest                                   # 跑测试，不需要读卡器
python -m uvicorn app.main:create_app --factory --reload
```

## 没有读卡器时怎么演示

```bash
python tools/simulate.py                 # 随机挑一个学生刷一次
python tools/simulate.py --count 5 --delay 2
python tools/simulate.py --unknown       # 模拟未登记卡
```

也可以直接在看板页面调 `POST /api/simulate?epc=...`。

## 配置

环境变量优先于 `config.yaml`。常用项：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `RDR_HOST` / `RDR_PORT` | `192.168.1.168` / `8160` | 读卡器地址 |
| `RDR_ANTENNA` | `1` | 使能的天线编号 |
| `RDR_POWER` | `30` | dBm；设为空则不修改设备配置 |
| `DEDUP_WINDOW` | `10` | 秒；此窗口内的重复刷卡静默丢弃 |
| `DUPLICATE_WINDOW` | `120` | 秒；此窗口内标记"重复刷卡"且不翻转状态 |
| `USE_DEVICE_BEEP` | `0` | 设备蜂鸣器；见下方说明 |
| `TZ` | `Asia/Shanghai` | **必须设置**，否则今日统计按 UTC 算会差 8 小时 |

## 已知限制

- **进出方向是"状态翻转"而非真实方向判定**：同一张卡刷一次记为进校，再刷一次记为出校。
  真方向判定需要双天线或 GPI 传感器，本次未做。方向逻辑集中在
  `app/service.py` 的 `decide_direction()`，将来替换只改这一处。
- **设备蜂鸣器默认关闭**：`MsgAppSetBeep` 属于配置类指令，而设备处于连续盘点状态时
  可能无法发送配置指令。刷卡提示音由浏览器端播放，不依赖设备。
- **容器访问读卡器**：Windows Docker Desktop 不支持 `network_mode: host`，容器通过
  bridge + NAT 访问局域网内的读卡器。若不通，可把读卡器配置成"客户端模式"主动连接
  宿主机映射出的端口。

## 目录结构

见 `docs/superpowers/specs/2026-10-15-rfid-campus-demo-design.md`。
````

- [ ] **Step 5: 构建并启动容器**

```bash
docker compose up --build -d
docker compose logs --tail=30
```
Expected: 日志出现 `[roster] 导入 6 名学生 | 时区=...`，且无 traceback

- [ ] **Step 6: 验证容器可用**

```bash
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/api/stats/today
curl -s -X POST "http://localhost:8000/api/simulate?epc=E28011B0A50500756E79A1C9"
curl -s "http://localhost:8000/api/swipes?page=1&size=10"
```
Expected:
- health 返回 `reader_connected` 与含 `时区=` 的字符串
- stats 返回 `total_students: 6`
- simulate 返回 `accepted: true`，`name: "李弘毅"`，`direction: "in"`
- swipes 返回 `total: 1`

- [ ] **Step 7: 验证模拟器能在容器内跑**

```bash
docker compose exec campus python tools/simulate.py --list
docker compose exec campus python tools/simulate.py
```
Expected: 第一条打印 6 名学生；第二条输出一次刷卡结果。
若第二条显示"已忽略（去重窗口内）"，说明距上一步的 curl 不足 10 秒 —— 等 10 秒再跑。

- [ ] **Step 8: 验证数据持久化**

```bash
docker compose restart
curl -s "http://localhost:8000/api/swipes?page=1&size=10"
```
Expected: `total` 仍为 1（数据在挂载卷里，未因重启丢失）

- [ ] **Step 9: 提交**

```bash
git add Dockerfile docker-compose.yml .dockerignore README.md
git commit -m "feat: Docker 打包与使用文档"
```

---

## Task 13: 真机联调与验收

**Files:** 无新增文件（只读验证；若发现问题，回到对应任务修复并补测试）

**前置条件：** 读卡器已上电、已连到与电脑同一局域网、已知其 IP（默认 `192.168.1.168`）。

- [ ] **Step 1: 验证网络可达**

```bash
ping 192.168.1.168
```
Expected: 能 ping 通。若不通，先解决网络问题再继续——后面所有步骤都依赖它。

- [ ] **Step 2: 验证容器能连上读卡器**

```bash
docker compose up --build -d
sleep 10
curl -s http://localhost:8000/api/health
```
Expected: `reader_connected` 为 `true`。

**若为 `false`**（已知风险 1）：
1. 进容器内测试连通性：`docker compose exec campus python -c "import socket; s=socket.create_connection(('192.168.1.168',8160),3); print('ok'); s.close()"`
2. 若容器不通但宿主机通，改用"读卡器作客户端"模式：让读卡器主动连宿主机 `8160` 端口，
   代码侧把 `ReaderClient` 的 `openTcp` 换成 `GServer().openServer(8160)` 方向，
   并在 `docker-compose.yml` 里加上 `- "8160:8160"` 端口映射。
3. 把结论补进设计文档 §12 风险表。

- [ ] **Step 3: 验证刷卡上报**

拿一张已登记的学生卡（例如 `E28011B0A50500756E79A1C9` / 李弘毅）在读卡器上刷。

Expected:
- 浏览器看板 1 秒内出现一条绿色的"李弘毅 入校 HH:MM:SS"
- "今日进校" +1，"当前在校" +1
- 若已开启提示音，能听到"嘀"一声

- [ ] **Step 4: 验证方向翻转**

等 10 秒以上，同一张卡再刷一次。
Expected: 出现蓝色的"李弘毅 出校"，"今日出校" +1，"当前在校" -1

- [ ] **Step 5: 验证去重窗口**

10 秒内连刷同一张卡两次。
Expected: 只产生一条记录，看板只多一条

10–120 秒内再刷。
Expected: 出现黄色"⚠重复刷卡"，且"当前在校"数字**不变**

- [ ] **Step 6: 验证未登记卡告警**

拿一张不在名单里的卡刷。
Expected: 出现红色"未登记卡 …XXXXXX"告警，"当前在校"不变

- [ ] **Step 7: 验证断线重连**

拔掉读卡器的网线或网口。
Expected: 5 秒内看板指示灯变红、显示"读卡器未连接"

插回网线。
Expected: 退避重连后指示灯变绿，刷卡恢复正常（退避最长 30 秒，等待即可）

- [ ] **Step 8: 验证历史查询**

在看板右侧输入"李弘毅"，点查询。
Expected: 只显示李弘毅的记录

输入今天的日期，点查询。
Expected: 只显示今天的记录

翻页按钮可用，页码显示正确。

- [ ] **Step 9: 跑全量测试**

```bash
pytest -v
```
Expected: 全部通过

- [ ] **Step 10: 逐条核对验收标准**

对照设计文档 §15 的 9 条验收标准，逐条确认。任何一条不通过，回到对应任务修复，
补上能复现该问题的测试，再重新验收。

- [ ] **Step 11: 提交验收记录**

把实测结果（读卡器型号、固件版本、实际 IP、各步骤结论、发现的偏差）追加到
`docs/superpowers/specs/2026-10-15-rfid-campus-demo-design.md` 的 §12 风险表之后，
新增一节「实测记录」。

```bash
git add docs/
git commit -m "docs: 真机联调实测记录"
```

---

## 附：任务依赖关系

```
Task 1 (配置)
  └─ Task 2 (数据层)
       └─ Task 3 (名单导入)
            └─ Task 4 (方向翻转)
                 └─ Task 5 (去重窗口)
                      └─ Task 7 (事件总线 + 消费线程)
                           └─ Task 8 (API)
                                └─ Task 9 (组装)
                                     ├─ Task 10 (前端)
                                     ├─ Task 11 (模拟器)
                                     └─ Task 12 (Docker)
                                          └─ Task 13 (真机联调)
Task 6 (读卡器封装) ── 依赖 Task 1、2、3，与 Task 4-5 并行，供 Task 9 使用
```

Task 6 可以在 Task 4/5 之前或之后做，只要在 Task 9 之前完成即可。
