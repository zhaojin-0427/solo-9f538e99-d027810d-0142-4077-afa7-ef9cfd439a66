"""FastAPI 入口: 上传批次、查询交验结果。"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .core import (
    NEEDED_DEVICES,
    CsvError,
    apply_batch,
    compare_runs,
    list_iri_runs,
    parse_inspect_csv,
    parse_repave_csv,
    query_acceptance,
)
from .db import get_conn, init_db

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 单次上传上限
RESULT_LIMIT = 5000                   # 单次查询最多返回段数

UPLOAD_LOCK = threading.Lock()  # 单进程内串行化写入; 多进程由 BEGIN IMMEDIATE 兜底

KIND_NAMES = {"repave": "重铺", "inspect": "检测"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="公路检测交验系统", lifespan=lifespan)
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)


def _fmt_ts(v):
    if not v:
        return "—"
    return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["ts"] = _fmt_ts


def _clean_name(value, field):
    s = (value or "").strip()
    if not s:
        raise CsvError(f"{field}不能为空")
    if len(s) > 64:
        raise CsvError(f"{field}过长(>64字符)")
    return s


def _redirect(msg: str, ok: bool) -> RedirectResponse:
    return RedirectResponse(
        f"/?ok={1 if ok else 0}&msg={quote(msg[:600])}", status_code=303
    )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    lane: Optional[str] = None,
    start_m: Optional[str] = None,
    end_m: Optional[str] = None,
    cmp_lane: Optional[str] = None,
    dev_a: Optional[str] = None,
    run_a: Optional[str] = None,
    dev_b: Optional[str] = None,
    run_b: Optional[str] = None,
    msg: str = "",
    ok: str = "",
):
    conn = get_conn()
    try:
        # 整页(批次列表 + 交验查询 + 跑次比对)在同一个读事务里取数,
        # 保证整页以同一份已提交的数据快照为准(WAL 下读事务看到一致快照)
        conn.execute("BEGIN")
        lanes = [
            r[0]
            for r in conn.execute("SELECT DISTINCT lane FROM batches ORDER BY lane")
        ]
        batches = conn.execute(
            "SELECT * FROM batches ORDER BY lane, kind, batch_no"
        ).fetchall()
        results = None
        truncated = False
        error = None
        if lane is not None or start_m is not None or end_m is not None:
            try:
                if not lane:
                    raise CsvError("请选择车道")
                a = int(str(start_m).strip())
                b = int(str(end_m).strip())
                if a < 0 or b <= a:
                    raise CsvError("里程范围需满足 0 <= 起点 < 终点")
                results, truncated = query_acceptance(conn, lane, a, b, RESULT_LIMIT)
            except (CsvError, TypeError, ValueError):
                error = "查询参数无效: 车道必选, 且需满足 0 <= 起点 < 终点(整数米)"
        cmp = None
        cmp_error = None
        if cmp_lane:
            picks = [dev_a, run_a, dev_b, run_b]
            if all(picks):
                try:
                    cmp = compare_runs(
                        conn, cmp_lane, dev_a, run_a, dev_b, run_b, RESULT_LIMIT
                    )
                except CsvError as e:
                    cmp_error = str(e)
            elif any(picks):
                cmp_error = "请完整选择两台不同设备各自的跑次"
        conn.commit()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "lanes": lanes,
                "batches": batches,
                "results": results,
                "truncated": truncated,
                "error": error,
                "q_lane": lane or "",
                "q_start": start_m or "",
                "q_end": end_m or "",
                "cmp": cmp,
                "cmp_error": cmp_error,
                "q_cmp_lane": cmp_lane or "",
                "q_dev_a": dev_a or "",
                "q_run_a": run_a or "",
                "q_dev_b": dev_b or "",
                "q_run_b": run_b or "",
                "msg": msg,
                "ok": ok == "1",
                "needed": NEEDED_DEVICES,
                "limit": RESULT_LIMIT,
            },
        )
    finally:
        conn.close()


@app.post("/upload/{kind}")
async def upload(
    kind: str,
    lane: str = Form(...),
    batch_no: str = Form(...),
    file: UploadFile = File(...),
):
    """上传一个批次(同车道同类型同批号即整批更正)。

    原始批次与交验结果在同一事务中提交; 任何失败都回滚, 两者沿用上次提交。
    """
    try:
        if kind not in KIND_NAMES:
            raise CsvError("未知的数据类型")
        lane = _clean_name(lane, "车道")
        batch_no = _clean_name(batch_no, "批号")
        raw = await file.read()
        if not raw:
            raise CsvError("文件为空")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise CsvError(f"文件超过大小上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CsvError("文件编码必须是 UTF-8")
        rows = parse_repave_csv(text) if kind == "repave" else parse_inspect_csv(text)
        if not rows:
            raise CsvError("CSV 没有数据行")
    except CsvError as e:
        return _redirect(f"上传失败, 已保留上次提交: {e}", ok=False)

    with UPLOAD_LOCK:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            apply_batch(conn, lane, kind, batch_no, rows, int(time.time()))
            conn.commit()
        except Exception as e:  # 原始批次与交验结果一起回滚
            conn.rollback()
            conn.close()
            return _redirect(f"提交失败, 已保留上次提交: {type(e).__name__}: {e}", ok=False)
        conn.close()
    return _redirect(
        f"{KIND_NAMES[kind]}批次「{batch_no}」已提交: 车道 {lane}, {len(rows)} 行, "
        f"交验结果已同步生效",
        ok=True,
    )


@app.get("/api/acceptance")
def api_acceptance(lane: str, start_m: int, end_m: int):
    if start_m < 0 or end_m <= start_m:
        return JSONResponse(
            {"error": "需满足 0 <= start_m < end_m"}, status_code=400
        )
    conn = get_conn()
    try:
        results, truncated = query_acceptance(conn, lane, start_m, end_m, RESULT_LIMIT)
    finally:
        conn.close()
    return {
        "lane": lane,
        "start_m": start_m,
        "end_m": end_m,
        "needed_devices": NEEDED_DEVICES,
        "truncated": truncated,
        "segments": results,
    }


@app.get("/api/inspect_runs")
def api_inspect_runs(lane: str):
    """某车道下有带测值(iri)记录的设备及其跑次(供比对页联动下拉)。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        devices = list_iri_runs(conn, lane)
        conn.commit()
    finally:
        conn.close()
    return {"lane": lane, "devices": devices}


@app.get("/api/iri_compare")
def api_iri_compare(lane: str, dev_a: str, run_a: str, dev_b: str, run_b: str):
    """比对两台不同设备各自的跑次: 最小共用区间、加权均值、差值与未配对里程。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")  # 同一读事务 -> 同一份已提交快照
        try:
            result = compare_runs(conn, lane, dev_a, run_a, dev_b, run_b, RESULT_LIMIT)
        except CsvError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        conn.commit()
    finally:
        conn.close()
    return result


@app.get("/api/batches")
def api_batches():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT lane,kind,batch_no,uploaded_at,row_count FROM batches "
            "ORDER BY lane, kind, batch_no"
        ).fetchall()
    finally:
        conn.close()
    return {"batches": [dict(r) for r in rows]}


@app.get("/health")
def health():
    return {"status": "ok"}
