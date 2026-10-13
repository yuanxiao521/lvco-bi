# 事件日志 & 任务队列 · 7 课入门

写给"从没接触过消息队列"的人。每课一个可运行的小 demo，先讲**为什么**，
再给**最小代码**，最后指出**真实项目里的哪一行**。

## 学习路线

| 课 | 文件 | 你会学到 |
|---|---|---|
| 1 | `01_why_log.py` | 为什么"读完就删"会丢事件，日志凭什么能续传 |
| 2 | `02_stream_basics.py` | Redis Stream 五个命令：XADD/XLEN/XRANGE/XREAD/MAXLEN |
| 3 | `03_cursor_resume.py` | **游标续传**——断线重连一条不丢的最小实现 |
| 4 | `04_block_and_timeout.py` | 阻塞读的坑：BLOCK 时长必须小于 socket 超时 ⭐ |
| 5 | `05_ordered_writer.py` | 为什么写日志要"缓冲 + 单写入者"（并发会乱序） |
| 6 | `06_arq_worker.py` + `06_arq_producer.py` | ARQ 任务队列：生产者投递 / 消费者执行 |
| 7 | `07_mini_system.py` | **结业**：把 B 和 A 串成最小系统 |

## 怎么跑

```bash
cd backend

# 1~5、7 课直接跑
.venv/Scripts/python.exe -X utf8 learning/01_why_log.py

# 第 6 课要两个终端
.venv/Scripts/python.exe -m arq learning.06_arq_worker.WorkerSettings   # 终端1
.venv/Scripts/python.exe learning/06_arq_producer.py                    # 终端2
```

前置：本地 Redis（`docker start lvco-redis`，端口 6379）。

## 两个核心概念

### B：事件日志（Redis Stream）—— 解决"读者断线"

```
SSE 是只能往前读的流 → 读者刷新后无法接上
解法：把事件变成"带编号的日志"（只追加），读者记住游标
      XADD 追加 → 返回 ID
      XREAD 从 ID 之后读 ← 不重复不丢失的关键
```

**类比：录像机**（随时回放）。

### A：任务队列（ARQ）—— 解决"执行者挂了"

```
任务跑在 API 进程的 asyncio.Task 里 → 进程重启任务就没了
解法：任务先入队（Redis），由独立 worker 进程消费
      生产端 enqueue_job(函数名, 参数)
      消费端 arq worker 取任务 → 执行
```

**类比：工单系统**（活派给谁干）。

### 为什么两个都要

| | 解决 | 不管 |
|---|---|---|
| B | 事件不丢（读者视角） | 执行者挂了，日志只有半截 |
| A | 任务不丢（执行者视角） | 读者断线，事件还是丢 |

**B + A = 事件不丢 + 任务不丢**，才是生产级形态。

## 三个最容易踩的坑（都在 demo 里复现了）

1. **BLOCK 时长 > socket 超时** → 阻塞读被提前打断（第 4 课）
2. **并发写日志** → 顺序错乱，回放时流程颠倒（第 5 课）
3. **上队列前没抽出执行内核** → worker 进程 import 不到请求闭包里的逻辑
   （这是真实项目里最大的一块改动：315 行内核从 API 模块搬到 services 层）

## 真实项目对照

| demo 里的角色 | 真实项目位置 |
|---|---|
| 事件日志读写 | `app/services/event_stream.py` |
| 执行内核 | `app/services/chat_task.py` |
| 队列生产/消费 | `app/worker.py` |
| SSE 侧读取 | `app/api/v1/ai.py` → `_stream_from_event_log` |
| 开关 | `.env` → `TASK_QUEUE_ENABLED` |
