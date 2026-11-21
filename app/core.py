"""核心业务逻辑。

- CSV 解析与校验(整批校验, 任何一行错误则整批拒绝)
- 区间扫描(sweep line)计算交验结果, 只按区间端点切分, 不逐米展开
- 批次应用: 原始批次与交验结果在同一事务中生效; 更正只重算旧批和新批涉及的米段
- 平整度(IRI)复核: 上传时检查组内带测值区间冲突; 两跑次比对求最小共用区间

交验规则:
  每处(每个原子区间)需要 NEEDED_DEVICES 台不同设备的有效跑次覆盖才算合格;
  已重铺的位置, 只认最近一次完工时刻(threshold)之后检测的跑次。

测值(iri)规则:
  检测 CSV 可带可选列 iri(0.001 m/km 为单位的非负整数); 未填的记录照常用于交验,
  只是不参与冲突检查与跑次比对。同车道、设备、跑次内, 起止与测值都相同的记录
  视为同一份(去重); 除此之外同组带测值区间相交即冲突, 上传时整批拒绝。
"""
from __future__ import annotations

import csv
import heapq
import io
from collections import defaultdict

NEEDED_DEVICES = 2  # 每处合格所需的不同设备台数

REPAVE_COLUMNS = ["start_m", "end_m", "completed_at"]
INSPECT_COLUMNS = ["start_m", "end_m", "measured_at", "device_id", "run_id"]
INSPECT_OPTIONAL_COLUMNS = ["iri"]  # 可选测值列: 0.001 m/km 为单位的非负整数

MAX_ROWS_PER_UPLOAD = 1_000_000
MAX_FIELD_LEN = 64

ACC_COLUMNS = (
    "lane,start_m,end_m,threshold,device_count,missing,device1,run1,device2,run2"
)
_ACC_ATTRS = ("threshold", "device_count", "missing", "device1", "run1", "device2", "run2")


class CsvError(ValueError):
    """CSV 内容不合法, 整批拒绝。"""


# ---------------------------------------------------------------- CSV 解析

def _to_int(raw, name, lineno, errors):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        errors.append(f"第{lineno}行: {name} 不是整数: {raw!r}")
        return None


def _to_text(raw, name, lineno, errors):
    s = (raw or "").strip()
    if not s:
        errors.append(f"第{lineno}行: {name} 不能为空")
        return None
    if len(s) > MAX_FIELD_LEN:
        errors.append(f"第{lineno}行: {name} 超过 {MAX_FIELD_LEN} 个字符")
        return None
    return s


def _check_span(s, e, lineno, errors):
    if s is None or e is None:
        return False
    if s < 0 or e <= s:
        errors.append(f"第{lineno}行: 需满足 0 <= start_m < end_m, 实际为 [{s}, {e})")
        return False
    return True


def _parse_csv(text, columns, build_row, optional=()):
    reader = csv.reader(io.StringIO(text))
    rows, errors = [], []
    idx = None
    for lineno, rec in enumerate(reader, start=1):
        if not rec or all(not c.strip() for c in rec):
            continue  # 跳过空行
        if idx is None:
            header = [c.strip() for c in rec]
            missing = [c for c in columns if c not in header]
            if missing:
                raise CsvError(f"CSV 缺少列 {missing}; 需要的列: {columns}")
            idx = {c: header.index(c) for c in columns}
            for c in optional:  # 可选列: 表头里有才解析
                if c in header:
                    idx[c] = header.index(c)
            continue
        if len(rows) >= MAX_ROWS_PER_UPLOAD:
            errors.append(f"超过单次上传最大行数 {MAX_ROWS_PER_UPLOAD}")
            break
        row = build_row(rec, idx, lineno, errors)
        if row is not None:
            rows.append(row)
        if len(errors) >= 20:
            errors.append("错误过多, 仅显示前 20 条")
            break
    if idx is None:
        raise CsvError("CSV 为空或缺少表头")
    if errors:
        raise CsvError("; ".join(errors))
    return rows


def _repave_row(rec, idx, lineno, errors):
    get = lambda c: rec[idx[c]] if idx[c] < len(rec) else ""
    s = _to_int(get("start_m"), "start_m", lineno, errors)
    e = _to_int(get("end_m"), "end_m", lineno, errors)
    t = _to_int(get("completed_at"), "completed_at", lineno, errors)
    if t is not None and t < 0:
        errors.append(f"第{lineno}行: completed_at 需 >= 0")
        t = None
    if not _check_span(s, e, lineno, errors) or t is None:
        return None
    return (s, e, t)


def _inspect_row(rec, idx, lineno, errors):
    get = lambda c: rec[idx[c]] if idx[c] < len(rec) else ""
    s = _to_int(get("start_m"), "start_m", lineno, errors)
    e = _to_int(get("end_m"), "end_m", lineno, errors)
    t = _to_int(get("measured_at"), "measured_at", lineno, errors)
    d = _to_text(get("device_id"), "device_id", lineno, errors)
    r = _to_text(get("run_id"), "run_id", lineno, errors)
    if t is not None and t < 0:
        errors.append(f"第{lineno}行: measured_at 需 >= 0")
        t = None
    iri = None
    iri_ok = True
    if "iri" in idx:  # 可选列: 留空表示无测值
        raw = str(get("iri")).strip()
        if raw:
            iri = _to_int(raw, "iri", lineno, errors)
            if iri is not None and iri < 0:
                errors.append(f"第{lineno}行: iri 需为非负整数(0.001 m/km)")
                iri = None
            if iri is None:
                iri_ok = False
    if not _check_span(s, e, lineno, errors) or None in (t, d, r) or not iri_ok:
        return None
    return (s, e, t, d, r, iri)


def parse_repave_csv(text):
    """返回 [(start_m, end_m, completed_at), ...]"""
    return _parse_csv(text, REPAVE_COLUMNS, _repave_row)


def parse_inspect_csv(text):
    """返回 [(start_m, end_m, measured_at, device_id, run_id, iri), ...]

    iri 为可选列: 表头无此列或单元格留空时为 None(无测值, 照常用于交验)。
    """
    return _parse_csv(text, INSPECT_COLUMNS, _inspect_row, INSPECT_OPTIONAL_COLUMNS)


# ---------------------------------------------------------------- 区间扫描

def sweep_segments(repaves, inspects, lo, hi):
    """在 [lo, hi) 上扫描计算交验状态, 不逐米展开。

    repaves:  可迭代的 (start, end, completed_at)
    inspects: 可迭代的 (start, end, measured_at, device_id, run_id)
    返回合并后的段列表 [(start, end, threshold, frozenset{(device, run)})],
    其中只保留 measured_at > threshold 的有效 (设备, 跑次)。
    """
    # 每个端点位置: (重铺进入, 重铺离开, 检测进入, 检测离开)
    events = defaultdict(lambda: ([], [], [], []))
    for s, e, t in repaves:
        s = lo if s < lo else s
        e = hi if e > hi else e
        if s < e:
            events[s][0].append(t)
            events[e][1].append(t)
    for s, e, t, d, r in inspects:
        s = lo if s < lo else s
        e = hi if e > hi else e
        if s < e:
            events[s][2].append((d, r, t))
            events[e][3].append((d, r, t))
    events.setdefault(lo)
    events.setdefault(hi)

    rep_active = {}   # completed_at -> 覆盖次数
    rep_heap = []     # 最大堆(存负值), 懒删除
    insp_active = {}  # (device, run, measured_at) -> 覆盖次数

    def bump(cnt, key, delta):
        v = cnt.get(key, 0) + delta
        if v:
            cnt[key] = v
        else:
            cnt.pop(key, None)

    atomics = []
    prev = None
    for pos in sorted(events):
        if prev is not None and pos > prev:
            thr = 0
            while rep_heap:
                top = -rep_heap[0]
                if rep_active.get(top, 0) > 0:
                    thr = top
                    break
                heapq.heappop(rep_heap)
            pairs = frozenset(
                (d, r) for (d, r, t) in insp_active if t > thr
            )
            atomics.append((prev, pos, thr, pairs))
        r_add, r_del, i_add, i_del = events[pos]
        for t in r_del:
            bump(rep_active, t, -1)
        for t in r_add:
            bump(rep_active, t, 1)
            heapq.heappush(rep_heap, -t)
        for k in i_del:
            bump(insp_active, k, -1)
        for k in i_add:
            bump(insp_active, k, 1)
        prev = pos

    # 相邻且状态相同的原子段合并; 合并后段内每个有效 (设备, 跑次) 都覆盖整段
    merged = []
    for s, e, thr, pairs in atomics:
        if merged and merged[-1][1] == s and merged[-1][2] == thr and merged[-1][3] == pairs:
            merged[-1] = (merged[-1][0], e, thr, pairs)
        else:
            merged.append((s, e, thr, pairs))
    return merged


def segment_rows(lane, merged):
    """把扫描结果转成 acceptance 表行。"""
    out = []
    for s, e, thr, pairs in merged:
        devs = sorted({d for d, _ in pairs})
        shown = []
        for d in devs[:NEEDED_DEVICES]:
            run = min(r for dd, r in pairs if dd == d)
            shown.append((d, run))
        d1, r1 = shown[0] if len(shown) > 0 else (None, None)
        d2, r2 = shown[1] if len(shown) > 1 else (None, None)
        count = len(devs)
        missing = max(0, NEEDED_DEVICES - count)
        out.append((lane, s, e, thr, count, missing, d1, r1, d2, r2))
    return out


# ---------------------------------------------------------------- 交验结果落库

def _insert_acc(cur, row):
    cur.execute(f"INSERT INTO acceptance({ACC_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)", row)


def merge_ranges(ranges):
    """合并重叠或相接的区间, 得到互不相交的重算组件。"""
    out = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if s < e]


def _merge_boundary(cur, lane, pos):
    """重算窗口边界处, 若左右两段属性完全相同则合并, 避免产生碎段。"""
    left = cur.execute(
        "SELECT rowid,* FROM acceptance WHERE lane=? AND end_m=?", (lane, pos)
    ).fetchall()
    right = cur.execute(
        "SELECT rowid,* FROM acceptance WHERE lane=? AND start_m=?", (lane, pos)
    ).fetchall()
    if len(left) != 1 or len(right) != 1:
        return
    L, R = left[0], right[0]
    if all(L[k] == R[k] for k in _ACC_ATTRS):
        cur.execute(
            "DELETE FROM acceptance WHERE rowid IN (?,?)", (L["rowid"], R["rowid"])
        )
        _insert_acc(
            cur, (lane, L["start_m"], R["end_m"]) + tuple(L[k] for k in _ACC_ATTRS)
        )


def _clip_acceptance(cur, lane, a, b):
    """删除/裁剪与 [a, b) 相交的旧结果行, 窗口外部分保留。"""
    for row in cur.execute(
        "SELECT rowid,* FROM acceptance WHERE lane=? AND start_m<? AND end_m>?",
        (lane, b, a),
    ).fetchall():
        cur.execute("DELETE FROM acceptance WHERE rowid=?", (row["rowid"],))
        attrs = tuple(row[k] for k in _ACC_ATTRS)
        if row["start_m"] < a:
            _insert_acc(cur, (lane, row["start_m"], a) + attrs)
        if row["end_m"] > b:
            _insert_acc(cur, (lane, b, row["end_m"]) + attrs)


def _assign(components, rows):
    """把区间行分配给各有序组件(每组件命中的行 = 与该组件相交的行)。

    行与组件均按起点有序, 单趟扫描完成, 避免每个组件各做一次范围查询。
    """
    rows = sorted(rows, key=lambda r: r[0])
    out, active = [], []
    ri = 0
    for a, b in components:
        while ri < len(rows) and rows[ri][0] < b:
            active.append(rows[ri])
            ri += 1
        active = [r for r in active if r[1] > a]
        out.append(list(active))
    return out


def recompute_components(conn, lane, components):
    """重算一组互不相交、按起点升序的米段组件。

    交验状态是逐点函数, 只取决于覆盖该点的记录, 因此组件外的既有结果
    不受影响, 直接保留; 结果只生成在组件内有数据(重铺或检测)的连通米段上,
    段内无检测覆盖的空洞体现为"缺 2 台"的段, 完全无数据的区域不产生结果行。
    """
    if not components:
        return
    lo, hi = components[0][0], components[-1][1]
    repaves = [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT start_m,end_m,completed_at FROM repave_segments "
            "WHERE lane=? AND start_m<? AND end_m>?",
            (lane, hi, lo),
        )
    ]
    inspects = [
        (r[0], r[1], r[2], r[3], r[4])
        for r in conn.execute(
            "SELECT start_m,end_m,measured_at,device_id,run_id FROM inspect_segments "
            "WHERE lane=? AND start_m<? AND end_m>?",
            (lane, hi, lo),
        )
    ]
    rep_sets = _assign(components, repaves)
    insp_sets = _assign(components, inspects)
    cur = conn.cursor()
    for (a, b), rep, insp in zip(components, rep_sets, insp_sets):
        _clip_acceptance(cur, lane, a, b)
        # 组件内数据的连通米段(裁剪到组件内), 逐段扫描并写入新结果
        hulls = merge_ranges(
            [(max(s, a), min(e, b)) for s, e, *_ in rep]
            + [(max(s, a), min(e, b)) for s, e, *_ in insp]
        )
        for hlo, hhi in hulls:
            for row in segment_rows(lane, sweep_segments(rep, insp, hlo, hhi)):
                _insert_acc(cur, row)
            # 与组件外相同属性的旧段合并, 避免碎段
            _merge_boundary(cur, lane, hlo)
            _merge_boundary(cur, lane, hhi)


# ---------------------------------------------------------------- 测值冲突检查

def check_iri_conflicts(conn, lane, groups):
    """检查 (lane, device_id, run_id) 各组内带测值(iri)区间是否相交。

    按本批生效后的数据检查(即含本次写入的全部批次记录)。同组内起止与测值
    都相同的记录视为同一份(去重, 可能由不同批次提供); 除此之外, 两条带测值
    区间相交即冲突, 抛出 CsvError(报出跑次与冲突里程), 由调用方回滚整批。
    """
    for dev, run in sorted(groups):
        rows = conn.execute(
            "SELECT start_m,end_m,iri FROM inspect_segments "
            "WHERE lane=? AND device_id=? AND run_id=? AND iri IS NOT NULL",
            (lane, dev, run),
        ).fetchall()
        segs = sorted({(r[0], r[1], r[2]) for r in rows})  # 去重后按起点排序
        for (s1, e1, _), (s2, e2, _) in zip(segs, segs[1:]):
            if s2 < e1:  # 左闭右开区间相交(相邻不算)
                raise CsvError(
                    f"设备 {dev} 跑次 {run} 的带测值区间相交: "
                    f"[{s1}, {e1}) 与 [{s2}, {e2}), "
                    f"冲突里程 [{s2}, {min(e1, e2)})"
                )


def apply_batch(conn, lane, kind, batch_no, rows, uploaded_at):
    """应用一个批次(录入或整批更正)。调用方负责事务(BEGIN/COMMIT/ROLLBACK)。

    同一 (lane, kind, batch_no) 已存在时为整批更正: 旧行全部删除后写入新行,
    并只重算旧批和新批涉及的米段。
    """
    cur = conn.cursor()
    table = "repave_segments" if kind == "repave" else "inspect_segments"
    old = cur.execute(
        f"SELECT start_m,end_m FROM {table} WHERE lane=? AND batch_no=?",
        (lane, batch_no),
    ).fetchall()
    cur.execute(f"DELETE FROM {table} WHERE lane=? AND batch_no=?", (lane, batch_no))
    if kind == "repave":
        cur.executemany(
            "INSERT INTO repave_segments(lane,start_m,end_m,completed_at,batch_no) "
            "VALUES (?,?,?,?,?)",
            [(lane, s, e, t, batch_no) for s, e, t in rows],
        )
    else:
        cur.executemany(
            "INSERT INTO inspect_segments"
            "(lane,start_m,end_m,measured_at,device_id,run_id,iri,batch_no) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(lane, s, e, t, d, r, iri, batch_no) for s, e, t, d, r, iri in rows],
        )
        # 本批生效后, 检查其涉及的同组(车道+设备+跑次)带测值区间是否冲突;
        # 冲突则抛错, 整批回滚, 原数据与交验结果照旧
        groups = {(d, r) for _, _, _, d, r, iri in rows if iri is not None}
        if groups:
            check_iri_conflicts(conn, lane, groups)
    cur.execute(
        "INSERT INTO batches(lane,kind,batch_no,uploaded_at,row_count) VALUES (?,?,?,?,?) "
        "ON CONFLICT(lane,kind,batch_no) DO UPDATE SET "
        "uploaded_at=excluded.uploaded_at, row_count=excluded.row_count",
        (lane, kind, batch_no, uploaded_at, len(rows)),
    )
    affected = [(r[0], r[1]) for r in old] + [(row[0], row[1]) for row in rows]
    recompute_components(conn, lane, merge_ranges(affected))


# ---------------------------------------------------------------- 跑次比对(IRI 复核)

def load_iri_segments(conn, lane, device_id, run_id):
    """取某跑次全部带测值区间(跨批次), 去重(起止与测值相同只算一份)后排序。"""
    rows = conn.execute(
        "SELECT start_m,end_m,iri FROM inspect_segments "
        "WHERE lane=? AND device_id=? AND run_id=? AND iri IS NOT NULL",
        (lane, device_id, run_id),
    ).fetchall()
    return sorted({(r[0], r[1], r[2]) for r in rows})


def _tiles(segs):
    """同一跑次的去重区间(已排序, 互不重叠)是否连续无空洞地拼接。"""
    return all(segs[k][1] == segs[k + 1][0] for k in range(len(segs) - 1))


def compare_run_segments(segs_a, segs_b):
    """比对两个跑次的带测值区间(各自已去重、排序、互不重叠)。

    最小共用区间: 两边都能用完整原段连续拼成、且无法按双方分段边界再拆小的
    区间 —— 即两边区间相交关系(有正长度交集才算相交)的连通分量中, 双方各自
    的原段恰好连续铺满同一里程段的分量。各自均值为原段 iri 按原段长度加权。

    返回 (common, only_a, only_b):
      common: [(start, end, mean_a, mean_b)]  最小共用区间及双方加权均值
      only_a / only_b: [(start, end)]  该跑次有测值却未配对的里程(相邻已合并)
    """
    items = ([(s, e, v, 0) for s, e, v in segs_a]
             + [(s, e, v, 1) for s, e, v in segs_b])
    items.sort(key=lambda x: (x[0], x[1]))
    # 按相交关系聚连通分量: 起点落在当前分量内部(严格小于分量右端)才归入
    clusters, cur_a, cur_b, cur_end = [], [], [], None
    for s, e, v, side in items:
        if cur_end is None or s >= cur_end:
            if cur_a or cur_b:
                clusters.append((cur_a, cur_b))
            cur_a, cur_b, cur_end = [], [], e
        elif e > cur_end:
            cur_end = e
        (cur_a if side == 0 else cur_b).append((s, e, v))
    if cur_a or cur_b:
        clusters.append((cur_a, cur_b))

    common, only_a, only_b = [], [], []
    for a_segs, b_segs in clusters:
        if (a_segs and b_segs and _tiles(a_segs) and _tiles(b_segs)
                and (a_segs[0][0], a_segs[-1][1]) == (b_segs[0][0], b_segs[-1][1])):
            s, e = a_segs[0][0], a_segs[-1][1]
            mean_a = sum(v * (ee - ss) for ss, ee, v in a_segs) / (e - s)
            mean_b = sum(v * (ee - ss) for ss, ee, v in b_segs) / (e - s)
            common.append((s, e, mean_a, mean_b))
        else:  # 无法两边都用完整原段拼成 -> 各自里程记为未配对
            only_a.extend((s, e) for s, e, _ in a_segs)
            only_b.extend((s, e) for s, e, _ in b_segs)
    return common, merge_ranges(only_a), merge_ranges(only_b)


def compare_runs(conn, lane, dev_a, run_a, dev_b, run_b, limit=5000):
    """比对同车道两台不同设备各自的跑次: 最小共用区间 + 各自未配对里程。

    均值与差值(前者减后者)的单位均为 0.001 m/km, 与原始 iri 列一致。
    """
    if dev_a == dev_b:
        raise CsvError("需选择两台不同设备的跑次")
    segs_a = load_iri_segments(conn, lane, dev_a, run_a)
    segs_b = load_iri_segments(conn, lane, dev_b, run_b)
    common, only_a, only_b = compare_run_segments(segs_a, segs_b)
    truncated = len(common) > limit or len(only_a) > limit or len(only_b) > limit
    return {
        "lane": lane, "dev_a": dev_a, "run_a": run_a,
        "dev_b": dev_b, "run_b": run_b,
        "unit": "0.001 m/km",
        "has_value_a": bool(segs_a),
        "has_value_b": bool(segs_b),
        "common": [
            {"start_m": s, "end_m": e, "iri_a": ma, "iri_b": mb, "diff": ma - mb}
            for s, e, ma, mb in common[:limit]
        ],
        "unpaired_a": [{"start_m": s, "end_m": e} for s, e in only_a[:limit]],
        "unpaired_b": [{"start_m": s, "end_m": e} for s, e in only_b[:limit]],
        "truncated": truncated,
    }


def list_iri_runs(conn, lane):
    """列出某车道下有带测值(iri)记录的设备及其跑次, 供比对页选择。"""
    rows = conn.execute(
        "SELECT DISTINCT device_id, run_id FROM inspect_segments "
        "WHERE lane=? AND iri IS NOT NULL ORDER BY device_id, run_id",
        (lane,),
    ).fetchall()
    devices = {}
    for d, r in rows:
        devices.setdefault(d, []).append(r)
    return [{"device_id": d, "runs": rs} for d, rs in sorted(devices.items())]


# ---------------------------------------------------------------- 查询

def query_acceptance(conn, lane, a, b, limit=5000):
    """读取已提交的交验结果(不做任何重算), 裁剪到 [a, b) 展示。"""
    rows = conn.execute(
        "SELECT * FROM acceptance WHERE lane=? AND start_m<? AND end_m>? "
        "ORDER BY start_m LIMIT ?",
        (lane, b, a, limit + 1),
    ).fetchall()
    truncated = len(rows) > limit
    out = []
    for r in rows[:limit]:
        d = dict(r)
        d["start_m"] = max(d["start_m"], a)
        d["end_m"] = min(d["end_m"], b)
        d["qualified"] = d["missing"] == 0
        out.append(d)
    return out, truncated
