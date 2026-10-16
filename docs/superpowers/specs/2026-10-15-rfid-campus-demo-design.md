# 基于 RFID 读卡器的学生出入校园 Demo — 设计文档

日期：2026-10-15
状态：**已实施并完成真机联调**（2026-10-16）。实施中的偏差与实测结果见 §7.3、§12、§17。

> 本文档已按最终实现更新。特别提醒：§7.3 描述的是**真机联调后修正过**的设计 ——
> 初版把「状态锚点」和「写入节流」合在一个字段里，导致常驻标签以约 2400 条/分钟
> 的速度写库。那段错误设计的来龙去脉保留在 §7.3 里作为记录。

---

## 1. 背景与目标

学校使用 UHF RFID 校园卡管理学生出入。本次要做一个 Demo 程序，把读卡器读到的标签实时映射成"谁在什么时候进校/出校"，并提供一个可视化看板用于演示。

**目标**：刷卡后 1 秒内，网页看板上出现"姓名 + 进/出 + 时间"，并实时更新今日统计数字。

**输入资料**：

- 读卡器 SDK：`E:\Code\school\Reader-SDK-2.0-ZH-CN-C5-20260512`
- 学生名单：`E:\Code\school\学生记录.xlsx`（表头 `EPC / 学号 / 姓名`，6 条记录）

名单样例：

| EPC | 学号 | 姓名 |
|---|---|---|
| E28011B0A50500756E79A1C9 | 1424764951 | 李弘毅 |
| E28011B0A50500756E7AD945 | 1213311379 | 单钰辰 |
| E28011B0A50500756E7AD9B8 | 1639469262 | 李宇泽 |
| E28011B0A50500756E7AD9D5 | 1336824849 | 曹福璟 |
| E28011B0A50500756E7B0135 | 1187616237 | 李铭洋 |
| E28011B0A50500756E7B0195 | 1162969113 | 刘子东 |

---

## 2. 范围

### 做

- 读卡器 TCP 连接、连续盘点、断线自动重连
- EPC → 学生映射，进出方向判定，记录落库
- 网页实时看板：实时刷卡列表、今日统计、历史查询
- 异常提醒：未登记卡、重复刷卡
- 刷卡提示音
- Docker 打包，一条命令启动
- 无硬件时的模拟刷卡通道（演示兜底 + 开发调试）

### 不做（明确排除）

- 学生名单的增删改界面（名单从 xlsx 导入，需要改就直接改文件重新导入）
- 家长通知、短信、微信推送
- 迟到/早退/考勤规则判定
- 多读卡器组网、跨校区
- 用户登录与权限
- 真方向判定（双天线/GPI 传感器）——本次用单天线状态翻转，见 §7.1

---

## 3. 技术栈

| 组件 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | 机器已装 3.12.10；SDK 官方提供 Python 包 |
| 读卡器 SDK | `uhfReaderApi-Cykeo==1.0.9` | SDK 自带 `python-api/` 示例，`from uhf.reader import *` |
| Web 框架 | FastAPI + uvicorn | 自带 `/docs` 交互式接口文档，演示时可展示；SSE 支持好 |
| 数据库 | SQLite（标准库 `sqlite3`） | 零安装、零运维，单文件便于挂载卷 |
| 名单读取 | openpyxl | 读 `学生记录.xlsx` |
| 前端 | 原生 HTML/CSS/JS | 无构建步骤，Docker 镜像不需要 Node 阶段 |
| 实时推送 | SSE（`EventSource`） | 数据流单向，比 WebSocket 少一半代码 |
| 容器 | Docker + docker-compose | 用户要求 |

**依赖清单**（`requirements.txt`）：

```
uhfReaderApi-Cykeo==1.0.9
fastapi
uvicorn[standard]
openpyxl
PyYAML
```

---

## 4. 架构

单进程，三个并发单元：

```
┌─────────────────────────────────────────────────────────────┐
│  进程：python -m app.main                                    │
│                                                              │
│  ┌────────────────┐   queue.Queue    ┌───────────────────┐  │
│  │ 读卡线程        │ ───────────────► │  业务消费线程      │  │
│  │ (reader.py)    │  RawTagEvent     │  (service.py)     │  │
│  │                │                  │                   │  │
│  │ GClient        │                  │ 方向翻转 / 去重    │  │
│  │ ├ 连续盘点      │                  │ 异常判定 / 落库    │  │
│  │ ├ 标签回调      │                  │                   │  │
│  │ └ 重连线程      │                  │  写 SQLite        │  │
│  └────────────────┘                  │  SSE 广播          │  │
│         ▲                            └───────────────────┘  │
│         │ TCP 8160                            │             │
│         │                                     ▼             │
│  ┌──────┴──────┐                    ┌───────────────────┐  │
│  │ 读写器       │                    │ uvicorn (FastAPI) │  │
│  │ 192.168.0.30 │                    │ REST + SSE + 静态 │  │
│  └─────────────┘                    └───────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                                               ▲
                                               │ HTTP :9600
                                          ┌────┴────┐
                                          │  浏览器  │
                                          └─────────┘
```

**分层原则**：`reader.py` 只负责"把 EPC 拿出来"，`service.py` 只负责"这个 EPC 意味着什么"。两者通过 `queue.Queue` 解耦。这样 `service.py` 是纯逻辑、不碰硬件，可以直接单元测试；`reader.py` 换成串口或模拟器都不影响业务层。

---

## 5. 目录结构

```
E:\Code\school\rfid-campus-demo\
├── Dockerfile
├── docker-compose.yml
├── requirements.txt            # 运行时依赖
├── requirements-dev.txt        # 测试依赖（-r requirements.txt + pytest + httpx）
├── pytest.ini
├── config.yaml                 # 默认配置（可被环境变量覆盖）
├── .dockerignore
├── README.md
├── data/                       # ← 挂载卷，容器外持久化
│   ├── 学生记录.xlsx            # 名单源文件（从 E:\Code\school\学生记录.xlsx 复制进来）
│   └── campus.db               # SQLite（运行时自动生成）
├── app/
│   ├── __init__.py
│   ├── config.py               # 配置加载：env > config.yaml > 内置默认；时区自检
│   ├── models.py               # 枚举、dataclass、normalize_epc、to_dict 序列化
│   ├── db.py                   # SQLite schema + DAO
│   ├── roster.py               # xlsx → students 表（幂等导入）
│   ├── reader.py               # GClient 封装：连接/重连/盘点/回调 → 队列
│   ├── service.py              # 业务核心：方向翻转、去重、异常 + 消费线程
│   ├── bus.py                  # 线程安全的 SSE 事件广播
│   ├── api.py                  # FastAPI 路由 + SSE + 静态托管
│   ├── main.py                 # 组装：启动读卡线程 + uvicorn
│   └── web/                    # 静态前端，无构建步骤
│       ├── index.html
│       ├── app.js
│       └── style.css
├── tools/
│   └── simulate.py             # 无硬件时模拟刷卡，驱动整条链路（含容器内）
└── tests/
    ├── test_config.py
    ├── test_db.py
    ├── test_roster.py
    ├── test_service.py
    ├── test_reader.py
    ├── test_bus.py
    ├── test_api.py
    ├── test_web.py
    ├── test_main.py
    └── test_simulate.py
```

消费线程把事件交给 `bus.py` 的 `EventBus` 广播给所有 SSE 订阅者。选它单独成一个文件、
而不是塞进 `service.py`，是因为它是纯 asyncio 桥接层（线程 → 事件循环），
和业务逻辑没有关系，单独放着可以独立测试。

---

## 6. 数据模型

### 6.1 SQLite Schema

```sql
CREATE TABLE students (
    epc            TEXT PRIMARY KEY,  -- 大写十六进制，如 E28011B0A50500756E79A1C9
    student_no     TEXT NOT NULL,
    name           TEXT NOT NULL,
    in_campus      INTEGER NOT NULL DEFAULT 0,  -- 0 离校 / 1 在校
    last_swipe_at  TEXT,              -- 状态锚点：每次有效读到就刷新
    last_logged_at TEXT,              -- 写入节流：只在真正写库时刷新
    state_date     TEXT,              -- 状态所属日期（YYYY-MM-DD），用于跨天自动重置
    created_at     TEXT NOT NULL
);

CREATE TABLE swipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    epc         TEXT NOT NULL,
    student_no  TEXT,                 -- 未登记卡为 NULL
    name        TEXT,                 -- 未登记卡为 NULL
    direction   TEXT NOT NULL,        -- 'in' | 'out' | 'unknown'
    status      TEXT NOT NULL,        -- 'ok' | 'unknown_card' | 'duplicate'
    rssi        INTEGER,
    ant_id      INTEGER,
    reader_addr TEXT,                 -- '192.168.0.30:8160'
    swiped_at   TEXT NOT NULL,        -- ISO8601 本地时间
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_swipes_swiped_at ON swipes(swiped_at);
CREATE INDEX idx_swipes_epc       ON swipes(epc);
CREATE INDEX idx_swipes_no_date   ON swipes(student_no, swiped_at);
```

### 6.2 设计决定

- **`in_campus` 直接放 `students` 表**，不单开状态表。名单只有几百人，一表读写最简单；未登记卡没有 students 行，天然不需要跟踪状态。
- **`state_date` 做跨天重置**。不写定时任务，而是在刷卡时比对 `state_date != 今天` 就把 `in_campus` 视为 0。这样程序半夜不跑也没关系，第二天第一个人刷卡时状态自动归零。比 cron 干净。
- **`last_swipe_at` 与 `last_logged_at` 必须分开**（真机联调后补的）。初版只有一个字段，
  导致常驻标签每 10 秒写一条库、永不停止。详见 §7.3。
- **未登记卡不落 `students` 表**，它的两个时间戳放在 `UnknownTagTracker` 内存表里。
  它只是限流器而非业务状态，重启后最多多写一轮，不值得为它建表。
- **未登记卡**：`student_no` / `name` 留 NULL，用 `status='unknown_card'` 区分，`direction='unknown'`。
- **EPC 归一化**：读写器上报的 EPC 与 xlsx 中的都统一转成**大写**存储和比对，避免大小写导致匹配失败。
- **时间戳**：全部用本地时间 ISO8601 字符串存储（容器内 `TZ=Asia/Shanghai`）。SQLite 无原生日期类型，ISO8601 字符串可直接做字典序比较和 `LIKE '2026-10-15%'` 前缀查询。

---

## 7. 业务逻辑

### 7.1 方向判定

本次采用**单天线 + 状态翻转**。同一个人第一次刷判为"进"，再次刷判为"出"，交替。

**这不是真正的方向判定**——它无法区分"刚进门又出门"和"出门又进门"的顺序错误。真方向需要双天线（同一读卡器两根天线判先后）或 GPI 传感器（红外对射/地感）。本 Demo 明确不做，接口上留好扩展位：`service.py` 里方向判定收敛到单个函数 `decide_direction(in_campus: bool) -> Direction`，将来换双天线只需改这一个函数。

### 7.2 处理流程

刷卡事件进来后，`service.py` 按顺序处理：

```
1. EPC 归一化（转大写）
2. 查 students 表
   ├─ 查得到 → 走「已知学生」路径（步骤 3–6）
   └─ 查不到 → 走「未登记卡」路径（步骤 3′–5′），direction='unknown'
3. 跨天重置：state_date != 今天 → 该生 in_campus 视为 0

   计算两个「距今多久」（两个域共用同一套判定，见 `_plan()`）：
     since_swipe = now - last_swipe_at     （距上次有效读到）
     since_log   = now - last_logged_at    （距上次写库）

4. 判定（`_plan()`）：
   ├─ since_swipe < dedup_window(10s)      → 丢弃，什么都不动
   ├─ since_log   < duplicate_window(120s) → 不写库（节流）
   └─ since_swipe >= duplicate_window(120s)→ 翻转状态，status='ok'
      since_swipe <  duplicate_window      → 不翻转，status='duplicate'

5. 写库（若判定允许）→ status = 'ok'（翻转）或 'duplicate'（未翻转）
6. 刷新 last_swipe_at（状态锚点）；若写了库，同时刷新 last_logged_at

3′. 未登记卡没有 students 行，用内存 TTL 表存放同样的两个时间戳
4′. 判定逻辑与步骤 4 完全一致
5′. 写库 → status='unknown_card'，direction='unknown'（永不翻转状态）
```

**关于 `duplicate` 记录的 `direction` 取值**：存"按当前状态本应判定的方向"（该生当时 `in_campus=1` 就存 `'out'`，反之存 `'in'`），而不是 `'unknown'`。理由是这条记录供人工复核用，"他这次像是要进校"是有价值的信息；`status='duplicate'` 已经表明该方向未生效。看板渲染时对 `duplicate` 只显示"⚠重复刷卡"，不显示进/出，避免误导。`unknown_card` 才是 `direction='unknown'`。

### 7.3 状态锚点与写入节流为什么必须分开

**这是真机联调才暴露的问题，初版设计是错的。**

初版把 `last_swipe_at` 同时用作"状态锚点"和"写入节流阀"：判重靠它、防翻转也靠它。
问题在于重复刷卡时会刷新它，于是常驻标签的窗口永远不过期，**每 10 秒就写一条库，永不停止**；
再加上未登记卡这条路径**完全没有去重**，现场 75 个样品标签常驻读取范围时，
实测写入速率约 **2400 条/分钟**，13 分钟写出 26363 条记录。

拆成两个字段后各司其职：

| 字段 | 何时刷新 | 作用 |
|---|---|---|
| `last_swipe_at` | 每次「有效读到」（未被 `dedup_window` 丢弃） | 状态锚点。保证逗留时「在校」不来回翻 |
| `last_logged_at` | 只在真正写库时 | 写入节流。同一 EPC 每个 `duplicate_window` 最多一条 |

结果是两者兼顾：学生在读卡范围内逗留 10 分钟，「在校」状态纹丝不动，
而写库次数被限制在每 120 秒一条。真机复测 **2400 → 23 条/分钟**。

**去重窗口的分工**：

- `dedup_window`（默认 10 秒）：纯软件兜底，静默丢弃，不写库。
- 设备侧同时配置 `MsgBaseSetTagLog(repeatedTime=50)`（单位 10ms，即 500ms），让读写器自己先过滤一遍同标签重复上报。
  注意参数名是**小写 `repeatedTime`** —— SDK 的 PDF 文档写作 `RepeatedTime`，
  但实现是 `kwargs.get("repeatedTime")`，写错不会报错、只会静默失效。见 §12 风险 6。

**代价（已知且接受）**：距上次写库不足 `duplicate_window` 的真实重复刷卡会被节流掉，
不产生记录。也就是说"出门 30 秒又折返"这类事件不会被记下来。
这是为了换取"常驻标签不刷爆数据库"而付出的代价，由配置项 `duplicate_window` 调节。

### 7.4 异常判定小结

| 情况 | status | 是否翻转状态 | 是否写库 | 看板表现 |
|---|---|---|---|---|
| 正常刷卡（距上次事件 ≥ 120s） | `ok` | 是 | 是 | 蓝色/绿色，"李弘毅 入校 08:23" |
| 10s 内重复读到 | — | 否 | 否 | 不显示（静默丢弃） |
| 10–120s 内重复，且距上次写库 < 120s | — | 否 | 否 | 不显示（节流） |
| 10–120s 内重复，但距上次写库 ≥ 120s | `duplicate` | 否 | 是 | 琥珀色，"李弘毅 重复刷卡" |
| EPC 不在名单 | `unknown_card` | 否（无状态） | 同上节流规则 | 红色告警，"未登记卡 E280…A1C9" |

**未登记卡的去重**：与已知学生完全同一套节流规则。真机实测前它完全没有去重，
现场 75 个标签常驻时这一条路径就贡献了绝大部分写入量。

---

## 8. 读卡器封装（`app/reader.py`）

```python
class ReaderClient:
    def start(self):   # 连接 + 配置 + 启动连续盘点，失败则进入重连循环
    def stop(self):    # MsgBaseStop + close，释放资源
```

### 8.1 关键约束（来自 SDK 文档）

- **回调里绝对不能调 `sendSynMsg`**。SDK 的 `TcpServer.py` 示例明确注释："请勿在任何事件里调用同步消息 sendSynMsg，可以采用异步调用"。所以 `callEpcInfo` 回调只做一件事：把 `(epc, rssi, ant_id, 时间戳)` 塞进 `queue.Queue`，立即返回。所有需要发指令的动作（重连、蜂鸣器）都在别的线程做。
- **重连用独立线程 + 指数退避**（1s → 2s → 4s → … → 上限 30s）。`callTcpDisconnect` 回调只置一个 `disconnected` 标志并唤醒重连线程，不在回调里做重连。SDK 文档指出该事件上报后必须由上位机释放连接对象，否则事件会循环上报。
- **配置消息必须在空闲态发送**。文档："当读写器处于读卡状态时，所有配置消息将无法发送，必须发送停止指令"。所以启动序列是 `openTcp` → `MsgBaseSetTagLog` → `MsgBaseInventoryEpc`（进入连续盘点）；退出时先 `MsgBaseStop` 再 `close()`，否则设备会一直停在读卡状态，下次连上发不了配置指令。

### 8.2 启动序列

```
openTcp((RDR_HOST, RDR_PORT))
  → MsgBaseSetTagLog(RepeatedTime=50)          # 设备侧去重
  → MsgBaseSetPower({antenna: power})           # 可选，配置了才发
  → MsgBaseInventoryEpc(antennaEnable, InventoryMode_Inventory)   # 连续盘点
```

### 8.3 蜂鸣器 —— 待真机验证

`MsgAppSetBeep` 在文档里归在「读写器配置与管理」，而文档同时写明"当读写器处于读卡状态时，所有配置消息将无法发送"。两条放一起，意味着**连续盘点期间很可能根本发不出蜂鸣器指令**。

**方案**：主用**前端提示音**（浏览器 Web Audio API，事件到达时"嘀"一声，零硬件风险，Docker 里也照响）；`MsgAppSetBeep` 作为可选的设备侧补充，由配置项 `use_device_beep` 控制，**默认关闭**。等真机实测确认能发再打开。不因为一个不确定项卡住整个 Demo。

---

## 9. Web API（`app/api.py`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/stats/today` | 今日进校数、出校数、当前在校人数、学生总数 |
| GET | `/api/swipes` | 历史查询，参数 `name` / `student_no` / `date` / `registered_only` / `page` / `size` |
| GET | `/api/students` | 学生名单 + 每人当前在校状态 |
| GET | `/api/events/stream` | SSE 实时推送刷卡事件 |
| POST | `/api/simulate` | 模拟刷卡（演示兜底，不依赖真卡） |
| POST | `/api/reset` | **清空所有数据**：删光 `swipes`、学生状态复位为离校。名单保留。不可撤销 |
| GET | `/api/health` | 读卡器连接状态（供前端指示灯） |
| GET | `/docs` | FastAPI 自带的交互式接口文档 |

**响应示例** — `GET /api/stats/today`：

```json
{
  "date": "2026-10-15",
  "in_count": 12,
  "out_count": 8,
  "in_campus_count": 4,
  "total_students": 6
}
```

**SSE 事件** — `GET /api/events/stream`：

```
event: swipe
data: {"id":42,"epc":"E28011B0A50500756E79A1C9","student_no":"1424764951",
       "name":"李弘毅","direction":"in","status":"ok",
       "rssi":-52,"ant_id":1,"swiped_at":"2026-10-15T08:23:11"}
```

选 **SSE 而不是 WebSocket**：数据流是单向的（服务端 → 浏览器），SSE 用原生 `EventSource` 三行代码就接上了，不用引入任何前端库，也没有握手/心跳的额外代码。

---

## 10. 前端（`app/web/`）

单个 `index.html` + 原生 JS + 一个 CSS 文件，直接用 `<script>` 引入，**不装 npm、不打包**。理由：Docker 镜像里多一个 Node 构建阶段就多一处会失败的地方，而看板本身只是"一排数字 + 一个实时列表 + 一个查询表格"。

### 布局

```
┌──────────────────────────────────────────────────────┐
│  校园出入看板                     ● 读卡器已连接      │  ← 连接状态指示灯
├──────────────────────────────────────────────────────┤
│   进校 12    出校 8    在校 4    学生 6              │  ← 统计卡片
├────────────────────────┬─────────────────────────────┤
│  实时刷卡              │  历史记录查询               │
│  ┌──────────────────┐  │  姓名[____] 日期[____] [查] │
│  │李弘毅 入校 08:23 │  │  ┌───────────────────────┐  │
│  │单钰辰 入校 08:25 │  │  │08:23 李弘毅 入校      │  │
│  │曹福璟 ⚠重复刷卡  │  │  │08:25 单钰辰 入校      │  │
│  └──────────────────┘  │  └───────────────────────┘  │
└────────────────────────┴─────────────────────────────┘
```

### 行为

- 页面加载时 `fetch` 一次 `/api/stats/today` 和最近记录，然后订阅 `/api/events/stream`
- 收到 `swipe` 事件：更新统计卡片、在实时列表顶部插入一条、播放提示音
- **连接状态指示灯**：轮询 `/api/health`（5 秒一次）。演示时如果读卡器掉线，一眼就能看出来，而不是对着不动的列表猜"是没人刷卡还是程序挂了"。
- 提示音用 Web Audio API 的 `OscillatorNode` 生成，不依赖音频文件。浏览器自动播放策略要求首次交互后才能出声，所以页面上放一个"开启提示音"按钮，点击后 `AudioContext.resume()`。
- **顶栏「清空数据」按钮**（破坏性操作）：红色描边与普通按钮区分，点击弹 `confirm`
  逐条写明后果（删除所有进出记录 / 学生状态复位 / 名单保留 / 不可撤销）。
  确认后调 `POST /api/reset`，清空本地实时列表并重新拉统计与历史。
  用红色而非普通样式、以及必须二次确认，是因为这个按钮就在「开启提示音」旁边，
  一次误点会抹掉演示数据。
- **历史记录只显示已登记学生**（`registered_only=true`）；未登记卡仍入库，
  只出现在实时刷卡面板的告警里。
- **实时面板同一 EPC 只占一行**：常驻标签反复上报时就地更新时间并提到顶部，
  不再堆叠新行把学生卡刷下去。

---

## 11. Docker

### `Dockerfile`

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app

# SDK 的 uhf.reader.communication 在模块级就 import HidClient/BLEClient，
# 即使本项目只用 TCP 也会连带加载 hidapi —— 不装 libusb 直接报错
RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY tools/ ./tools/
COPY config.yaml .
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

注意 `--factory`：`app.main` 模块**故意不在导入时创建应用**，
否则 `import app.main` 就会尝试连读卡器，测试和无硬件环境都会挂。

### `docker-compose.yml`

```yaml
services:
  campus:
    build: .
    container_name: rfid-campus-demo
    ports:
      # 宿主机端口默认 9600 而不是 8000：Windows 的「排除端口段」包含 7989-8088，
      # Docker 绑定 0.0.0.0:8000 会被系统拒绝（报 access permissions）。
      # 容器内部仍然是 8000，不受影响。
      - "${HOST_PORT:-9600}:8000"
    volumes:
      - ./data:/app/data          # SQLite 和 xlsx 都在这里，容器删了数据还在
    environment:
      TZ: Asia/Shanghai           # 关键：否则"今日统计"按 UTC 算会差 8 小时
      RDR_HOST: "192.168.0.30"    # 本部署实际地址，不是 SDK 示例里的 192.168.1.168
      RDR_PORT: "8160"
      RDR_ANTENNA: "1"
      RDR_POWER: "30"
      RDR_REPEATED_TIME: "50"     # 设备侧去重，单位 10ms
      DEDUP_WINDOW: "10"
      DUPLICATE_WINDOW: "120"
      USE_DEVICE_BEEP: "0"
    restart: unless-stopped
```

### 配置优先级

**环境变量 > `config.yaml` > 内置默认**。Docker 里用环境变量改参数最顺手，不用重新打镜像；本地直接跑 Python 时改 `config.yaml` 更方便。两条路都留着。

### 时区

`python:3.12-slim` 默认 UTC。不设 `TZ` 的话"今日进校人数"会在早上 8 点整重置，跨天重置逻辑也会跟着错位。`config.py` 启动时自检并打印当前时区与当前日期，便于排查。

---

## 12. 已知风险

| # | 风险 | 状态 | 说明 |
|---|---|---|---|
| 1 | 容器访问 LAN 设备 | ✅ 已消除 | Windows Docker Desktop 不支持 `network_mode: host`，实测 bridge + NAT 可直接访问 `192.168.0.30:8160`，无需备选方案。 |
| 2 | SDK 安装 | ✅ 已消除 | `uhfReaderApi-Cykeo==1.0.9` 在 Windows 与 `python:3.12-slim` 容器内都装得上。**但容器里必须装 `libusb-1.0-0`**：SDK 的 `uhf.reader.communication` 在模块级就 import `HidClient`/`BLEClient`，即使本项目只用 TCP 也会连带加载 hidapi。已写进 Dockerfile。 |
| 3 | 设备侧蜂鸣器发不出去 | ⚠️ 未验证 | 已用前端提示音兜底，`use_device_beep` 默认关闭。 |
| 4 | 单天线非真方向 | ⚠️ 已知限制 | 已明确排除在本次范围外，见 §7.1。方向判定收敛在 `decide_direction()` 单函数内，便于将来替换。 |
| 5 | EPC 大小写不一致 | ✅ 已消除 | 统一转大写存储与比对。 |
| 6 | **SDK 文档与实现参数名不一致** | ✅ 已消除 | PDF 文档写 `MsgBaseSetTagLog(RepeatedTime=...)`，实现却是 `kwargs.get("repeatedTime")`（小写）。因为签名是 `**kwargs`，传大写不报错、只静默失效 → 设备侧去重完全不生效。已修正并在 `tests/test_reader.py` 加了一条直接对着真实 SDK 的契约测试钉住行为。**教训：这类 `**kwargs` 接口不能只信文档。** |
| 7 | **Windows 排除端口段** | ✅ 已规避 | 本机保留段含 7989–8088，Docker 绑定 `0.0.0.0:8000` 被系统拒绝（报 access permissions）。宿主机端口改用可覆盖的 `HOST_PORT`，默认 9600。排查命令：`netsh interface ipv4 show excludedportrange protocol=tcp`。 |
| 8 | **常驻标签刷爆数据库** | ✅ 已修复 | 真机联调发现，见 §7.3 与 §17。这是初版设计的真实缺陷，单测全绿也照样发生。 |
| 9 | 读卡器断线重连（真机） | ⚠️ 未验证 | 逻辑有单测覆盖（假 SDK 驱动 `callTcpDisconnect` → 退避重连），但真实拔网线未验证。见 §17 待验证清单。 |

---

## 13. 测试策略

### 自动化测试

- **`tests/test_service.py`** —— 主战场。方向翻转、跨天重置、两级去重窗口、未登记卡，全部是纯逻辑，不碰硬件，用 `tmp_path` 建临时 SQLite 就能跑。重点覆盖：
  - 连续刷两次的窗口边界（恰好 10s、恰好 120s 两侧）
  - `state_date` 跨天时 `in_campus` 归零
  - 同一 EPC 在 10s / 120s 两个阈值两侧的行为
  - 未登记卡不写 students 行、不翻转状态
- **`tests/test_roster.py`** —— 真实读 `学生记录.xlsx`，验证 6 条记录导入正确、EPC 归一化为大写、重复导入幂等（不产生重复行）。
- **`tests/test_db.py`** —— schema 建立、索引存在、查询分页与筛选。

### 手工验证

- **`tools/simulate.py`** —— 命令行模拟刷卡，无硬件时驱动整条链路（含容器内跑）。支持指定 EPC 或随机挑一个已登记学生。
- **`POST /api/simulate`** —— 网页侧的模拟入口，演示兜底。
- **真机联调清单**：连接成功 → 连续盘点上报 → 拔网线看重连（观察指示灯） → 重启容器看恢复 → 断电重启读卡器看恢复。

`reader.py` 本身不做自动化测试（需要真设备），靠 `tools/simulate.py` 手工验证整条链路。

---

## 14. 配置项一览

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 读卡器地址 | `RDR_HOST` | `192.168.0.30` | 读写器 IP（本部署实际地址） |
| 读卡器端口 | `RDR_PORT` | `8160` | 读写器 TCP 端口 |
| 天线 | `RDR_ANTENNA` | `1` | 使能的天线编号 |
| 功率 | `RDR_POWER` | `30` | dBm；留空则不改设备配置 |
| 设备去重时间 | `RDR_REPEATED_TIME` | `50` | 单位 10ms，即 500ms |
| 软件去重窗口 | `DEDUP_WINDOW` | `10` | 秒；窗口内静默丢弃 |
| 重复刷卡窗口 | `DUPLICATE_WINDOW` | `120` | 秒；窗口内标记 duplicate 且不翻转 |
| 名单文件 | `ROSTER_FILE` | `data/学生记录.xlsx` | xlsx 路径 |
| 数据库 | `DB_PATH` | `data/campus.db` | SQLite 路径 |
| 设备蜂鸣器 | `USE_DEVICE_BEEP` | `0` | 默认关闭，见 §8.3 |
| 时区 | `TZ` | `Asia/Shanghai` | 必须显式设置 |

---

## 15. 验收标准

Demo 完成的判定条件（含 2026-10-16 实测结果）：

| # | 验收项 | 结果 |
|---|---|---|
| 1 | `docker compose up` 一条命令启动，浏览器打开看板 | ✅ 通过（宿主机端口 9600，见 §12 风险 7） |
| 2 | 刷已登记学生卡，1 秒内看板出现"姓名 + 入校 + 时间"，统计 +1 | ✅ 真机通过。6 张卡同批读到全部正确入校 |
| 3 | 同一张卡再刷，显示"出校"，在校人数 −1 | ✅ 真机通过。卡离开范围 >120s 后再读到即翻转为出校 |
| 4 | 刷不在名单里的卡，看板出现红色"未登记卡"告警 | ✅ 真机通过 |
| 5 | 10 秒内连刷只产生一条记录；10–120 秒内再刷标"重复刷卡"且在校人数不变 | ✅ 通过（真机 + 单测，行为见 §7.4） |
| 6 | 重启容器后历史记录仍在 | ✅ 通过（挂载卷生效） |
| 7 | 拔读卡器网线指示灯变红，插回自动重连变绿 | ⚠️ **未验证**，见 §17 待验证清单 |
| 8 | 不接读卡器时 `python tools/simulate.py` 跑通整条链路 | ✅ 通过（含容器内运行） |
| 9 | `pytest` 全部通过 | ✅ 115 passed |

---

## 16. 后续可扩展（本次不做）

- 双天线 / GPI 真方向判定
- 名单管理界面
- 迟到、早退、异常离校的考勤规则引擎
- 家长/班主任通知
- 多校门、多校区组网
- 出入热力图、周月报表

---

## 17. 实测记录（2026-10-16）

### 环境

- 读卡器：**192.168.0.30:8160**（注意：不是 SDK 示例里的 `192.168.1.168`）
- 天线 1，功率 30dBm
- 现场另有约 75 个样品标签常驻读取范围内（`FA2025...`、`E2806894...`、`12345678` 等）

### 验证通过的部分

- TCP 连接、`SetTagLog` → `SetPower` → `Inventory` 指令序列，日志可见完整收发往返
- 读到 EPC + RSSI + 天线编号；`LogBaseEpcInfo` 字段确认为小写驼峰（`epc`/`antId`/`rssi`/`result`）
- 6 张学生卡识别与入校记录、离开范围后再读到翻转为出校
- 未登记卡告警
- Docker 镜像构建、容器启动、数据跨重启保留、容器内模拟器
- 浏览器端：渲染、统计、SSE 实时推送、历史表格、分页、姓名筛选、连接指示灯两态

### 发现并修复的缺陷

**写入泛滥**。初版实测约 **2400 条/分钟**，13 分钟写出 26363 条记录。根因与修复见 §7.3。
修复后真机复测 **23 条/分钟**，降幅 104 倍；6 张学生卡全程保持「在校」状态未误翻转。

值得一提：这个问题在**单测全绿的情况下照样发生** —— 因为当时的测试
`test_unknown_card_is_not_subject_to_dedup` 恰好把这个 bug 当作预期行为锁住了。
这个任务的教训是：单元测试只能验证"代码符合我认为的需求"，验证不了"我认为的需求是对的"。

### 待验证清单

| 项 | 怎么验 |
|---|---|
| 拔网线断线重连 | 拔掉读卡器端网线，看板指示灯应在 5 秒内变红；插回后最长 30 秒（退避上限）内自动重连变绿 |
| 设备侧蜂鸣器 | 打开 `USE_DEVICE_BEEP=1`，确认连续盘点期间 `MsgAppSetBeep` 能否发出（见 §8.3） |
| 真实闸机场景 | 本次测试时卡片常驻读取范围，未模拟"人持卡走过"的短时经过场景 |
