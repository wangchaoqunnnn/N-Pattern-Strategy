# -*- coding: utf-8 -*-
"""REST API(全部相对路径, 前端同源访问; 不含任何静态/绝对地址)。"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Request

from .core import db
from .core import market as mk
from .core.util import get_logger, now_str, save_json, today_str, CONFIG_PATH
from .engine import backtest, optimizer, pool as poolmod, review as reviewmod
from .engine import scan as scanmod
from .engine import stats as statsmod
from .engine import strategy as strat
from .engine import trader as trader
from .service import svc

log = get_logger("api")
api = APIRouter(prefix="/api")


def _ok(data=None, **kw):
    out = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(kw)
    return out


def _err(msg: str, code: int = 400):
    return {"ok": False, "error": msg, "code": code}


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ---------------- 基础 ----------------
@api.get("/health")
def health():
    return _ok({"ts": now_str(), "status": "running"})


@api.get("/diag")
def diag():
    """服务器自检: 环境/数据源连通性/最近错误(排查"响应解析失败"类问题)。"""
    import platform
    import sys
    from .core import providers as P
    from .core.util import hhmm_now, trading_clock_state
    tests = {}
    for name, build in (
        ("sina_quote", lambda b: P.http_get_text(b + "sh600519",
                                                 headers={"Referer": "https://finance.sina.com.cn"},
                                                 charset="gbk", timeout=8, retries=1)),
        ("tencent_kline", lambda b: P.http_get_text(b + "?param=sh000001,day,2026-09-01,2026-09-07,5,",
                                                    timeout=8, retries=1)),
        ("sina_universe", lambda b: P.http_get_text(b + "?page=1&num=3&sort=symbol&asc=1&node=hs_a"
                                                    "&symbol=&_s_r_a=init", timeout=8, retries=1)),
    ):
        cfg = P._prov(name)
        base = cfg.get("base", "")
        ok, detail = False, ""
        try:
            txt = build(base)
            ok = bool(txt and len(txt) > 20)
            detail = txt[:40].replace("\n", " ")
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:140]
        tests[name] = {"reachable": ok, "detail": detail}
    errs = db.rows("SELECT ts,level,msg FROM engine_log WHERE level IN ('ERROR','WARN') "
                   "ORDER BY id DESC LIMIT 12")
    return _ok({
        "server_time": now_str(),
        "clock": trading_clock_state(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "engine_enabled": svc.engine_on(),
        "bootstrap_done": svc.bootstrap_done,
        "universe_count": db.scalar("SELECT COUNT(*) FROM universe", (), 0) or 0,
        "provider": tests,
        "recent_errors": errs,
    })


@api.get("/meta")
def meta():
    return _ok(svc.status())


@api.get("/logs")
def logs(limit: int = 300):
    return _ok(db.recent_logs(limit))


# ---------------- 推荐买入池 ----------------
@api.get("/pool")
def pool():
    return _ok(poolmod.list_pool())


@api.post("/pool/manual")
async def pool_manual(request: Request):
    b = await _body(request)
    code = (b.get("code") or "").strip()
    if not code:
        return _err("缺少股票代码")
    reason = b.get("reason") or "用户手动添加"
    r = poolmod.manual_add(code, reason)
    return _ok(r) if r.get("ok") else _err(r.get("error", "添加失败"))


@api.post("/pool/remove")
async def pool_remove(request: Request):
    b = await _body(request)
    code = (b.get("code") or "").strip()
    reason = b.get("reason") or "用户手动移除"
    return _ok(poolmod.manual_remove(code, reason)) if poolmod.manual_remove(code, reason) \
        else _err("池中不存在该股票")


@api.get("/pool/history")
def pool_history(limit: int = 200):
    return _ok(db.rows("SELECT * FROM pool ORDER BY id DESC LIMIT ?", (limit,)))


@api.get("/search")
def search(kw: str = "", limit: int = 30):
    return _ok(mk.find_stocks(kw, limit))


# ---------------- 交易系统版本 ----------------
@api.get("/strategy/current")
def strategy_current():
    v = strat.current_version()
    if not v:
        return _err("无可用版本")
    return _ok({"version": {k: v[k] for k in
                            ("id", "version_no", "name", "source", "reason",
                             "readable", "created_at", "is_active")},
                "params": v["params"]})


@api.get("/strategy/versions")
def strategy_versions():
    vs = strat.list_versions()
    return _ok([{k: v[k] for k in ("id", "version_no", "name", "source",
                                    "reason", "trigger", "readable",
                                    "created_at", "is_active")} for v in vs])


@api.post("/strategy/activate")
async def strategy_activate(request: Request):
    b = await _body(request)
    vid = int(b.get("id") or 0)
    if not vid or not strat.set_active(vid):
        return _err("版本不存在")
    return _ok({"active": vid})


@api.get("/strategy/performance")
def strategy_performance(months: str = ""):
    return _ok(statsmod.version_stats(None, months))


# ---------------- 交易系统买卖池 ----------------
@api.get("/positions")
def positions(status: str = "open"):
    if status == "open":
        return _ok(_positions_with_live(trader.open_positions()))
    rows = trader.closed_positions()
    out = []
    for p in rows:
        avg = db.scalar("SELECT AVG(price) FROM executions WHERE pos_id=? AND side='sell'",
                        (p["id"],))
        out.append({**p, "exit_price": round(float(avg or 0), 3)})
    return _ok(out)


def _positions_with_live(rows):
    out = []
    for p in rows:
        q = mk.market.quotes.get(p["code"]) or db.row("SELECT * FROM watch_quotes WHERE code=?",
                                                      (p["code"],))
        price = float((q or {}).get("price") or p["entry_price"])
        float_pct = (price / p["entry_price"] - 1) * 100 if p["entry_price"] else 0
        out.append({**p, "price": round(price, 3),
                    "float_pct": round(float_pct, 3),
                    "stop_price": p["stop_price"], "target_price": p["target_price"]})
    return out


@api.get("/executions")
def executions(limit: int = 400, code: str = "", side: str = ""):
    q = "SELECT e.*, p.name pname FROM executions e LEFT JOIN positions p ON p.id=e.pos_id "
    conds, args = [], []
    if code:
        conds.append("e.code=?")
        args.append(code)
    if side:
        conds.append("e.side=?")
        args.append(side)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY e.id DESC LIMIT ?"
    args.append(limit)
    return _ok(db.rows(q, args))


@api.post("/positions/close")
async def position_close(request: Request):
    b = await _body(request)
    pid = int(b.get("id") or 0)
    note = b.get("note") or ""
    if not pid:
        return _err("缺少持仓id")
    r = trader.manual_close(pid, svc.eff_cfg(), note)
    return _ok(r) if r.get("ok") else _err(r.get("error", "平仓失败"))


# ---------------- 统计复盘 ----------------
@api.get("/stats")
def stats():
    return _ok(statsmod.account())


@api.get("/stats/monthly")
def stats_monthly():
    return _ok(statsmod.monthly_stats())


@api.get("/stats/rule5")
def stats_rule5():
    return _ok(optimizer.rule5_window_state())


@api.get("/reviews")
def reviews(rtype: str = ""):
    return _ok(reviewmod.list_reviews(rtype))


@api.post("/reviews/run-daily")
async def reviews_run(request: Request):
    b = await _body(request)
    date = b.get("date") or ""
    r = reviewmod.run_daily_review(date)
    return _ok(r)


@api.post("/reviews/run-monthly")
async def reviews_run_monthly(request: Request):
    b = await _body(request)
    ym = b.get("ym") or ""
    r = reviewmod.run_monthly_review(ym)
    return _ok(r)


# ---------------- 自优化 ----------------
@api.get("/optimizations")
def optimizations():
    return _ok(optimizer.list_optimizations())


@api.post("/optimize/manual")
async def optimize_manual(request: Request):
    b = await _body(request)
    reason = b.get("reason") or "用户手动发起优化"
    r = optimizer.auto_optimize("manual", reason=reason, force=True)
    return _ok(r) if r.get("ok") else _err(r.get("error", "优化失败"))


# ---------------- 回测 ----------------
@api.get("/backtests")
def backtests():
    return _ok(db.rows("SELECT id,status,progress,created_at,params,error,summary "
                       "FROM backtests ORDER BY id DESC LIMIT 50"))


@api.post("/backtests")
async def backtests_new(request: Request):
    b = await _body(request)
    start, end = (b.get("start") or "").strip(), (b.get("end") or "").strip()
    if not start or not end or start > end:
        return _err("请输入有效回测区间(start<=end)")
    try:
        jid = backtest.start_backtest(start, end,
                                      int(b.get("version_id") or 0) or None,
                                      float(b.get("capital") or 1_000_000))
    except Exception as e:  # noqa: BLE001
        return _err(f"启动回测失败: {e}")
    return _ok({"id": jid, "status": "running"})


@api.get("/backtests/{jid}")
def backtests_get(jid: int):
    r = db.row("SELECT * FROM backtests WHERE id=?", (jid,))
    if not r:
        return _err("回测不存在", 404)
    r["params"] = json.loads(r["params"] or "{}")
    r["summary"] = json.loads(r["summary"] or "{}")
    r["trades"] = json.loads(r["trades"] or "[]")
    r["equity"] = json.loads(r["equity"] or "[]")
    return _ok(r)


@api.post("/backtests/{jid}/cancel")
def backtests_cancel(jid: int):
    return _ok({"cancelled": backtest.cancel_backtest(jid)})


# ---------------- 引擎控制 ----------------
@api.post("/engine/toggle")
async def engine_toggle(request: Request):
    b = await _body(request)
    svc.set_engine_enabled(bool(b.get("enabled", False)))
    return _ok({"enabled": svc.engine_on()})


@api.post("/engine/scan")
async def engine_scan(request: Request):
    """立即扫描(异步提交, 秒回; 结果看仪表盘 最近扫描/信号数)。"""
    return _ok(svc.kick_scan())


@api.post("/engine/close")
async def engine_close(request: Request):
    """收盘复盘流程(异步提交)。"""
    b = await _body(request)
    if b.get("date"):
        from .core.util import parse_date
        return _err("手动指定日期的收盘流程仅限同步接口, 请不带date提交(系统自动按当日)")
    return _ok(svc.kick_close())


@api.post("/engine/sync")
async def engine_sync(request: Request):
    """一键同步(异步): 先确保全市场股票列表, 再后台增量同步历史K线。"""
    return _ok(svc.kick_sync())


@api.post("/engine/sync-universe")
async def engine_sync_universe(request: Request):
    """仅重试全市场股票列表同步(异步)。"""
    return _ok(svc.kick_universe())


@api.get("/config")
def config_get():
    v = strat.current_version()
    return _ok({
        "engine": {"enabled": svc.engine_on(),
                   "buy_start_hhmm": svc.engine_cfg.get("buy_start_hhmm", "0935"),
                   "buy_end_hhmm": svc.engine_cfg.get("buy_end_hhmm", "1445"),
                   "sell_end_hhmm": svc.engine_cfg.get("sell_end_hhmm", "1455")},
        "scheduler": {"data_refresh_sec": svc.interval("data_refresh_sec",
                                                       svc.cfg.get("data_refresh_sec", 5)),
                      "scan_interval_sec": svc.interval("scan_interval_sec",
                                                        svc.cfg.get("scan_interval_sec", 60))},
        "base_capital": svc.cfg.get("base_capital", 1_000_000),
        "strategy_readable": v["readable"] if v else "",
    })


@api.put("/config")
async def config_put(request: Request):
    b = await _body(request)
    if "enabled" in b:
        svc.set_engine_enabled(bool(b["enabled"]))
    if b.get("data_refresh_sec"):
        db.meta_set("data_refresh_sec", int(b["data_refresh_sec"]))
    if b.get("scan_interval_sec"):
        db.meta_set("scan_interval_sec", int(b["scan_interval_sec"]))
    svc.cfg.update({k: v for k, v in b.items() if k in ("engine",)})
    return _ok({"saved": True})


# ---------------- 行情/K线 ----------------
@api.get("/quote/{code}")
def quote(code: str):
    q = mk.market.quotes.get(code) or db.row("SELECT * FROM watch_quotes WHERE code=?", (code,))
    return _ok(q)


@api.get("/kline/{code}")
def kline(code: str, days: int = 160):
    series = mk.market.series(code, max_len=days)
    if not series:
        return _err("无K线数据")
    live = mk.market.live_bar(code)
    if live:
        series = scanmod.append_live(series, live)
    markers = db.rows("SELECT substr(entry_dt,1,10) d,'买' t, entry_price p FROM positions "
                      "WHERE code=? UNION ALL SELECT substr(closed_dt,1,10), '卖', "
                      "(SELECT AVG(price) FROM executions e WHERE e.pos_id=positions.id AND side='sell') "
                      "FROM positions WHERE code=? AND status='closed' ORDER BY d",
                      (code, code))
    return _ok({"series": series, "markers": markers,
                "name": mk.market.universe_name.get(code, code)})
