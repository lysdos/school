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
python tools/simulate.py --direct        # 绕过 HTTP，直接读写数据库
```

也可以直接调 `POST /api/simulate?epc=...`。

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

## 目录结构

见 `docs/superpowers/specs/2026-10-15-rfid-campus-demo-design.md`。
