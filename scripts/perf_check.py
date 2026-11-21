"""性能验证: 每车道 20 万条记录下的录入、更正与查询耗时。

运行: python scripts/perf_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import apply_batch
from app.db import get_conn, init_db

LANE = "PERF-LANE"
LANE_LEN = 2_000_000      # 2000 km
N_RUNS = 20               # 20 台设备各 1 个跑次
ROWS_PER_RUN = 10_000     # 每跑次 1 万行 -> 共 20 万行检测记录
SEG = LANE_LEN // ROWS_PER_RUN  # 200 m/行


def make_inspect_rows(run_idx):
    rows = []
    dev = f"DEV-{run_idx:02d}"
    run = f"RUN-{run_idx:02d}"
    base = 1_700_000_000 + run_idx  # 各设备检测时刻错开
    for i in range(ROWS_PER_RUN):
        rows.append((i * SEG, (i + 1) * SEG, base, dev, run))
    return rows


def timed(label, fn):
    t0 = time.perf_counter()
    r = fn()
    print(f"{label:<46} {time.perf_counter() - t0:7.2f} s")
    return r


def main():
    db = os.path.join(tempfile.mkdtemp(), "perf.db")
    os.environ["DB_PATH"] = db
    init_db()

    # 1) 20 个检测批次 x 1 万行 = 20 万行
    for k in range(N_RUNS):
        rows = make_inspect = make_inspect_rows(k)
        conn = get_conn()
        timed(f"upload inspect batch {k + 1}/{N_RUNS} (10k rows)", lambda: (
            conn.execute("BEGIN IMMEDIATE"),
            apply_batch(conn, LANE, "inspect", f"JC-{k:02d}", rows, 1_700_100_000),
            conn.commit(),
        ))
        conn.close()

    # 2) 重铺批次: 500 行
    repaves = [(i * 4000, i * 4000 + 1000, 1_700_000_500) for i in range(500)]
    conn = get_conn()
    timed("upload repave batch (500 rows)", lambda: (
        conn.execute("BEGIN IMMEDIATE"),
        apply_batch(conn, LANE, "repave", "RP-1", repaves, 1_700_200_000),
        conn.commit(),
    ))
    conn.close()

    # 3) 整批更正一个局部批次(只影响局部米段)
    conn = get_conn()
    # 先录入一个局部小批
    small = [(100_000, 100_200, 1_700_300_000, "DEV-X", "RUN-X1")]
    timed("upload small inspect batch (1 row)", lambda: (
        conn.execute("BEGIN IMMEDIATE"),
        apply_batch(conn, LANE, "inspect", "JC-X", small, 1_700_300_000),
        conn.commit(),
    ))
    # 更正它: 范围仍在原米段附近 -> 只重算涉及的米段
    small2 = [(100_000, 100_200, 1_700_300_100, "DEV-Y", "RUN-Y1")]
    timed("correct small batch (local recompute)", lambda: (
        conn.execute("BEGIN IMMEDIATE"),
        apply_batch(conn, LANE, "inspect", "JC-X", small2, 1_700_300_100),
        conn.commit(),
    ))

    # 4) 查询
    n = timed("query 10km window", lambda: conn.execute(
        "SELECT COUNT(*) FROM acceptance WHERE lane=? AND start_m<? AND end_m>?",
        (LANE, 60_000, 50_000)).fetchone()[0])
    print(f"  -> {n} segments in window")
    total = conn.execute(
        "SELECT COUNT(*), SUM(missing=0) FROM acceptance WHERE lane=?", (LANE,)
    ).fetchone()
    print(f"acceptance rows: {total[0]}, qualified rows: {total[1]}")
    insp = conn.execute(
        "SELECT COUNT(*) FROM inspect_segments WHERE lane=?", (LANE,)
    ).fetchone()[0]
    print(f"inspect rows in lane: {insp}")
    conn.close()


if __name__ == "__main__":
    main()
