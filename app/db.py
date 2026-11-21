"""SQLite 连接与表结构。

原始批次(repave_segments / inspect_segments / batches)与交验结果(acceptance)
在同一个事务中提交; 网页只读 acceptance 表, 重启后结果仍在。
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "acceptance.db",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  lane        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK(kind IN ('repave','inspect')),
  batch_no    TEXT NOT NULL,
  uploaded_at INTEGER NOT NULL,          -- UTC 秒
  row_count   INTEGER NOT NULL,
  PRIMARY KEY (lane, kind, batch_no)
);

-- 重铺原始记录: 左闭右开整数米段 [start_m, end_m), completed_at 为完工时刻(UTC秒)
CREATE TABLE IF NOT EXISTS repave_segments (
  lane         TEXT NOT NULL,
  start_m      INTEGER NOT NULL,
  end_m        INTEGER NOT NULL,
  completed_at INTEGER NOT NULL,
  batch_no     TEXT NOT NULL,
  CHECK (start_m >= 0 AND end_m > start_m)
);
CREATE INDEX IF NOT EXISTS idx_repave_lane ON repave_segments(lane, start_m, end_m);
-- 覆盖索引: 区间重算的范围查询只读索引, 不回表
CREATE INDEX IF NOT EXISTS idx_repave_cover ON repave_segments(lane, start_m, end_m, completed_at);

-- 检测原始记录: 左闭右开整数米段 + 检测时刻(UTC秒) + 设备号 + 跑次号
CREATE TABLE IF NOT EXISTS inspect_segments (
  lane        TEXT NOT NULL,
  start_m     INTEGER NOT NULL,
  end_m       INTEGER NOT NULL,
  measured_at INTEGER NOT NULL,
  device_id   TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  batch_no    TEXT NOT NULL,
  CHECK (start_m >= 0 AND end_m > start_m)
);
CREATE INDEX IF NOT EXISTS idx_inspect_lane ON inspect_segments(lane, start_m, end_m);
-- 覆盖索引: 区间重算的范围查询只读索引, 不回表
CREATE INDEX IF NOT EXISTS idx_inspect_cover ON inspect_segments(lane, start_m, end_m, measured_at, device_id, run_id);

-- 已提交的交验结果(网页直接读取):
--   threshold    该段最近一次重铺完工时刻(UTC秒), 0 表示从未重铺
--   device_count 覆盖整段且时刻有效的不同设备数
--   missing      还缺几台设备 = max(0, 2 - device_count)
--   device1/run1, device2/run2  至多两台各自覆盖整段的设备及其有效跑次
CREATE TABLE IF NOT EXISTS acceptance (
  lane         TEXT NOT NULL,
  start_m      INTEGER NOT NULL,
  end_m        INTEGER NOT NULL,
  threshold    INTEGER NOT NULL,
  device_count INTEGER NOT NULL,
  missing      INTEGER NOT NULL,
  device1      TEXT,
  run1         TEXT,
  device2      TEXT,
  run2         TEXT,
  CHECK (start_m < end_m)
);
CREATE INDEX IF NOT EXISTS idx_acc_lane ON acceptance(lane, start_m, end_m);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def get_conn() -> sqlite3.Connection:
    """autocommit 模式; 写操作由调用方显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK。"""
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
