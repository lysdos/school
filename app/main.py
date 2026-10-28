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

from . import api
from .bus import EventBus
from .config import Config, describe_timezone, load_config
from .db import Database
from .models import RawTagEvent, swipe_to_dict
from .reader import ReaderClient
from .roster import import_roster
from .service import SwipeConsumer, SwipeService
from .upload import UploadWorker

CONFIG_PATH = Path("config.yaml")


class _Workers:
    """读卡线程、消费线程、上传线程与数据库连接的生命周期持有者，
    由 FastAPI 的 lifespan 驱动。
    """

    def __init__(self, reader: Any, consumer: SwipeConsumer, upload: UploadWorker,
                 db: Database):
        self._reader = reader
        self._consumer = consumer
        self._upload = upload
        self._db = db

    def start(self) -> None:
        self._reader.start()
        self._consumer.start()
        # 上传 worker 自身有 enabled 判断，禁用了也是空操作
        self._upload.start()

    def stop(self) -> None:
        # 倒序关闭:先停消费线程,再停上传,最后停读卡器
        # —— 上传依赖数据库存活的 swipes,读卡器依赖队列没人塞
        self._consumer.stop()
        self._upload.stop()
        self._reader.stop()
        self._db.close()


def build(cfg: Config | None = None, reader: Any = None,
          upload: UploadWorker | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config(CONFIG_PATH)

    db = Database(cfg.db_path)
    try:
        imported = import_roster(db, cfg.roster_file)
        print(f"[roster] 导入 {imported} 名学生 | {describe_timezone()}")
    except FileNotFoundError:
        print(f"[roster] 警告：名单文件不存在 {cfg.roster_file}，跳过导入")
    except ValueError as exc:
        print(f"[roster] 警告：{exc}，跳过导入")

    if upload is None:
        upload = UploadWorker(cfg, db)

    bus = EventBus()
    service = SwipeService(db, cfg.dedup_window, cfg.duplicate_window)

    tag_queue: "queue.Queue[RawTagEvent]" = queue.Queue()
    if reader is None:
        reader = ReaderClient(cfg, tag_queue)
    consumer = SwipeConsumer(
        service, tag_queue,
        on_event=lambda event: bus.publish_threadsafe(swipe_to_dict(event)),
    )

    return api.create_app(cfg, db, service, reader, bus,
                          workers=_Workers(reader, consumer, upload, db),
                          upload=upload)


def create_app() -> FastAPI:
    """uvicorn 工厂入口：uvicorn app.main:create_app --factory"""
    return build(load_config(CONFIG_PATH))
