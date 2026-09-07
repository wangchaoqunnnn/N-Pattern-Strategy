# -*- coding: utf-8 -*-
"""绩效统计: 成功率/盈亏比/权益/月度/窗口滚动指标(供仪表盘与自优化触发器使用)。"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..core import db
from ..core.market import market
from ..core.util import num
from .indicators import max_drawdown


def _closed_in(dates: List[str]) -> List[dict]:
    if not dates:
        return []
    qs = ",".join("?" * len(dates))
    rows = db.rows(f"SELECT * FROM positions WHERE status='closed' AND "
                   f"substr(closed_dt,1,10) IN ({qs}) ORDER BY closed_dt", dates)
    return rows


def stats_of(trades: List[dict]) -> dict:
    """由平仓交易列表计算指标。"""
    n = len(trades)
    wins = [t for t in trades if num(t.get("realized_pnl")) > 0]
    losses = [t for t in trades if num(t.get("realized_pnl")) <= 0]
    total_pnl = sum(num(t.get("realized_pnl")) for t in trades)
    winrate = len(wins) / n * 100 if n else 0.0
    avg_win = (sum(num(t.get("realized_pnl_pct")) for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(abs(num(t.get("realized_pnl_pct"))) for t in losses) / len(losses)) if losses else 0.0
    rr = (avg_win / avg_loss) if avg_loss > 0 else (0.0 if not wins else 99.0)
    return {"n": n, "wins": len(wins), "losses": len(losses),
            "winrate": round(winrate, 2), "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_loss_ratio": round(rr, 3),        # 盈亏比=平均盈利/平均亏损
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / equity_base() * 100, 2) if equity_base() else 0}


def equity_base() -> float:
    return num(db.meta_get("capital_base") or 1_000_000)


def equity_curve() -> List[dict]:
    """已实现权益曲线(按平仓日累计)。"""
    rows = db.rows("SELECT substr(closed_dt,1,10) d, SUM(realized_pnl) p "
                   "FROM positions WHERE status='closed' GROUP BY d ORDER BY d")
    base = equity_base()
    curve = []
    eq = base
    for r in rows:
        eq += num(r["p"])
        curve.append({"date": r["d"], "equity": round(eq, 2),
                      "pnl": round(num(r["p"]), 2)})
    return curve


def account() -> dict:
    """账户与绩效总览。"""
    base = equity_base()
    closed = closed_list()
    realized = sum(num(t["realized_pnl"]) for t in closed)
    st = stats_of(closed)
    curve = equity_curve()
    eqs = [c["equity"] for c in curve]
    mdd = max_drawdown([base] + eqs) if eqs else 0.0
    open_ps = db.rows("SELECT * FROM positions WHERE status='open'")
    open_unreal = 0.0
    for p in open_ps:
        q = market.quotes.get(p["code"])
        price = num(q.get("price")) if q else num(p["entry_price"])
        open_unreal += (price - num(p["entry_price"])) * num(p["entry_shares"])
    return {"base": base, "realized": round(realized, 2),
            "unrealized": round(open_unreal, 2),
            "equity": round(base + realized + open_unreal, 2),
            "equity_realized": round(base + realized, 2),
            "max_drawdown_pct": round(mdd, 2),
            "open_positions": len(open_ps),
            "stats_all": st, "curve": curve[-400:],
            "n_trades": st["n"]}


def closed_list(limit: int = 500) -> List[dict]:
    return db.rows("SELECT * FROM positions WHERE status='closed' ORDER BY closed_dt DESC LIMIT ?",
                   (limit,))


def monthly_stats() -> List[dict]:
    rows = db.rows("SELECT substr(closed_dt,1,7) ym, COUNT(*) n, "
                   "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) w, "
                   "SUM(realized_pnl) p FROM positions WHERE status='closed' "
                   "GROUP BY ym ORDER BY ym DESC")
    out = []
    for r in rows:
        trades = db.rows("SELECT * FROM positions WHERE status='closed' AND "
                         "substr(closed_dt,1,7)=? ", (r["ym"],))
        st = stats_of(trades)
        out.append({"ym": r["ym"], "n": r["n"], "wins": r["w"],
                    "winrate": st["winrate"], "profit_loss_ratio": st["profit_loss_ratio"],
                    "pnl": round(num(r["p"]), 2)})
    return out


def session_windows(days: int = 5) -> List[dict]:
    """近 days 个交易日中, 每个交易日往前 days 天窗口的已平仓统计(自优化触发器输入)。"""
    tdates = market.trading_dates()
    if not tdates:
        return []
    out = []
    for d in tdates[-days:]:
        i = tdates.index(d)
        win = tdates[max(0, i - days + 1): i + 1]
        trades = _closed_in(win)
        st = stats_of(trades)
        st["window_end"] = d
        out.append(st)
    return out


def version_stats(version_id: Optional[int] = None, months: Optional[str] = None) -> dict:
    """按版本统计(若version_id None 则全部; months 形如 2026-09)。"""
    q = "SELECT * FROM positions WHERE status='closed'"
    args = []
    if version_id:
        q += " AND version_id=?"
        args.append(version_id)
    if months:
        q += " AND substr(entry_dt,1,7)=?"
        args.append(months)
    trades = db.rows(q + " ORDER BY closed_dt", args)
    st = stats_of(trades)
    st["by_version"] = db.rows(
        "SELECT version_no, version_id, COUNT(*) n, "
        "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) w, SUM(realized_pnl) p "
        "FROM positions WHERE status='closed' "
        + ("AND version_id=? " if version_id else "") +
        "GROUP BY version_no, version_id ORDER BY version_no DESC",
        [version_id] if version_id else [])
    return st
