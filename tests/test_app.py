"""端到端与单元测试。

运行: pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.core import compare_run_segments, merge_ranges, sweep_segments


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

def test_old_database_migrates(tmp_path, monkeypatch):
    # 旧版库(inspect_segments 无 iri 列)打开时自动补列, 旧记录照常用于交验
    import sqlite3
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE batches (lane TEXT NOT NULL, kind TEXT NOT NULL, batch_no TEXT NOT NULL,"
        " uploaded_at INTEGER NOT NULL, row_count INTEGER NOT NULL,"
        " PRIMARY KEY (lane, kind, batch_no));"
        "CREATE TABLE repave_segments (lane TEXT NOT NULL, start_m INTEGER NOT NULL,"
        " end_m INTEGER NOT NULL, completed_at INTEGER NOT NULL, batch_no TEXT NOT NULL);"
        "CREATE TABLE inspect_segments (lane TEXT NOT NULL, start_m INTEGER NOT NULL,"
        " end_m INTEGER NOT NULL, measured_at INTEGER NOT NULL, device_id TEXT NOT NULL,"
        " run_id TEXT NOT NULL, batch_no TEXT NOT NULL);"
        "CREATE TABLE acceptance (lane TEXT NOT NULL, start_m INTEGER NOT NULL,"
        " end_m INTEGER NOT NULL, threshold INTEGER NOT NULL, device_count INTEGER NOT NULL,"
        " missing INTEGER NOT NULL, device1 TEXT, run1 TEXT, device2 TEXT, run2 TEXT);"
        "INSERT INTO inspect_segments VALUES ('L1',0,100,100,'DEV-A','R1','B1');"
        "INSERT INTO batches VALUES ('L1','inspect','B1',100,1);"
    )
    c.commit()
    c.close()
    monkeypatch.setenv("DB_PATH", db)
    from app.main import app
    with TestClient(app) as client:
        # 旧记录(无 iri)与新记录(带 iri)一起用于交验
        upload(client, "inspect", "L1", "B2", IRI_H + "0,100,101,DEV-B,R1,1500\n")
        out = segs(client, "L1", 0, 100)
        assert out[0]["qualified"]
        cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
        assert not cmp["has_value_a"] and cmp["has_value_b"]
        assert cmp["common"] == []  # DEV-A/R1 无测值, 无共用区间


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


# ------------------------------------------------------------ iri 可选列

IRI_H = "start_m,end_m,measured_at,device_id,run_id,iri\n"


def compare(client, lane, da, ra, db, rb):
    r = client.get("/api/iri_compare", params={
        "lane": lane, "dev_a": da, "run_a": ra, "dev_b": db, "run_b": rb})
    assert r.status_code == 200
    return r.json()


def test_iri_column_optional(client):
    # 无 iri 列的旧格式照常上传并用于交验
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,100,DEV-A,R1\n0,100,100,DEV-B,R1\n")
    # 带 iri 列, 部分行留空: 照常计入交验
    upload(client, "inspect", "L1", "B2",
           IRI_H + "200,300,100,DEV-A,R2,1500\n200,300,100,DEV-B,R2,\n")
    out = segs(client, "L1", 0, 400)
    got = {(s["start_m"], s["end_m"]): s["missing"] for s in out}
    assert got[(0, 100)] == 0 and got[(200, 300)] == 0


def test_iri_validation(client):
    # 负数 / 非整数的 iri 整批拒绝; 0 合法
    loc = upload(client, "inspect", "L1", "B1", IRI_H + "0,100,100,DEV-A,R1,-1\n")
    assert "ok=0" in loc
    loc = upload(client, "inspect", "L1", "B1", IRI_H + "0,100,100,DEV-A,R1,1.5\n")
    assert "ok=0" in loc
    loc = upload(client, "inspect", "L1", "B1", IRI_H + "0,100,100,DEV-A,R1,0\n")
    assert "ok=1" in loc
    assert client.get("/api/batches").json()["batches"][0]["row_count"] == 1


# ------------------------------------------------------------ 带测值区间冲突

def test_iri_conflict_within_batch_rejected(client):
    # 同车道+设备+跑次内, 带测值区间相交 -> 整批拒绝
    loc = upload(client, "inspect", "L1", "B1",
                 IRI_H + "0,300,100,DEV-A,R1,1000\n200,400,100,DEV-A,R1,1100\n")
    assert "ok=0" in loc
    msg = unquote(loc)
    assert "R1" in msg and "[200, 300)" in msg  # 报出跑次和冲突里程
    assert client.get("/api/batches").json()["batches"] == []  # 整批未生效


def test_iri_conflict_across_batches_rejected(client):
    # 按本批生效后的数据检查: 新批与已提交批次的同组区间相交 -> 整批拒绝
    upload(client, "inspect", "L1", "B1", IRI_H + "0,300,100,DEV-A,R1,1000\n")
    loc = upload(client, "inspect", "L1", "B2", IRI_H + "200,500,100,DEV-A,R1,1100\n")
    assert "ok=0" in loc
    assert "[200, 300)" in unquote(loc)
    # 原数据照旧: B1 仍在, B2 未生效
    batches = {b["batch_no"] for b in client.get("/api/batches").json()["batches"]}
    assert batches == {"B1"}
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "RX")
    assert cmp["has_value_a"] and not cmp["has_value_b"]


def test_iri_conflict_only_same_group(client):
    # 不同跑次或不同设备的区间相交不算冲突
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,300,100,DEV-A,R1,1000\n0,300,100,DEV-A,R2,1000\n"
           "0,300,100,DEV-B,R1,1000\n")
    batches = client.get("/api/batches").json()["batches"]
    assert len(batches) == 1 and batches[0]["row_count"] == 3


def test_iri_adjacent_intervals_no_conflict(client):
    # 端点相接(左闭右开不相交)不冲突
    loc = upload(client, "inspect", "L1", "B1",
                 IRI_H + "0,100,100,DEV-A,R1,1000\n100,200,100,DEV-A,R1,1100\n")
    assert "ok=1" in loc


def test_iri_same_span_different_value_conflicts(client):
    # 起止相同但测值不同 -> 不是重复记录, 相交冲突
    loc = upload(client, "inspect", "L1", "B1",
                 IRI_H + "0,100,100,DEV-A,R1,1000\n0,100,101,DEV-A,R1,2000\n")
    assert "ok=0" in loc


def test_iri_identical_records_dedup_across_batches(client):
    # 起止与测值都相同的记录只算一份: 跨批次相同记录不冲突;
    # 更正其中一批后, 另一批提供的相同测值继续有效
    upload(client, "inspect", "L1", "B1", IRI_H + "0,300,100,DEV-A,R1,1500\n")
    loc = upload(client, "inspect", "L1", "B2", IRI_H + "0,300,200,DEV-A,R1,1500\n")
    assert "ok=1" in loc  # measured_at 不同但起止+测值相同 -> 仍视为同一份
    upload(client, "inspect", "L1", "B3", IRI_H + "0,300,100,DEV-B,R1,1400\n")
    # 更正 B1: 删掉 [0,300) 改录别的 -> B2 的相同测值继续有效
    upload(client, "inspect", "L1", "B1", IRI_H + "500,700,100,DEV-A,R1,1600\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    spans = [(c["start_m"], c["end_m"]) for c in cmp["common"]]
    assert (0, 300) in spans  # [0,300) 测值 1500 仍在(来自 B2)
    c0 = [c for c in cmp["common"] if c["start_m"] == 0][0]
    assert c0["iri_a"] == 1500 and c0["iri_b"] == 1400


def test_iri_correction_can_resolve_conflict(client):
    # 更正掉冲突记录后, 之前被拒的批次可以重新上传
    upload(client, "inspect", "L1", "B1", IRI_H + "0,300,100,DEV-A,R1,1000\n")
    loc = upload(client, "inspect", "L1", "B2", IRI_H + "200,500,100,DEV-A,R1,1100\n")
    assert "ok=0" in loc
    upload(client, "inspect", "L1", "B1", IRI_H + "0,150,100,DEV-A,R1,1000\n")  # 更正 B1
    loc = upload(client, "inspect", "L1", "B2", IRI_H + "200,500,100,DEV-A,R1,1100\n")
    assert "ok=1" in loc


def test_failed_conflict_keeps_acceptance(client):
    # 冲突批次整批拒绝后, 原数据与交验结果照旧
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,1000,100,DEV-A,R1,1000\n0,1000,100,DEV-B,R1,1200\n")
    before = segs(client, "L1", 0, 1000)
    loc = upload(client, "inspect", "L1", "B2",
                 IRI_H + "0,500,100,DEV-A,R1,1300\n400,900,100,DEV-A,R1,1400\n")
    assert "ok=0" in loc
    assert segs(client, "L1", 0, 1000) == before
    assert client.get("/api/batches").json()["batches"][0]["batch_no"] == "B1"


def test_no_iri_records_skip_conflict(client):
    # 不带测值的区间即使相交也不冲突, 照常用于交验
    loc = upload(client, "inspect", "L1", "B1",
                 INSPECT_H + "0,300,100,DEV-A,R1\n200,400,100,DEV-A,R1\n"
                 "0,300,100,DEV-B,R1\n200,400,100,DEV-B,R1\n")
    assert "ok=1" in loc
    out = segs(client, "L1", 0, 400)
    assert all(s["qualified"] for s in out)


# ------------------------------------------------------------ 跑次比对

def test_compare_minimal_common_intervals(client):
    # 分段不同: 找出两边都能用完整原段连续拼成的最小共用区间
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,100,100,DEV-A,R1,1000\n100,200,100,DEV-A,R1,1200\n"
           "200,300,100,DEV-A,R1,1400\n")
    upload(client, "inspect", "L1", "B2",
           IRI_H + "0,200,100,DEV-B,R1,1100\n200,300,100,DEV-B,R1,2000\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    assert cmp["unit"] == "0.001 m/km"
    common = [(c["start_m"], c["end_m"], c["iri_a"], c["iri_b"], c["diff"])
              for c in cmp["common"]]
    # [0,200): A 两段按原段长度加权 (1000*100+1200*100)/200 = 1100
    assert common == [
        (0, 200, 1100.0, 1100.0, 0.0),
        (200, 300, 1400.0, 2000.0, -600.0),  # 前者减后者
    ]
    assert cmp["unpaired_a"] == [] and cmp["unpaired_b"] == []


def test_compare_unpaired_mileage(client):
    # 有测值却未配对的里程分跑次列出
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,100,100,DEV-A,R1,1000\n200,300,100,DEV-A,R1,1200\n")
    upload(client, "inspect", "L1", "B2", IRI_H + "0,100,100,DEV-B,R1,1500\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    assert [(c["start_m"], c["end_m"]) for c in cmp["common"]] == [(0, 100)]
    assert cmp["unpaired_a"] == [{"start_m": 200, "end_m": 300}]
    assert cmp["unpaired_b"] == []


def test_compare_no_common_when_not_tileable(client):
    # 区间相交但任一方无法用完整原段拼出公共段 -> 无共用区间, 双方里程都未配对
    upload(client, "inspect", "L1", "B1", IRI_H + "0,300,100,DEV-A,R1,1000\n")
    upload(client, "inspect", "L1", "B2",
           IRI_H + "0,100,100,DEV-B,R1,1100\n200,300,100,DEV-B,R1,1200\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    assert cmp["common"] == []
    assert cmp["unpaired_a"] == [{"start_m": 0, "end_m": 300}]
    assert cmp["unpaired_b"] == [
        {"start_m": 0, "end_m": 100}, {"start_m": 200, "end_m": 300}]


def test_compare_dedup_counts_once(client):
    # 同组内重复记录(起止+测值相同)在比对时只算一份
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,100,100,DEV-A,R1,1000\n0,100,100,DEV-A,R1,1000\n")
    upload(client, "inspect", "L1", "B2", IRI_H + "0,100,100,DEV-B,R1,2000\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    assert len(cmp["common"]) == 1
    assert cmp["common"][0]["iri_a"] == 1000  # 加权均值不受重复份数影响


def test_compare_same_device_rejected(client):
    upload(client, "inspect", "L1", "B1", IRI_H + "0,100,100,DEV-A,R1,1000\n")
    r = client.get("/api/iri_compare", params={
        "lane": "L1", "dev_a": "DEV-A", "run_a": "R1",
        "dev_b": "DEV-A", "run_b": "R1"})
    assert r.status_code == 400


def test_compare_ignores_records_without_iri(client):
    # 未填 iri 的记录不参与比对
    upload(client, "inspect", "L1", "B1",
           INSPECT_H + "0,100,100,DEV-A,R1\n0,100,100,DEV-B,R1\n")
    cmp = compare(client, "L1", "DEV-A", "R1", "DEV-B", "R1")
    assert not cmp["has_value_a"] and not cmp["has_value_b"]
    assert cmp["common"] == []


def test_inspect_runs_api(client):
    upload(client, "inspect", "L1", "B1",
           IRI_H + "0,100,100,DEV-A,R1,1000\n0,100,100,DEV-A,R2,1100\n"
           "0,100,100,DEV-B,R1,\n")  # DEV-B/R1 无测值
    r = client.get("/api/inspect_runs", params={"lane": "L1"})
    assert r.status_code == 200
    assert r.json()["devices"] == [
        {"device_id": "DEV-A", "runs": ["R1", "R2"]},
    ]


def test_compare_page_renders(client):
    upload(client, "inspect", "L1", "B1", IRI_H + "0,100,100,DEV-A,R1,1000\n")
    upload(client, "inspect", "L1", "B2", IRI_H + "0,100,100,DEV-B,R1,2000\n")
    r = client.get("/", params={
        "cmp_lane": "L1", "dev_a": "DEV-A", "run_a": "R1",
        "dev_b": "DEV-B", "run_b": "R1"})
    assert r.status_code == 200
    assert "最小共用区间" in r.text
    assert "1.000" in r.text and "2.000" in r.text and "-1.000" in r.text
    # 同设备时页面给出错误提示
    r = client.get("/", params={
        "cmp_lane": "L1", "dev_a": "DEV-A", "run_a": "R1",
        "dev_b": "DEV-A", "run_b": "R1"})
    assert "不同设备" in r.text


# ------------------------------------------------------------ 比对算法单元测试

def test_compare_run_segments_units():
    # 端点相接的同侧分段连续拼接; 加权均值按原段长度
    a = [(0, 100, 1000), (100, 300, 2000)]   # 拼成 [0,300), 均值 5000/3
    b = [(0, 300, 1500)]
    common, only_a, only_b = compare_run_segments(a, b)
    assert len(common) == 1
    s, e, ma, mb = common[0]
    assert (s, e) == (0, 300)
    assert ma == pytest.approx((1000 * 100 + 2000 * 200) / 300)
    assert mb == 1500
    assert only_a == [] and only_b == []

    # 互不相交 -> 全部未配对, 相邻未配对里程合并
    common, only_a, only_b = compare_run_segments(
        [(0, 100, 1), (100, 200, 1)], [(300, 400, 1)])
    assert common == []
    assert only_a == [(0, 200)] and only_b == [(300, 400)]

    # 空跑次
    assert compare_run_segments([], []) == ([], [], [])

