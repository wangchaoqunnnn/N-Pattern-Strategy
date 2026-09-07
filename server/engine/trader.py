# -*- coding: utf-8 -*-
"""交易执行引擎: 严格按照当前交易系统版本执行买卖, 全部操作写入交易系统买卖池(executions)。

- 买入: 仅对推荐买入池中当日新鲜的 B1/B2 信号, 满足环境闸门/仓位/冷却等风控后执行;
- 卖出: 盘中每个刷新周期检查 硬止损/形态止损/目标分批止盈/移动止盈;
- 每条执行记录保存 交易系统版本号/理由/标签, 支撑复盘与统计。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..core import db
from ..core.market import market
from ..core.util import get_logger, num, now_str, parse_dt, today_str
from . import scan as scanmod
from .indicators import ma

log = get_logger("trader")

BUY_FEE = 0.00025
SELL_FEE = 0.00075


def t1_sellable(pos: dict, today: str = "") -> bool:
    """A股T+1: 当日买入的股票当日(同一交易日)禁止卖出, 最早须下一交易日方可卖。
    pos 的 entry_dt 为买入成交时间(北京时间)。交易日由日期串比较即可(下一交易日必然日期更大)。"""
    today = today or today_str()
    entry_day = (pos.get("entry_dt") or "")[:10]
    return entry_day < today


def capital_base(cfg: dict) -> float:
    v = db.meta_get("capital_base")
    if v is None:
        db.meta_set("capital_base", float(cfg.get("base_capital", 1_000_000)))
        v = str(float(cfg.get("base_capital", 1_000_000)))
    return float(v)


def realized_total() -> float:
    return num(db.scalar("SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed'"))


def equity_now() -> float:
    """账户权益 = 初始资金 + 已实现盈亏(未含持仓浮盈)。"""
    return capital_base({}) + realized_total()


def open_positions() -> List[dict]:
    return db.rows("SELECT * FROM positions WHERE status='open' ORDER BY entry_dt")


def closed_positions(limit: int = 500) -> List[dict]:
    return db.rows("SELECT * FROM positions WHERE status='closed' ORDER BY closed_dt DESC LIMIT ?",
                   (limit,))


# ------------------------------------------------------------------ 执行落库
def _fees(amount: float, side: str) -> float:
    return amount * (SELL_FEE if side == "sell" else BUY_FEE)


def _next_pos_id() -> int:
    return db.scalar("SELECT COALESCE(MAX(id),0)+1 FROM positions", (), 0)


def execute_buy(code: str, name: str, board: str, price_ref: float, sig: dict,
                version: dict, params: dict, cfg: dict, mode: str = "system",
                force_shares: int = 0) -> Optional[dict]:
    """执行买入。price_ref 为最新现价。按当前版本 risk 参数计算仓位。"""
    q = market.quotes.get(code)
    price = q.get("price", 0) if q else 0
    price = float(price) if price and price > 0 else float(price_ref)
    if price <= 0:
        return None
    eng = cfg.get("engine") or {}
    risk = params.get("risk") or {}
    slippage = num(eng.get("slippage_pct", 0.1)) / 100.0
    fill = price * (1 + slippage)
    eq = equity_now()
    pos_pct = num(risk.get("position_pct", 0.2))
    amount_allowed = eq * pos_pct
    if force_shares and force_shares > 0:
        shares = force_shares
    else:
        shares = int(amount_allowed / fill / 100) * 100
    if shares < 100:
        return None
    amount = fill * shares
    if amount > eq * 0.95:
        shares = int(eq * 0.95 / fill / 100) * 100
        amount = fill * shares
    if shares < 100:
        return None
    fee = _fees(amount, "buy")
    sellp = params.get("sell") or {}
    hard_stop = fill * (1 - num(sellp.get("hard_stop_pct", 8.0)) / 100.0)
    zlow = num(sig.get("zone_low")) or (fill * 0.9)
    tol = num(sellp.get("pattern_stop_tol", 0.99))
    pattern_stop = zlow * tol
    stop = max(hard_stop, pattern_stop) if pattern_stop < fill else hard_stop
    stop = min(stop, fill)                     # 止损绝不高于成本
    target = fill * (1 + num(sellp.get("target_pct", 25.0)) / 100.0)
    vno = version.get("version_no") if version else "?"
    reason = (f"按交易系统 v{vno} {('B2放量突破' if sig.get('kind') == 'b2' else 'B1回踩企稳')}信号买入: "
              f"点火{sig.get('ignite_date')}(涨{sig.get('ignite_pct'):+.2f}%量比{sig.get('ignite_vol_ratio'):.2f}), "
              f"回调{sig.get('pull_days')}天; 信号价{sig.get('ref_price')}; 成交价≈{fill:.2f}。"
              f"止损{stop:.2f}(-{num(sellp.get('hard_stop_pct', 8.0)):.0f}%硬止损/形态止损取较高), "
              f"目标+{num(sellp.get('target_pct', 25.0)):.0f}%分批。")
    now = now_str()
    pos_id = _next_pos_id()
    db.execute(
        "INSERT INTO positions(id,code,name,board,status,entry_dt,entry_price,entry_shares,"
        "entry_amount,entry_reason,signal,stop_price,target_price,partial_done,version_id,"
        "version_no,peak_high,peak_dt,created_at,updated_at,note) "
        "VALUES(?,?,?,?,'open',?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
        (pos_id, code, name, board, now, fill, shares, amount, reason,
         sig.get("kind"), stop, target, version.get("id"), str(vno), price, now, now, now,
         f"止损位按『第二拉升阳底/N型回调低点×{tol} 与 -{num(sellp.get('hard_stop_pct', 8.0))}%硬止损孰高』设定"))
    db.execute(
        "INSERT INTO executions(pos_id,code,name,side,dt,price,shares,amount,fee,reason,tags,"
        "version_id,version_no,mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos_id, code, name, "buy", now, round(fill, 3), shares, round(amount, 2),
         round(fee, 2), reason, json.dumps({"signal": sig.get("kind"), "mode": mode}),
         version.get("id"), str(vno), mode, now))
    db.log_event("INFO", f"系统买入 v{vno}: {code} {name} {shares}股 @{fill:.2f} 金额{amount:.0f} "
                         f"信号{sig.get('kind')} 止损{stop:.2f} 目标{target:.2f}")
    return {"pos_id": pos_id, "code": code, "name": name, "shares": shares,
            "fill": round(fill, 3), "amount": round(amount, 2), "reason": reason}


def execute_sell(pos: dict, price_ref: float, reason: str, tags: List[str],
                 version: dict, cfg: dict, mode: str = "system",
                 shares_to_sell: Optional[int] = None, note: str = "") -> Optional[dict]:
    """卖出(支持分批). 卖完即平仓结算, 部分卖出更新已实现盈亏。返回执行信息或None。"""
    q = market.quotes.get(pos["code"])
    price = float(q.get("price", 0)) if q else 0
    price = price if price and price > 0 else float(price_ref)
    if price <= 0:
        return None
    eng = cfg.get("engine") or {}
    slippage = num(eng.get("slippage_pct", 0.1)) / 100.0
    fill = price * (1 - slippage)
    total_shares = int(pos["entry_shares"])
    sold = int(num(db.scalar(
        "SELECT COALESCE(SUM(shares),0) FROM executions WHERE pos_id=? AND side='sell'",
        (pos["id"],))))
    remain = total_shares - sold
    if remain <= 0:
        return None
    n_sell = min(shares_to_sell if shares_to_sell and shares_to_sell > 0 else remain, remain)
    if n_sell <= 0:
        return None
    amount = fill * n_sell
    fee = _fees(amount, "sell")
    entry = num(pos["entry_price"])
    entry_fee = num(db.scalar(
        "SELECT fee FROM executions WHERE pos_id=? AND side='buy' ORDER BY id LIMIT 1", (pos["id"],)))
    entry_fee_part = entry_fee * n_sell / total_shares if total_shares else 0.0
    pnl = (fill - entry) * n_sell - entry_fee_part - fee
    now = now_str()
    vno = version.get("version_no") if version else pos.get("version_no")
    db.execute(
        "INSERT INTO executions(pos_id,code,name,side,dt,price,shares,amount,fee,reason,tags,"
        "version_id,version_no,mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos["id"], pos["code"], pos["name"], "sell", now, round(fill, 3), n_sell,
         round(amount, 2), round(fee, 2), reason, json.dumps({"tags": tags, "mode": mode}),
         version.get("id") if version else pos.get("version_id"), str(vno), mode, now))
    cur_pnl = num(pos.get("realized_pnl"))
    new_pnl = cur_pnl + pnl
    entry_amount = num(pos["entry_amount"])
    new_sold = sold + n_sell
    if new_sold >= total_shares:                       # 平仓
        pnl_pct = new_pnl / entry_amount * 100 if entry_amount else 0.0
        hold_days = 1
        try:
            hold_days = max(1, (parse_dt(now) - parse_dt(pos["entry_dt"])).days + 1)
        except Exception:
            pass
        db.execute(
            "UPDATE positions SET status='closed', closed_dt=?, exit_reason=?, "
            "realized_pnl=?, realized_pnl_pct=?, holding_days=?, "
            "note=COALESCE(?, note), updated_at=? WHERE id=?",
            (now, reason, round(new_pnl, 2), round(pnl_pct, 4), hold_days,
             note or None, now, pos["id"]))
        db.log_event("INFO", f"系统平仓 v{vno}: {pos['code']} {pos['name']} "
                             f"已实现{new_pnl:+.2f}元({pnl_pct:+.2f}%) 原因: {reason}")
    else:                                              # 部分了结
        pnl_pct = new_pnl / (entry_amount * new_sold / total_shares) * 100 if entry_amount else 0.0
        db.execute("UPDATE positions SET realized_pnl=?, updated_at=? WHERE id=?",
                   (round(new_pnl, 2), now, pos["id"]))
        db.log_event("INFO", f"系统分批止盈: {pos['code']} {pos['name']} {n_sell}股 @{fill:.2f} "
                             f"(剩余{total_shares - new_sold}股) 原因: {reason}")
    return {"pos_id": pos["id"], "shares": n_sell, "fill": round(fill, 3),
            "pnl": round(pnl, 2), "closed": new_sold >= total_shares}


# ------------------------------------------------------------------ 卖出检查
def check_sells(version: dict, params: dict, cfg: dict) -> List[dict]:
    """对全部持仓执行卖出规则(使用当前实时价)。返回本次执行列表。"""
    outs = []
    sellp = params.get("sell") or {}
    eng = cfg.get("engine") or {}
    hard = num(sellp.get("hard_stop_pct", 8.0))
    target = num(sellp.get("target_pct", 25.0))
    partial = num(sellp.get("partial_ratio", 0.5))
    trail = num(sellp.get("trail_pct", 15.0))
    trail_act = num(sellp.get("trail_activate_pct", 5.0))
    for pos in open_positions():
        q = market.quotes.get(pos["code"])
        if not q:
            continue
        price = num(q.get("price"))
        high = num(q.get("high")) or price
        if price <= 0:
            continue
        entry = num(pos["entry_price"])
        peak = max(num(pos["peak_high"]), high, price, entry)
        db.execute("UPDATE positions SET peak_high=?, peak_dt=? WHERE id=?",
                   (peak, now_str(), pos["id"]))
        pos["peak_high"] = peak
        # T+1 闸门: 当日买入的持仓不可在当日卖出(硬约束, 先于一切卖出规则)
        if not t1_sellable(pos):
            continue
        # 1 硬/形态止损
        if price <= num(pos["stop_price"]):
            r = (f"止损离场(现价{price:.2f}≤止损位{num(pos['stop_price']):.2f}): "
                 f"{'形态止损' if num(pos['stop_price']) > entry * (1 - hard / 100) else f'硬止损-{hard:.0f}%'}")
            ex = execute_sell(pos, price, r, ["stop", "loss"], version, cfg)
            if ex:
                outs.append(ex)
            continue
        # 2 目标分批止盈 (+target% 先出 partial 比例)
        if not pos["partial_done"] and price >= num(pos["target_price"]):
            n = int(pos["entry_shares"] * partial / 100) * 100
            if n < 100:
                n = int(pos["entry_shares"])  # 持仓过小一次性
            r = f"目标止盈+{target:.0f}%分批了结({partial * 100:.0f}%仓位)"
            ex = execute_sell(pos, price, r, ["target", "partial"], version, cfg)
            if ex:
                outs.append(ex)
                db.execute("UPDATE positions SET partial_done=1, stop_price=entry_price, updated_at=? "
                           "WHERE id=?", (now_str(), pos["id"]))
            continue
        # 3 移动止盈: 浮盈≥激活值后从高点回撤 trail%
        peak_pct = (peak - entry) / entry * 100 if entry else 0
        if peak_pct >= trail_act and price <= peak * (1 - trail / 100):
            r = (f"移动止盈: 最高{peak:.2f}(+{peak_pct:.1f}%) 回撤{trail:.0f}%至{price:.2f}触发离场")
            ex = execute_sell(pos, price, r, ["trail", "profit"], version, cfg)
            if ex:
                outs.append(ex)
            continue
        # 4 尾盘破昨收(可选, 默认关)
        if sellp.get("tail_stop"):
            from ..core.util import hhmm_now
            prev = num(q.get("prev_close"))
            if hhmm_now() >= 1450 and prev > 0 and price < prev:
                r = f"尾盘止损: 价格{price:.2f}跌破昨收{prev:.2f}"
                ex = execute_sell(pos, price, r, ["tail_stop", "loss"], version, cfg)
                if ex:
                    outs.append(ex)
    return outs


# ------------------------------------------------------------------ 买入决策
def buyable(sig: dict, today: str) -> bool:
    return sig and sig.get("kind") in ("b1", "b2") and sig.get("date") == today


def attempt_buys(signals: List[dict], version: dict, params: dict, cfg: dict,
                 gate: dict) -> List[dict]:
    """按信号质量排序后尝试买入(受环境闸门/仓位/每日笔数/冷却限制)。返回成交列表。"""
    eng = cfg.get("engine") or {}
    if not eng.get("enabled", True):
        return []
    risk = params.get("risk") or {}
    mode = gate.get("mode", "full")
    if mode == "off":
        return []
    half = mode == "half"
    max_pos = int(risk.get("max_positions", 5))
    pos_pct = num(risk.get("position_pct", 0.2))
    if half:                     # 环境震荡: 半仓运行(总持仓数与单票上限减半)
        max_pos = max(1, max_pos // 2)
        pos_pct = pos_pct / 2
    open_codes = {p["code"] for p in open_positions()}
    if len(open_codes) >= max_pos:
        return []
    today = today_str()
    bought_today = num(db.scalar(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' AND dt LIKE ? THEN 1 ELSE 0 END),0) "
        "FROM executions", (today + "%",)))
    max_day = int(eng.get("max_buys_per_day", risk.get("max_buys_per_day", 3)))
    if bought_today >= max_day:
        return []
    cooldown = int(risk.get("cooldown_days", 10))
    tdates = market.trading_dates()
    chase = num((params.get("buy") or {}).get("chase_guard_pct", 8.0))

    cand = [s for s in signals if buyable(s, today)]
    cand = [s for s in cand if s["code"] not in open_codes]
    # 冷却过滤
    def cooled(sig) -> bool:
        lc = db.row("SELECT closed_dt FROM positions WHERE code=? AND status='closed' "
                    "ORDER BY closed_dt DESC LIMIT 1", (sig["code"],))
        if not lc or not lc["closed_dt"]:
            return True
        d = lc["closed_dt"][:10]
        if d not in tdates:
            return True
        i_now = len(tdates) - 1
        i_prev = tdates.index(d) if d in tdates else 0
        return (i_now - i_prev) >= cooldown

    cand = [s for s in cand if cooled(s)]
    cand.sort(key=lambda s: -float(s.get("score", 0)))
    outs = []
    for sig in cand:
        if bought_today >= max_day or len(open_codes) >= max_pos:
            break
        q = market.quotes.get(sig["code"])
        if not q:
            continue
        price = num(q.get("price"))
        if price <= 0:
            continue
        ref = num(sig.get("ref_price"))
        if ref > 0 and sig["kind"] == "b2" and price > ref * (1 + chase / 100):
            continue  # 追高保护
        ex = execute_buy(sig["code"], sig.get("name", ""), sig.get("board", ""),
                         price, sig, version, params, cfg)
        if ex:
            outs.append(ex)
            bought_today += 1
            open_codes.add(sig["code"])
    return outs


def manual_close(pos_id: int, cfg: dict, note: str = "") -> dict:
    """人工干预平仓(记录mode=manual与版本号)。同样受T+1约束: 当日买入不可当日卖。"""
    pos = db.row("SELECT * FROM positions WHERE id=? AND status='open'", (pos_id,))
    if not pos:
        return {"ok": False, "error": "持仓不存在或已平仓"}
    if not t1_sellable(pos):
        return {"ok": False, "error": f"T+1规则: {pos['code']} {pos['name']} 于 "
                                      f"{pos['entry_dt'][:10]} 买入, 当日不可卖出, "
                                      f"最早下一交易日方可平仓"}
    version = db.row("SELECT * FROM versions WHERE id=?",
                     (pos["version_id"],)) if pos["version_id"] else None
    if version:
        version["params"] = json.loads(version["params"] or "{}")
    ex = execute_sell(pos, market.quotes.get(pos["code"], {}).get("price", 0) or pos["entry_price"],
                      f"人工干预平仓(用户操作) {note}".strip(), ["manual"], version, cfg,
                      mode="manual", note=note)
    if not ex:
        return {"ok": False, "error": "无法取得现价, 平仓失败"}
    return {"ok": True, **ex}
