"""FastAPI 路由：REST + SSE + 静态前端。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
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
               reader: Any, bus: EventBus, workers: Any = None) -> FastAPI:
    """装配 FastAPI 应用。

    workers 是一个有 start() / stop() 的对象（读卡线程与消费线程的持有者），
    由 app/main.py 注入。测试里传 None 即可，此时只绑定事件总线。
    用 lifespan 而不是已废弃的 on_event。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        if workers is not None:
            workers.start()
        try:
            yield
        finally:
            if workers is not None:
                workers.stop()

    app = FastAPI(title="校园出入看板", version="1.0.0", lifespan=lifespan)

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
               registered_only: bool = False,
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=200)) -> dict:
        rows = db.list_swipes(name=name, student_no=student_no, date=date,
                              page=page, size=size,
                              registered_only=registered_only)
        return {
            "total": db.count_swipes(name=name, student_no=student_no, date=date,
                                     registered_only=registered_only),
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
                        item = await asyncio.wait_for(
                            q.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
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
