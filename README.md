# 公路检测交验系统

FastAPI + SQLite 实现的公路检测交验页。按车道分开上传**重铺**与**检测** CSV,
自动计算每个里程段的交验结论, 网页直接读取已提交的交验结果。

## 交验规则

- 每处(每个原子里程段)要有 **2 台不同设备**的有效跑次覆盖才算合格;
  分段显示"还缺几台设备"(0/1/2), 并列出至多两台各自覆盖整段的设备及其有效跑次。
- 已重铺的地方**只认最近一次完工时刻之后**检测的跑次(严格大于);
  同一位置多次重铺时, 以最近一次完工时刻为准。
- 里程段为**左闭右开**的整数米段 `[start_m, end_m)`, 时刻为 UTC 秒(整数)。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

访问 http://localhost:8000 。SQLite 数据库保存在命名卷 `acceptance-data`
(容器内 `/data/acceptance.db`), 重启后交验结果仍可查询。

### 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

数据库默认在 `./data/acceptance.db`, 可用环境变量 `DB_PATH` 覆盖。

## 使用流程

1. **上传 CSV**(网页第 1 区或 `POST /upload/{kind}`):
   - 按车道分开上传重铺(`kind=repave`)与检测(`kind=inspect`)CSV;
   - 每批一个**批号**; 同车道、同类型、同批号再次上传即**整批更正**
     (旧批整批删除、新批整批写入, 只重算旧批和新批涉及的米段)。
2. **查询交验结果**(网页第 3 区或 `GET /api/acceptance`):
   - 选车道和里程范围, 分段显示还缺几台设备、是否合格、
     至多两台覆盖整段的设备及其有效跑次、最近一次重铺完工时刻。

### CSV 格式(UTF-8, 首行表头)

重铺 CSV:

```csv
start_m,end_m,completed_at
300,600,1700000100
```

检测 CSV:

```csv
start_m,end_m,measured_at,device_id,run_id
0,1000,1700000000,DEV-A,RUN-A1
```

- `start_m`/`end_m`: 整数米, `0 <= start_m < end_m`;
- `completed_at`/`measured_at`: 完工/检测时刻, UTC 秒, 非负整数;
- `device_id`/`run_id`: 设备号/跑次号, 非空文本(<=64 字符);
- 任何一行不合法则**整批拒绝**, 原始批次与交验结果都沿用上次提交。

可运行 `python scripts/gen_sample.py` 生成 `samples/` 演示数据
(含重铺前后跑次失效/复测/补测的完整场景)。

## 一致性与事务

- 原始批次行、批次登记、交验结果在**同一个 SQLite 事务**中提交;
  任何一步失败整体回滚, 两者都沿用上次提交的状态。
- 上传在进程内串行(`threading.Lock` + `BEGIN IMMEDIATE`), WAL 模式下
  查询不阻塞, 网页始终读取最近一次已提交的结果。

## 计算方式(不逐米展开)

- 采用**扫描线算法**: 只以所有区间的端点为事件点切分原子段,
  复杂度 O((记录数) · log + 活跃区间), 与里程长度无关;
  相邻且状态(重铺完工时刻 + 有效设备跑次集合)相同的原子段合并,
  因此列出的每台设备的跑次必然覆盖整个结果段。
- 每车道 20 万条记录下: 1 万行批次上传约 1 秒, 局部整批更正约 0.15 秒
  (见 `scripts/perf_check.py`, 可复测)。
- 更正时交验状态是逐点函数(只取决于覆盖该点的记录), 所以只需重算
  旧批和新批涉及的米段组件; 组件外的既有结果行原样保留。
- 交验结果只生成在有数据的连通米段上: 段内无检测覆盖的空洞显示为
  "缺 2 台", 完全无数据的外围区域不产生结果行。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 交验页(上传、批次列表、查询) |
| POST | `/upload/repave` | 上传/更正重铺批次(multipart: `lane`, `batch_no`, `file`) |
| POST | `/upload/inspect` | 上传/更正检测批次(同上) |
| GET | `/api/acceptance?lane=&start_m=&end_m=` | 查询已提交交验结果(JSON) |
| GET | `/api/batches` | 批次列表(JSON) |
| GET | `/health` | 健康检查 |

## 测试与性能验证

```bash
pip install -r requirements-dev.txt
pytest tests/ -q            # 15 个用例: 规则、整批更正、失败回滚、重启持久化等
python scripts/perf_check.py   # 20 万条/车道的录入、更正、查询耗时
```

## 目录结构

```
app/
  main.py        FastAPI 路由(上传/查询/页面)
  core.py        CSV 解析、扫描线计算、批次应用与局部重算
  db.py          SQLite 连接与表结构
  templates/     交验页模板
tests/test_app.py
scripts/gen_sample.py   生成演示 CSV
scripts/perf_check.py   20 万条性能验证
Dockerfile / docker-compose.yml
```
