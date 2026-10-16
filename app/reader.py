"""读卡器封装：连接、配置、连续盘点、断线重连。

SDK 约束（来自《RFID读写器开发指南-Python》与 python-api/example/TcpServer.py）：
- 回调内绝对不能调用 sendSynMsg，否则会死锁；回调只入队、只置标志。
- 设备处于读卡状态时无法发送配置消息，所以 SetTagLog 必须在 Inventory 之前。
- 退出时必须先 MsgBaseStop 再 close()，否则设备停在读卡状态，下次连上发不了配置。
- callTcpDisconnect 上报后必须由上位机释放连接对象，否则事件会循环上报。

关于断线检测（真机实测踩过的坑）
--------------------------------
**不能只依赖 SDK 的 callTcpDisconnect。** 读卡器断电时它不会触发：

  TcpClient.startReceived 陷入忙等 —— recv 立即返回空 → sendMsg 报 EPIPE
  （被 sendMsg 内部的 except 吞掉，只 print 不抛）→ 立刻下一轮。
  实测约 8500 次/秒，一个 CPU 核心打满。
  而 callDisConnect 只在 timeout_count == 3 时触发，可 timeout_count
  又被每次 recv 返回清零，永远到不了 3。

结果就是：服务一直报「已连接」，看板指示灯骗人，且永不重连。

所以这里额外加了**主动探针**：设备在连续盘点期间照样会应答查询指令
（真机实测 MsgAppGetBaseVersion 5/5 次有应答、耗时 < 0.1s），
每几秒探一次、连续失败两次就判定链路已死，主动进入重连。
检测到断开后必须 close()，否则 SDK 那个忙等线程不会停。
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

# 链路存活探针。盘点期间查询指令照样有应答（真机实测），
# 所以「主动问一句有没有回音」比等 SDK 的断开回调可靠得多。
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_TIMEOUT_SECONDS = 2.0
# 连续失败几次才判定断开。留一次余量，避免偶发丢包导致连接抖动。
HEARTBEAT_MAX_FAILURES = 2


class Sdk:
    """真实 SDK 的命名空间。构造时才导入，便于无硬件环境跑测试。"""

    def __init__(self) -> None:
        from uhf.reader import (  # type: ignore[import-not-found]
            EnumG,
            GClient,
            MsgAppGetBaseVersion,
            MsgBaseInventoryEpc,
            MsgBaseSetPower,
            MsgBaseSetTagLog,
            MsgBaseStop,
        )

        self.GClient = GClient
        self.EnumG = EnumG
        self.MsgAppGetBaseVersion = MsgAppGetBaseVersion
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
                self._watch_connection()
                self._cleanup()
                self._disconnected.clear()
            if self._stop.is_set():
                break
            self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _watch_connection(self) -> None:
        """守着已建立的连接，直到它断开或进程退出。

        不能无限等 SDK 的断开回调 —— 断电场景下它不会触发（见模块文档），
        改为主用主动探针。
        """
        failures = 0
        next_probe = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        while not self._stop.is_set():
            # SDK 主动报的断开照单全收，能更快反应
            if self._disconnected.wait(timeout=0.25):
                return
            if time.monotonic() < next_probe:
                continue
            next_probe = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
            if self._probe_alive():
                failures = 0
            else:
                failures += 1
                if failures >= HEARTBEAT_MAX_FAILURES:
                    return

    def _probe_alive(self) -> bool:
        """发一条查询指令，看有没有应答。

        返回 None 表示超时未收到任何响应（sendSynMsg 无响应时就是返回 None），
        即链路已死。
        """
        client = self._client
        if client is None:
            return False
        try:
            rc = client.sendSynMsg(self._sdk.MsgAppGetBaseVersion(),
                                   timeout=HEARTBEAT_TIMEOUT_SECONDS)
        except Exception:
            return False
        return rc is not None

    def _antenna_enable(self) -> int:
        return getattr(self._sdk.EnumG, f"AntennaNo_{self._cfg.rdr_antenna}").value

    def _try_connect(self) -> bool:
        sdk = self._sdk
        try:
            client = sdk.GClient()
            if not client.openTcp((self._cfg.rdr_host, self._cfg.rdr_port)):
                # openTcp 内部是先建 socket 再 connect，失败时 SDK 不会关它。
                # 设备长时间断电时每 30 秒重试一次，不管就会一直漏 fd。
                try:
                    client.close()
                except Exception:
                    pass
                return False

            client.callEpcInfo = self._on_epc
            client.callEpcOver = self._on_epc_over
            client.callTcpDisconnect = self._on_disconnect
            self._client = client

            # 先让设备回到空闲态。设备若被上次异常断开的会话留在盘点态，
            # 新的盘点指令会被**静默丢弃**（真机实测返回 None），重连就会一直失败。
            # 对本来就空闲的设备发 Stop 是无害的（SDK 示例也这么做）。
            client.sendSynMsg(sdk.MsgBaseStop())

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
