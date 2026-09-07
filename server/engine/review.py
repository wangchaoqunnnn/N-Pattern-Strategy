# -*- coding: utf-8 -*-
"""复盘引擎: 每日收盘复盘(分析买入失败原因并写入备注) + 月度复盘(判断是否优化交易系统)。

每个失败平仓都会做"失败归因"(写入 positions.failure / failure_note / note),
归因结果供自优化器选择参数调整方向。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..core import db
from ..core.market import market
from ..core.util import get_logger, num, now_str, parse_date, today_str
from . import stats as statsmod

log = get_logger("review")

FAILURE_KINDS = {
    "early_entry": "买点过早/趋势走弱(入场后未浮盈即破位)",
    "fake_breakout": "假突破追高(突破后冲高回落)",
    "stop_too_tight": "止损过近被洗出(离场后重新上涨)",
    "bad_environment": "大盘拖累(持有期大盘下跌)",
    "pattern_break": "形态破位(跌破N字结构)",
    "data_suspend": "停牌/数据异常",
    "other": "其他",
}


def _exec_tags(pos: dict) -> List[str]:
    rows = db.rows("SELECT tags FROM executions WHERE pos_id=? AND side='sell' "
                   "ORDER BY id DESC LIMIT 1", (pos["id"],))
    if not rows:
        return []
    try:
        return (json.loads(rows[0]["tags"] or "{}").get("tags")) or []
    except Exception:
        return []


def _series_window(code: str, start: str, end: str) -> List[dict]:
    rows = db.rows("SELECT date,open,high,low,close,vol_shares,pct_chg FROM daily_bars "
                   "WHERE code=? AND date>=? AND date<=? ORDER BY date", (code, start, end))
    return rows


def classify_failure(pos: dict) -> dict:
    """对亏损单做失败归因(含持仓期大盘环境)。"""
    entry = num(pos.get("entry_price"))
    code = pos["code"]
    tags = _exec_tags(pos)
    reason_txt = str(pos.get("exit_reason") or "")
    is_loss = num(pos.get("realized_pnl")) <= 0
    if not is_loss:
        return {"failure": None, "note": ""}
    fk = "other"
    e_date = (pos.get("entry_dt") or "")[:10]
    c_date = (pos.get("closed_dt") or "")[:10]
    note = f"入场{pos.get('entry_dt')}@{entry:.2f}, 离场{pos.get('exit_reason')}"
    idx_chg = index_change_between(e_date, c_date or today_str())
    if "manual" in tags:
        fk, note = "other", note + " [人工干预平仓, 不归咎于交易系统]"
    elif "停牌" in note or "数据" in note:
        fk = "data_suspend"
    else:
        bars = _series_window(code, e_date, c_date or today_str())
        best_after = 0.0
        for b in bars:
            if b["date"] > e_date:
                best_after = max(best_after, num(b["high"]))
        up = (best_after / entry - 1) * 100 if entry else 0.0
        if idx_chg is not None and idx_chg < -3:
            fk = "bad_environment"
            note += f"; 持有期沪深300跌{idx_chg:.1f}%, 大盘拖累明显"
        elif up < 3:
            if "破位" in reason_txt or "形态" in reason_txt or "止损" in reason_txt:
                fk = "early_entry"
                note += f"; 入场后最高仅+{up:.1f}%即破位/止损 → 买点过早或趋势走弱"
            else:
                fk = "pattern_break"
                note += f"; 持有期最高仅+{up:.1f}%, 形态走坏"
        else:
            fk = "fake_breakout"
            note += f"; 入场后曾达+{up:.1f}%但未能延续, 冲高回落/假突破"
    note += f"。盈亏{num(pos.get('realized_pnl')):+.2f}元({num(pos.get('realized_pnl_pct')):+.2f}%)"
    return {"failure": fk, "failure_label": FAILURE_KINDS.get(fk, fk), "note": note}


def index_change_between(start: str, end: str) -> Optional[float]:
    closes = market.index_closes("sh000300")
    cs = [x for x in closes if start <= x["date"] <= end]
    if len(cs) < 2:
        return None
    return (cs[-1]["close"] - cs[0]["close"]) / cs[0]["close"] * 100


def run_daily_review(date: str = "") -> dict:
    """每日收盘复盘: 分析当日平仓成败, 失败归因写备注, 生成复盘记录。"""
    date = date or today_str()
    closed_today = db.rows("SELECT * FROM positions WHERE status='closed' AND "
                           "substr(closed_dt,1,10)=? ORDER BY closed_dt", (date,))
    open_now = db.rows("SELECT * FROM positions WHERE status='open'")
    lines = [f"# {date} 收盘复盘", ""]
    st = statsmod.stats_of(closed_today)
    lines.append(f"## 今日平仓统计: 平仓{st['n']}笔 盈利{st['wins']}笔 亏损{st['losses']}笔 "
                 f"成功率{st['winrate']:.1f}% 盈亏比{st['profit_loss_ratio']:.2f} "
                 f"当日已实现{st['total_pnl']:+.2f}元")
    lines.append("")
    failures_notes = []
    if closed_today:
        lines.append("### 今日平仓明细与失败归因(备注)")
        for p in closed_today:
            res = classify_failure(p)
            avg_sell = db.scalar("SELECT AVG(price) FROM executions WHERE pos_id=? AND side='sell'",
                                 (p["id"],))
            flag = "✅盈利" if num(p["realized_pnl"]) > 0 else f"❌亏损 归因:{res['failure_label']}"
            db.execute("UPDATE positions SET failure=?, failure_note=?, "
                       "note=COALESCE(? , note) WHERE id=?",
                       (res["failure"], res["note"], res["note"], p["id"]))
            if num(p["realized_pnl"]) <= 0:
                failures_notes.append(f"[{p['code']} {p['name']}] {res['note']}")
            lines.append(f"- {p['code']} {p['name']}: 买@{num(p['entry_price']):.2f} "
                         f"卖@{num(avg_sell or 0):.2f} {flag} "
                         f"已实现{num(p['realized_pnl']):+.2f}元; 离场原因: {p['exit_reason']}")
            lines.append(f"  - 备注: {res['note']}")
    else:
        lines.append("今日无平仓记录。")
    lines.append("")
    lines.append("### 持仓状态")
    if open_now:
        for p in open_now:
            q = market.quotes.get(p["code"])
            price = num(q.get("price")) if q else num(p["entry_price"])
            fp = (price / num(p["entry_price"]) - 1) * 100 if num(p["entry_price"]) else 0
            lines.append(f"- {p['code']} {p['name']} 持仓中 现价{price:.2f} 浮动{fp:+.1f}% "
                         f"止损{num(p['stop_price']):.2f}")
    else:
        lines.append("当前空仓。")
    lines.append("")
    env = market.index_closes("sh000300")
    if env:
        c20 = (env[-1]["close"] - env[-20]["close"]) / env[-20]["close"] * 100 if len(env) >= 20 else 0
        lines.append(f"### 环境: 沪深300近20日{c20:+.2f}%, 池内股票{db.scalar('SELECT COUNT(*) FROM pool WHERE status=\"in\"') or 0}只。")
    notes = "\n".join(failures_notes) if failures_notes else "今日无失败买入。"
    content = "\n".join(lines)
    stats_j = json.dumps(st, ensure_ascii=False)
    db.execute(
        "INSERT INTO reviews(rtype,date,title,content,stats,notes,created_at,updated_at) "
        "VALUES('daily',?,?,?,?,?,?,?) ON CONFLICT(rtype,date) DO UPDATE SET "
        "content=excluded.content,stats=excluded.stats,notes=excluded.notes,updated_at=excluded.updated_at",
        (date, f"{date} 每日收盘复盘", content, stats_j, notes, now_str(), now_str()))
    db.meta_set("last_daily_review", date)
    db.log_event("INFO", f"每日复盘完成 {date}: 平仓{st['n']}笔")
    return {"date": date, "stats": st, "notes": notes, "content": content}


def run_monthly_review(ym: str = "") -> dict:
    """月度复盘: 统计当月交易、失败归因汇总, 判断是否需优化。"""
    if not ym:
        ym = today_str()[:7]
    trades = db.rows("SELECT * FROM positions WHERE status='closed' AND substr(entry_dt,1,7)=? "
                     "ORDER BY closed_dt", (ym,))
    st = statsmod.stats_of(trades)
    fails = [t for t in trades if num(t["realized_pnl"]) <= 0]
    fcount: Dict[str, int] = {}
    for t in fails:
        fk = t.get("failure") or "other"
        fcount[fk] = fcount.get(fk, 0) + 1
    lines = [f"# {ym} 月度复盘", ""]
    lines.append(f"当月平仓 {st['n']} 笔: 盈利 {st['wins']} / 亏损 {st['losses']}；"
                 f"成功率 {st['winrate']:.1f}%; 盈亏比 {st['profit_loss_ratio']:.2f}; "
                 f"已实现盈亏 {st['total_pnl']:+.2f} 元 ({st['total_pnl_pct']:+.2f}%)")
    lines.append("")
    lines.append("### 失败归因汇总")
    if fcount:
        for k, v in sorted(fcount.items(), key=lambda x: -x[1]):
            lines.append(f"- {FAILURE_KINDS.get(k, k)}: {v} 笔")
    else:
        lines.append("当月无失败交易。")
    lines.append("")
    lines.append("### 交易明细")
    for t in trades:
        lines.append(f"- {t['code']} {t['name']} v{t['version_no']} "
                     f"{'盈' if num(t['realized_pnl']) > 0 else '亏'}{abs(num(t['realized_pnl'])):.0f}元 "
                     f"({num(t['realized_pnl_pct']):+.1f}%) 备注: {t.get('note') or t.get('exit_reason')}")
    need_opt = (st["n"] >= 3 and (st["winrate"] < 50 or st["profit_loss_ratio"] < 1 or st["total_pnl"] < 0))
    if st["n"] < 3:
        judge = "当月样本不足(平仓<3笔), 暂不触发系统优化, 记录观察。"
        need_opt = False
    elif need_opt:
        judge = ("当月成功率/盈亏比/收益未达标, 判定【需要优化交易系统】, "
                 "将自动创建并切换到新的优化版本。")
    else:
        judge = "当月指标达标或可接受, 判定【无需优化】, 继续使用当前版本。"
    lines.append("")
    lines.append(f"### 优化判定\n{judge}")
    content = "\n".join(lines)
    db.execute(
        "INSERT INTO reviews(rtype,date,title,content,stats,notes,created_at,updated_at) "
        "VALUES('monthly',?,?,?,?,?,?,?) ON CONFLICT(rtype,date) DO UPDATE SET "
        "content=excluded.content,stats=excluded.stats,notes=excluded.notes,updated_at=excluded.updated_at",
        (ym, f"{ym} 月度复盘", content, json.dumps(st, ensure_ascii=False),
         json.dumps(fcount, ensure_ascii=False), now_str(), now_str()))
    db.meta_set("last_monthly_review", ym)
    db.log_event("INFO", f"月度复盘完成 {ym}: 平仓{st['n']}笔 需要优化={need_opt}")
    return {"ym": ym, "stats": st, "need_optimize": need_opt,
            "failure_count": fcount, "content": content, "judge": judge}


def list_reviews(rtype: str = "") -> List[dict]:
    if rtype:
        return db.rows("SELECT * FROM reviews WHERE rtype=? ORDER BY date DESC LIMIT 100", (rtype,))
    return db.rows("SELECT * FROM reviews ORDER BY date DESC LIMIT 200")


def failure_tally(limit_trades: int = 30) -> Dict[str, int]:
    rows = db.rows("SELECT failure FROM positions WHERE status='closed' AND realized_pnl<=0 "
                   "ORDER BY closed_dt DESC LIMIT ?", (limit_trades,))
    out: Dict[str, int] = {}
    for r in rows:
        k = r["failure"] or "other"
        out[k] = out.get(k, 0) + 1
    return out
