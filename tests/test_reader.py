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
    assert client.sent[0].repeatedTime == 50
    assert client.sent[1].kwargs == {"1": 30}
    assert client.sent[2].antennaEnable == 1
    assert client.sent[2].inventoryMode == 1


def test_set_tag_log_param_name_matches_real_sdk(cfg):
    """回归测试：参数名必须是小写 repeatedTime。

    真实 SDK 用 kwargs.get("repeatedTime") 取值，PDF 文档却写 RepeatedTime。
    传大写会被静默忽略 → pack() 跳过该字段 → 设备侧去重失效，
    而且不会有任何报错，只有上真机才能发现。
    """
    client = FakeClient(open_ok=True)
    rc = ReaderClient(cfg, queue.Queue(), sdk=FakeSdk([client]))
    rc._try_connect()
    assert client.sent[0].repeatedTime == cfg.rdr_repeated_time


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
