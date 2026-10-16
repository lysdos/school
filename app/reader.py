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

            # 配置消息必须在设备空闲态发送，因此先于 Inventory。
            # 参数名必须是小写 repeatedTime：真实 SDK 的实现是
            #   self.repeatedTime = kwargs.get("repeatedTime", None)
            # 而 PDF 文档写的是 RepeatedTime。传大写会被 **kwargs 静默吞掉，
            # pack() 跳过该字段，设备侧去重完全不生效。
            client.sendSynMsg(sdk.MsgBaseSetTagLog(
                repeatedTime=self._cfg.rdr_repeated_time
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
            # 连接尝试失败的各种原因（网络、SDK 内部错误）都在这里收敛成"重试"，
            # 由 _run 的退避循环负责节流；异常不该冒泡出去杀死读卡线程。
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
