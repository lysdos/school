import queue
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
        use_device_beep=False, http_port=8000,
    )


# ---------- 假 SDK ----------

class FakeMsgSetTagLog:
    """镜像真实 SDK 的取值方式，而不是照抄文档里的参数名。

    真实实现（uhf.reader.MsgBaseSetTagLog.__init__）：
        self.repeatedTime = kwargs.get("repeatedTime", None)
    因为是 **kwargs，传错大小写不会报错，只会静默失效。这个假类必须复刻这个行为，
    否则测试永远抓不到参数名写错。
    """

    def __init__(self, **kwargs):
        self.repeatedTime = kwargs.get("repeatedTime", None)


class FakeMsgSetPower:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeMsgInventory:
    def __init__(self, antennaEnable=None, inventoryMode=None):
        self.antennaEnable = antennaEnable
        self.inventoryMode = inventoryMode


class FakeMsgStop:
    pass


class FakeMsgGetBaseVersion:
    """链路存活探针用的消息。"""


class FakeEnumG:
    AntennaNo_1 = SimpleNamespace(value=1)
    AntennaNo_2 = SimpleNamespace(value=2)
    InventoryMode_Inventory = SimpleNamespace(value=1)


class FakeClient:
    """probe_rc=None 模拟「探针发了但设备没应答」，也就是链路已死。"""

    def __init__(self, open_ok=True, inventory_rc=0, probe_rc=0, probe_plan=None):
        self._open_ok = open_ok
        self._inventory_rc = inventory_rc
        self._probe_rc = probe_rc
        self._probe_plan = list(probe_plan) if probe_plan else None
        self.callEpcInfo = None
        self.callEpcOver = None
        self.callTcpDisconnect = None
        self.opened_with = []
        self.sent = []
        self.probe_count = 0
        self.closed = False

    def openTcp(self, addr):
        self.opened_with.append(addr)
        return self._open_ok

    def sendSynMsg(self, msg, timeout=3):
        self.sent.append(msg)
        if isinstance(msg, FakeMsgInventory):
            return self._inventory_rc
        if isinstance(msg, FakeMsgGetBaseVersion):
            self.probe_count += 1
            if self._probe_plan:
                return self._probe_plan.pop(0)
            return self._probe_rc
        return 0

    def close(self):
        self.closed = True


class FakeSdk:
    def __init__(self, clients):
        self._clients = list(clients)
        self.created = 0
        self.EnumG = FakeEnumG
        self.MsgAppGetBaseVersion = FakeMsgGetBaseVersion
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
    assert kinds == ["FakeMsgStop", "FakeMsgSetTagLog", "FakeMsgSetPower",
                     "FakeMsgInventory"]
    assert client.sent[1].repeatedTime == 50
    assert client.sent[2].kwargs == {"1": 30}
    assert client.sent[3].antennaEnable == 1
    assert client.sent[3].inventoryMode == 1


def test_connect_stops_device_before_configuring(cfg):
    """连接序列必须以 MsgBaseStop 开头。

    设备若被上次异常断开的会话留在盘点态，新的盘点指令会被静默丢弃（真机实测），
    重连就会一直失败。先发 Stop 把它打回空闲态。
    """
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    assert isinstance(client.sent[0], FakeMsgStop)


def test_set_tag_log_param_name_matches_real_sdk(cfg):
    """回归测试：参数名必须是小写 repeatedTime。

    真实 SDK 用 kwargs.get("repeatedTime") 取值，PDF 文档却写 RepeatedTime。
    传大写会被静默忽略 → pack() 跳过该字段 → 设备侧去重失效，
    而且不会有任何报错，只有上真机才能发现。
    """
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    tag_log = next(m for m in client.sent if isinstance(m, FakeMsgSetTagLog))
    assert tag_log.repeatedTime == cfg.rdr_repeated_time


def test_real_sdk_set_tag_log_transmits_repeated_time():
    """直接对着真实 SDK 钉住参数名行为（SDK 未安装时跳过）。

    这条测试同时是文档：说明为什么 reader.py 里必须写小写 repeatedTime。
    """
    uhf = pytest.importorskip("uhf.reader")

    good = uhf.MsgBaseSetTagLog(repeatedTime=50)
    good.pack()
    assert good.repeatedTime == 50
    assert good.dataLen > 0, "小写 repeatedTime 应被编码进报文"

    bad = uhf.MsgBaseSetTagLog(RepeatedTime=50)   # PDF 文档里的写法
    bad.pack()
    assert bad.repeatedTime is None
    assert bad.dataLen == 0, (
        "大写 RepeatedTime 被 **kwargs 静默忽略、报文为空，设备侧去重不会生效"
    )


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
    assert kinds == ["FakeMsgStop", "FakeMsgSetTagLog", "FakeMsgInventory"]


def test_connect_fails_when_open_tcp_fails(cfg):
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([FakeClient(open_ok=False)]))
    assert rc._try_connect() is False
    assert rc.connected is False


def test_failed_open_tcp_still_releases_client(cfg):
    """openTcp 失败时也必须 close()。

    SDK 的 openTcp 内部是先建 socket 再 connect，失败时不会关。
    设备长时间断电、每 30 秒重试一次的话，socket 会一直漏。
    """
    client = FakeClient(open_ok=False)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    assert client.closed is True


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
    assert client.sent[2].kwargs == {"2": 30}


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


# ---------- Task 13 修复：主动探针（真机断电场景）----------
# 读卡器断电时 SDK 的 callTcpDisconnect 不触发，只靠它会导致服务永远
# 报「已连接」且永不重连。改用主动探针：盘点期间查询指令照样有应答。

@pytest.fixture()
def fast_heartbeat(monkeypatch):
    import app.reader as reader_mod
    monkeypatch.setattr(reader_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(reader_mod, "HEARTBEAT_TIMEOUT_SECONDS", 0.05)


def test_probe_failure_triggers_reconnect(cfg, fast_heartbeat):
    """探针收不到应答 = 链路已死 → 必须放弃这条连接并重连。"""
    dead = FakeClient(open_ok=True, probe_rc=None)   # 永不应答，模拟断电
    alive = FakeClient(open_ok=True, probe_rc=0)
    sdk = FakeSdk([dead, alive])

    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    try:
        assert _wait_until(lambda: sdk.created == 2 and rc.connected, timeout=5)
        assert dead.closed is True, "死连接必须被关闭，否则 SDK 的忙等线程不会停"
    finally:
        rc.stop()


def test_probe_success_keeps_connection(cfg, fast_heartbeat):
    """探针有应答就不该重连，也不能自己断开。"""
    client = FakeClient(open_ok=True, probe_rc=0)
    sdk = FakeSdk([client])
    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    try:
        assert _wait_until(lambda: rc.connected)
        assert _wait_until(lambda: client.probe_count >= 3, timeout=5)
        assert sdk.created == 1
        assert rc.connected is True
        assert client.closed is False
    finally:
        rc.stop()


def test_single_probe_failure_does_not_disconnect(cfg, fast_heartbeat):
    """偶发丢包不该导致连接抖动：要连续失败两次才判死。"""
    client = FakeClient(open_ok=True, probe_plan=[None, 0] + [0] * 20)
    sdk = FakeSdk([client])
    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    try:
        assert _wait_until(lambda: rc.connected)
        assert _wait_until(lambda: client.probe_count >= 5, timeout=5)
        assert sdk.created == 1
        assert rc.connected is True
    finally:
        rc.stop()


def test_two_consecutive_probe_failures_disconnect(cfg, fast_heartbeat):
    """连续两次失败才判死 —— 把边界钉住。"""
    client = FakeClient(open_ok=True, probe_plan=[None, None] + [0] * 20)
    sdk = FakeSdk([client, FakeClient(open_ok=True, probe_rc=0)])
    rc = ReaderClient(cfg, queue.Queue(), sdk=sdk, sleep=lambda _s: None)
    rc.start()
    try:
        assert _wait_until(lambda: sdk.created == 2, timeout=5)
    finally:
        rc.stop()
