# 校园出入 RFID 看板

基于 UHF RFID 读卡器的学生出入校园 Demo。刷卡后网页看板实时显示「姓名 + 进/出 + 时间」，
并更新今日统计。

## 快速开始

```bash
# 1. 准备名单（首次）
mkdir -p data && cp "E:/Code/school/学生记录.xlsx" data/

# 2. 启动
docker compose up --build -d

# 3. 打开看板
#    http://localhost:9600
#    接口文档 http://localhost:9600/docs
```

> **为什么是 9600 而不是 8000？** Windows 的「排除端口段」默认包含 7989–8088，
> Docker 绑定 `0.0.0.0:8000` 会被系统拒绝并报 `access permissions` 错误。
> 用 `netsh interface ipv4 show excludedportrange protocol=tcp` 可以查看本机的保留段。
>
> 换端口：`HOST_PORT=9700 docker compose up -d`
> （先确认该端口既不在保留段内、也没被别的程序占用）。
> 容器内部始终监听 8000，不受影响。
>
> 从宿主机跑模拟器时要指到映射后的端口：
> `python tools/simulate.py --base-url http://127.0.0.1:9600`；
> 或者直接在容器里跑，就不用管端口：`docker compose exec campus python tools/simulate.py`

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
python tools/simulate.py --direct        # 绕过 HTTP，直接读写数据库
```

也可以直接调 `POST /api/simulate?epc=...`。

## 演示前清空数据

看板顶栏有个红色的**「清空数据」**按钮，点一下会二次确认，确认后：

- 删除所有进出记录
- 所有学生状态复位为「离校」
- **学生名单保留**

也可以直接调接口：

```bash
curl -X POST http://localhost:9600/api/reset
# {"cleared_swipes":4178,"reset_students":6}
```

> 注意：清空后如果读卡器范围内还有标签，它们会立刻重新上报一次
> （去重表也一起清了，这是「真清空」该有的行为）。想让看板保持干净，
> 把标签拿远再点清空。

## 配置

环境变量优先于 `config.yaml`。常用项：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `RDR_HOST` / `RDR_PORT` | `192.168.1.168` / `8160` | 读卡器地址 |
| `RDR_ANTENNA` | `1` | 使能的天线编号 |
| `RDR_POWER` | `30` | dBm；留空则不改设备配置 |
| `RDR_REPEATED_TIME` | `50` | 设备侧去重，单位 10ms（即 500ms） |
| `DEDUP_WINDOW` | `10` | 秒；此窗口内的重复刷卡静默丢弃 |
| `DUPLICATE_WINDOW` | `120` | 秒；此窗口内标记「重复刷卡」且不翻转状态 |
| `USE_DEVICE_BEEP` | `0` | 设备蜂鸣器，见下方说明 |
| `TZ` | `Asia/Shanghai` | **必须设置**，否则今日统计按 UTC 算会差 8 小时 |

## 已知限制

- **进出方向是「状态翻转」而非真实方向判定**：同一张卡刷一次记为进校，再刷一次记为出校。
  真方向判定需要双天线或 GPI 传感器，本次未做。方向逻辑集中在
  `app/service.py` 的 `decide_direction()`，将来替换只改这一处。
- **设备蜂鸣器默认关闭**：`MsgAppSetBeep` 属于配置类指令，而设备处于连续盘点状态时
  可能无法发送配置指令。刷卡提示音由浏览器端播放，不依赖设备。
- **容器访问读卡器**：Windows Docker Desktop 不支持 `network_mode: host`，容器通过
  bridge + NAT 访问局域网内的读卡器。若不通，可把读卡器配置成「客户端模式」主动连接
  宿主机映射出的端口。
- **`repeatedTime` 参数名是小写**：SDK 的 PDF 文档写作 `RepeatedTime`，但实现是
  `kwargs.get("repeatedTime")`。写错不会报错，只会让设备侧去重静默失效。
  `tests/test_reader.py` 里有一条直接对着真实 SDK 的契约测试钉住这个行为。
- **别让本地进程和容器同时用同一个 `data/`**：两个进程同时打开同一个 SQLite 文件时，
  Docker Desktop 的 bind mount 处理不好锁，容器会报 `sqlite3.OperationalError: disk I/O error`。
  要么只用容器，要么只用本地。用容器时想模拟刷卡就这样跑：
  `docker compose exec campus python tools/simulate.py`。
  （`--epc` 指定卡号的 HTTP 模式已经改成不碰本地数据库，但 `--direct` 和随机挑人仍需要。）

## 目录结构

见 `docs/superpowers/specs/2026-10-15-rfid-campus-demo-design.md`。
