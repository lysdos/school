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
#    http://localhost:10000
#    接口文档 http://localhost:10000/docs
```

> **为什么是 10000 而不是 8000？** Windows 的「排除端口段」包含 7989–8088，
> Docker 绑定 `0.0.0.0:8000` 会被系统拒绝并报 `access permissions` 错误。
> 用 `netsh interface ipv4 show excludedportrange protocol=tcp` 可以查看本机的保留段。
>
> 历史教训：9600 也中过招 —— Windows 每次重启后动态保留段会漂移，
> 2026-10-20 重启后 9600 落进了 9529–9628，容器起不来（同样的 `access permissions` 报错）。
> 排除段不是固定的，换端口前一定要先跑一遍上面的 netsh 命令确认。
>
> 换端口：`HOST_PORT=10001 docker compose up -d`
> （先确认该端口既不在保留段内、也没被别的程序占用）。
> 容器内部始终监听 8000，不受影响。
>
> 从宿主机跑模拟器时要指到映射后的端口：
> `python tools/simulate.py --base-url http://127.0.0.1:10000`；
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
curl -X POST http://localhost:10000/api/reset
# {"cleared_swipes":4178,"reset_students":6}
```

> 注意：清空后如果读卡器范围内还有标签，它们会立刻重新上报一次
> （去重表也一起清了，这是「真清空」该有的行为）。想让看板保持干净，
> 把标签拿远再点清空。

## 出校语音播报

浏览器右上角「开启提示音+语音」开起来后,每次有人正常出校(`direction=out`,
`status=ok`),浏览器会用中文播一遍「姓名,X 班,出校」。

- 优先用浏览器自带的 SpeechSynthesisUtterance(`SpeechSynthesisUtterance`,
  zh-CN),零依赖。
- 不支持的浏览器回退到双音提示,**不会** 装作播了。
- 注意:同一浏览器的 `speechSynthesis` 队列有长度上限,所以代码里每次先
  `cancel()` 再 `speak()`,常驻标签反复上报时只会播最新一条,不会堆积。

需要关掉就再点一次按钮。

## 后台管理(考勤系统对接)

`config.yaml` 里设 `admin_password` 之后,`/api/admin/*` 整组接口启用,顶栏
也会出现一个「管理」入口(默认隐藏)。

后台能做四件事:

1. **学生增删改**(连 `EPC ↔ 学号 ↔ 姓名 ↔ 班级`):跟 xlsx 同步写,
   `upsert_student` 是幂等的,不会清空在馆状态。
2. **刷卡记录查询 / 修订 / 删除**:支持按姓名、日期、方向、状态、是否已
   上传做组合过滤;修订可以改 `direction`、`status`、`swiped_at` 一项或多项。
3. **立即重传**:点一下就把所有 `uploaded_at IS NULL` 的记录立刻
   POST 给接收端。
4. **查看上传配置**:`/api/admin/upload/config` 给出当前生效的 URL/Token/参数。
   修改仍走环境变量(下面)。

```bash
curl -c /tmp/c.txt -X POST localhost:10000/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"your-password"}'

curl -b /tmp/c.txt localhost:10000/api/admin/students
```

## 数据上传(考勤系统对接)

worker 每 15 秒拉一批 `uploaded_at IS NULL` 的刷卡记录,POST 到配置的目标 URL。

- 接收端期望的请求格式:`POST <url>`,Header `Content-Type: application/json`,
  可选 `Authorization: Bearer <token>`,Body 是 `{"events": [<swipe>, ...]}`。
  单条内容跟看板 SSE 推的 `event: swipe` 数据相同(包含姓名、班级、
  方向、状态、时间戳等),可以直接存为考勤系统的考勤记录。
- 2xx 视为成功,worker 把这一批的 `uploaded_at` 戳上;
  非 2xx 或网络错误整批留到下一轮再试。
- 没配置 `UPLOAD_URL` 或 `UPLOAD_ENABLED` 不是 `1` 时 worker 不启动,
  零额外开销,演示场景不接对接系统也能照跑。

```yaml
# config.yaml 示例
upload_enabled: true
upload_url: "https://school-platform.example.com/api/attendance/ingest"
upload_token: "your-bearer-token"
upload_timeout: 5.0
upload_interval: 15.0
upload_batch_size: 50
```

`tools/update_roster_with_class.py` 用来把现有的 3 列名单 xlsx
(只有 EPC/学号/姓名)升级到 4 列,把班级留空,后台或 Excel 填完就行。

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
| `UPLOAD_ENABLED` | `0` | `1` 时启动上传 worker（还需 `UPLOAD_URL` 非空） |
| `UPLOAD_URL` | `""` | 接收 swipes 的 HTTP 端点 |
| `UPLOAD_TOKEN` | `""` | Bearer Token；可留空 |
| `UPLOAD_TIMEOUT` | `5` | 单次 HTTP 超时秒数 |
| `UPLOAD_INTERVAL` | `15` | 轮询间隔秒数 |
| `UPLOAD_BATCH_SIZE` | `50` | 单批最多条数 |
| `ADMIN_PASSWORD` | `""` | 后台管理口令；**留空时整个后台不挂路由**，从外部探测不到 |

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
