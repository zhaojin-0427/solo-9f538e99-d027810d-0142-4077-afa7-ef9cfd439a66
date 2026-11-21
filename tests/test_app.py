"""端到端与单元测试。

运行: pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.core import merge_ranges, sweep_segments


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, kind, lane, batch, csv_text):
    r = client.post(
        f"/upload/{kind}",
        data={"lane": lane, "batch_no": batch},
        files={"file": ("data.csv", csv_text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"]


def segs(client, lane, a, b):
    r = client.get("/api/acceptance", params={"lane": lane, "start_m": a, "end_m": b})
    assert r.status_code == 200
    return r.json()["segments"]


INSPECT_H = "start_m,end_m,measured_at,device_id,run_id\n"
REPAVE_H = "start_m,end_m,completed_at\n"


# ------------------------------------------------------------ 基本合格判定

def test_two_devices_qualified(client):
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,1000,100,DEV-A,R1\n0,1000,101,DEV-B,R1\n")
    out = segs(client, "L1", 0, 1000)
    assert len(out) == 1
    s = out[0]
    assert (s["start_m"], s["end_m"]) == (0, 1000)
    assert s["missing"] == 0 and s["qualified"]
    assert {s["device1"], s["device2"]} == {"DEV-A", "DEV-B"}
    assert s["run1"] == "R1" and s["run2"] == "R1"


def test_missing_counts(client):
    # 一台设备 -> 缺 1 台; 无设备 -> 缺 2 台
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,500,100,DEV-A,R1\n500,1000,100,DEV-A,R1\n0,500,100,DEV-B,R1\n")
    upload(client, "repave", "L1", "R1", REPAVE_H + "1500,2000,50\n")  # 只重铺未检测
    out = segs(client, "L1", 0, 2000)
    got = {(s["start_m"], s["end_m"]): s["missing"] for s in out}
    assert got[(0, 500)] == 0
    assert got[(500, 1000)] == 1
    assert got[(1500, 2000)] == 2


def test_same_device_twice_not_enough(client):
    # 同一台设备两个跑次仍只算一台
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,100,DEV-A,R1\n0,100,200,DEV-A,R2\n")
    out = segs(client, "L1", 0, 100)
    assert out[0]["missing"] == 1 and not out[0]["qualified"]


# ------------------------------------------------------------ 重铺规则

def test_repave_invalidates_earlier_runs(client):
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,1000,100,DEV-A,R1\n0,1000,101,DEV-B,R1\n")
    # [300,600) 在时刻 150 重铺完工 -> 此前的跑次在该段无效
    upload(client, "repave", "L1", "RP1", REPAVE_H + "300,600,150\n")
    out = segs(client, "L1", 0, 1000)
    got = {(s["start_m"], s["end_m"]): s for s in out}
    assert got[(0, 300)]["qualified"]
    assert not got[(300, 600)]["qualified"] and got[(300, 600)]["missing"] == 2
    assert got[(300, 600)]["threshold"] == 150
    assert got[(600, 1000)]["qualified"]


def test_only_latest_completion_matters(client):
    # 两次重铺, 只认最近一次完工时刻
    upload(client, "repave", "L1", "RP1", REPAVE_H + "0,100,100\n")
    upload(client, "repave", "L1", "RP2", REPAVE_H + "0,100,200\n")
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,150,DEV-A,R1\n"   # 150 在 200 之前 -> 无效
           "0,100,250,DEV-B,R1\n"               # 250 > 200 -> 有效
           "0,100,260,DEV-C,R1\n")
    out = segs(client, "L1", 0, 100)
    assert out[0]["threshold"] == 200
    assert out[0]["qualified"]
    assert {out[0]["device1"], out[0]["device2"]} == {"DEV-B", "DEV-C"}


def test_measured_equal_to_completion_is_invalid(client):
    # “完工后”为严格大于
    upload(client, "repave", "L1", "RP1", REPAVE_H + "0,100,100\n")
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,100,DEV-A,R1\n0,100,101,DEV-B,R1\n0,100,101,DEV-C,R1\n")
    out = segs(client, "L1", 0, 100)
    assert out[0]["qualified"]
    assert {out[0]["device1"], out[0]["device2"]} == {"DEV-B", "DEV-C"}


# ------------------------------------------------------------ 批次与整批更正

def test_correction_replaces_whole_batch(client):
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,1000,100,DEV-A,R1\n0,1000,100,DEV-B,R1\n")
    upload(client, "inspect", "L1", "B2",
           INSPECT_H + "2000,3000,100,DEV-C,R1\n2000,3000,100,DEV-D,R1\n")
    # 整批更正 B1: 覆盖范围移到 [500,800), 设备换为 DEV-E/DEV-F
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "500,800,100,DEV-E,R9\n500,800,100,DEV-F,R9\n")
    assert segs(client, "L1", 0, 400) == []            # 旧批覆盖消失
    out = segs(client, "L1", 500, 800)
    assert out[0]["qualified"]
    assert {out[0]["device1"], out[0]["device2"]} == {"DEV-E", "DEV-F"}
    assert out[0]["run1"] == "R9"
    out2 = segs(client, "L1", 2000, 3000)              # 未涉及的批不受影响
    assert out2[0]["qualified"]
    # 批次表行数已更新
    batches = {b["batch_no"]: b["row_count"] for b in client.get("/api/batches").json()["batches"]}
    assert batches["B1"] == 2 and batches["B2"] == 2


def test_correction_only_recomputes_affected_ranges(client, tmp_path):
    # 更正只重算旧批和新批涉及的米段: 未涉及区段的 acceptance 行保持原 rowid
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,1000,100,DEV-A,R1\n0,1000,100,DEV-B,R1\n")
    upload(client, "inspect", "L1", "B2",
           INSPECT_H + "5000,6000,100,DEV-C,R1\n5000,6000,100,DEV-D,R1\n")
    import sqlite3, os
    db = os.environ["DB_PATH"]
    def rowids():
        c = sqlite3.connect(db)
        got = c.execute(
            "SELECT rowid,start_m,end_m FROM acceptance WHERE lane='L1' ORDER BY start_m"
        ).fetchall()
        c.close()
        return got
    before = rowids()
    # 更正 B2: 范围仍在 [5000,6000) 内 -> [0,1000) 的结果行不应被触碰
    upload(client, "inspect", "L1", "B2",
           INSPECT_H + "5200,5800,100,DEV-C,R2\n5200,5800,100,DEV-D,R2\n")
    after = rowids()
    before_first = [r for r in before if r[1] == 0 and r[2] == 1000]
    after_first = [r for r in after if r[1] == 0 and r[2] == 1000]
    assert before_first and before_first == after_first  # rowid 未变 -> 未重算
    out = segs(client, "L1", 5000, 6000)
    assert all(s["run1"] == "R2" for s in out if s["qualified"])


def test_failed_upload_keeps_previous_commit(client):
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,1000,100,DEV-A,R1\n0,1000,100,DEV-B,R1\n")
    # 坏 CSV 更正 B1: 原始批次与交验结果都应沿用上次提交
    loc = upload(client, "inspect", "L1", "B1",
                 INSPECT_H + "0,1000,100,DEV-A,R1\nBAD,ROW,xx,,\n")
    assert "ok=0" in loc
    out = segs(client, "L1", 0, 1000)
    assert len(out) == 1 and out[0]["qualified"]
    batches = {b["batch_no"]: b["row_count"] for b in client.get("/api/batches").json()["batches"]}
    assert batches["B1"] == 2


def test_invalid_csv_rejected(client):
    for bad in [
        "start_m,end_m,measured_at,device_id,run_id\n100,50,1,D,R\n",   # end<=start
        "start_m,end_m,measured_at,device_id,run_id\n0,100,-1,D,R\n",   # 负时刻
        "start_m,end_m,measured_at,device_id,run_id\n0,100,1,,R\n",     # 空设备号
        "start_m,end_m\n0,100\n",                                       # 缺列
        "start_m,end_m,measured_at,device_id,run_id\n",                 # 无数据行
    ]:
        loc = upload(client, "inspect", "L1", "BX", bad)
        assert "ok=0" in loc


# ------------------------------------------------------------ 持久化

def test_results_survive_restart(tmp_path, monkeypatch):
    db = str(tmp_path / "persist.db")
    monkeypatch.setenv("DB_PATH", db)
    from app.main import app
    with TestClient(app) as c:
        upload(c, "inspect", "L1", "B1",
               INSPECT_H + "0,100,100,DEV-A,R1\n0,100,100,DEV-B,R1\n")
        assert segs(c, "L1", 0, 100)[0]["qualified"]
    # 模拟重启: 重新创建应用实例, 直接读已提交结果
    with TestClient(app) as c2:
        out = segs(c2, "L1", 0, 100)
        assert len(out) == 1 and out[0]["qualified"]


# ------------------------------------------------------------ 页面

def test_index_page_renders(client):
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,100,DEV-A,R1\n0,100,100,DEV-B,R1\n")
    r = client.get("/", params={"lane": "L1", "start_m": 0, "end_m": 100})
    assert r.status_code == 200
    assert "合格" in r.text and "DEV-A" in r.text


# ------------------------------------------------------------ 扫描算法单元测试

def test_sweep_no_per_meter_expansion():
    # 大区间的段数只取决于端点数量, 与里程长度无关
    repaves = [(0, 10**9, 50)]
    inspects = [(0, 10**9, 100, "A", "R1"), (0, 10**9, 101, "B", "R1")]
    out = sweep_segments(repaves, inspects, 0, 10**9)
    assert out == [(0, 10**9, 50, frozenset({("A", "R1"), ("B", "R1")}))]


def test_sweep_merges_adjacent_identical():
    inspects = [(0, 500, 100, "A", "R1"), (500, 1000, 100, "A", "R1"),
                (0, 1000, 100, "B", "R1")]
    out = sweep_segments([], inspects, 0, 1000)
    assert len(out) == 1 and out[0][0] == 0 and out[0][1] == 1000


def test_merge_ranges():
    assert merge_ranges([(5, 8), (0, 3), (3, 5), (20, 25)]) == [(0, 8), (20, 25)]
    assert merge_ranges([]) == []
