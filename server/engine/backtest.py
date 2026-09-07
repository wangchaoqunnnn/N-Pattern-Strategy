# -*- coding: utf-8 -*-
"""历史回测引擎(可复盘)。

与实盘同一套 N字扫描/卖出规则(收盘价执行+滑点), 支持选择任意历史版本参数与任意区间;
逐日推进: 卖出现则(止损/分批止盈/移动止盈) → 扫描当日信号(点火/回调/买点) → 按风险参数买入。
交易日取自指数K线, 候选股历史数据不足时自动从数据源补齐(按需拉取)。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional

from ..core import db
from ..core.market import (SCREEN_BOARDS, classify, market, symbol_of,
                           universe_list)
from ..core.providers import fetch_kline
from ..core.util import get_logger, num, now_str, parse_date
from . import scan as scanmod
from .indicators import max_drawdown, mean
from .strategy import current_version, get_version

log = get_logger("backtest")
BT_FEE_BUY = 0.00025
BT_FEE_SELL = 0.00075
LOOKBACK_CAL = 460   # 回测数据向前补的历史日历天数


class _BData:
    """单只股票的整段K线与日期定位。"""

    def __init__(self, code: str, rows: List[dict]):
        self.code = code
        self.dates = [r["date"] for r in rows]
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self.o = [r["open"] for r in rows]
        self.h = [r["high"] for r in rows]
        self.l = [r["low"] for r in rows]
        self.c = [r["close"] for r in rows]
        self.v = [r["vol_shares"] for r in rows]
        self.pct = [r["pct_chg"] or 0.0 for r in rows]

    def series_to(self, date: str) -> Optional[dict]:
        i = self.pos.get(date)
        if i is None or i < 0:
            return None
        return {"dates": self.dates[:i + 1], "open": self.o[:i + 1],
                "high": self.h[:i + 1], "low": self.l[:i + 1],
                "close": self.c[:i + 1], "vol": self.v[:i + 1],
                "pct": self.pct[:i + 1]}

    def bar(self, date: str) -> Optional[dict]:
        i = self.pos.get(date)
        if i is None:
            return None
        return {"date": date, "open": self.o[i], "high": self.h[i], "low": self.l[i],
                "close": self.c[i], "vol": self.v[i], "pct": self.pct[i]}


class _Loader:
    """惰性加载K线(DB优先, 缺失部分从数据源补齐)。"""

    def __init__(self, start_cal: str, end: str, params: dict):
        self.start_cal = start_cal
        self.end = end
        self.misc = params.get("misc") or {}
        self.cache: Dict[str, _BData] = {}
        self.lock = threading.Lock()

    def get(self, code: str) -> Optional[_BData]:
        with self.lock:
            if code in self.cache:
                return self.cache[code]
            rows = db.rows("SELECT date,open,high,low,close,vol_shares,pct_chg FROM daily_bars "
                           "WHERE code=? AND date>=? AND date<=? ORDER BY date",
                           (code, self.start_cal, self.end))
            if len(rows) < 60:
                # 数据不足 → 从数据源补齐历史
                try:
                    fetched = fetch_kline(symbol_of(code), self.start_cal, self.end, 1100, fq="")
                    if fetched:
                        # 落库
                        from ..core.market import _finalize_rows
                        rows2 = _finalize_rows(code, fetched)
                        market.upsert_bars(code, rows2)
                        rows = db.rows("SELECT date,open,high,low,close,vol_shares,pct_chg "
                                       "FROM daily_bars WHERE code=? AND date>=? AND date<=? "
                                       "ORDER BY date", (code, self.start_cal, self.end))
                except Exception as e:  # noqa: BLE001
                    log.warning("backtest fetch %s 失败: %s", code, e)
            if len(rows) < int(self.misc.get("min_listed_bars", 60)):
                self.cache[code] = None
                return None
            bd = _BData(code, rows)
            self.cache[code] = bd
            return bd


def _run(params: dict, job_id: int, progress: callable = None, cancel: callable = None) -> dict:
    vparams = params["version_params"]
    start, end = params["start"], params["end"]
    capital = float(params.get("capital", 1_000_000))
    eng = params.get("engine_cfg") or {}
    slippage = num(eng.get("slippage_pct", 0.1)) / 100.0
    risk = vparams.get("risk") or {}
    sellp = vparams.get("sell") or {}
    hard = num(sellp.get("hard_stop_pct", 8.0))
    target = num(sellp.get("target_pct", 25.0))
    partial_ratio = num(sellp.get("partial_ratio", 0.5))
    trail = num(sellp.get("trail_pct", 15.0))
    trail_act = num(sellp.get("trail_activate_pct", 5.0))
    tol = num(sellp.get("pattern_stop_tol", 0.99))
    max_pos = int(risk.get("max_positions", 5))
    pos_pct = num(risk.get("position_pct", 0.2))
    max_day = int(risk.get("max_buys_per_day", 3))
    chase = num((vparams.get("buy") or {}).get("chase_guard_pct", 8.0))
    pct_threshold = 2.5   # 逐日扫描预筛: |涨跌幅|≥该值的股票才评估形态(点火需≥7%, 突破≥5%)

    # 交易日历(指数日期)
    rows_idx = db.rows("SELECT date,close FROM daily_bars WHERE code='sh000300' AND date>=? "
                       "AND date<=? ORDER BY date", (start, end))
    idx_close = {r["date"]: r["close"] for r in rows_idx}
    idx_dates = sorted(idx_close.keys())
    # 若指数数据不足, 用任意一只股票的日期兜底(不应发生, 指数已同步)
    all_dates = idx_dates
    if len(all_dates) < 5:
        all_dates = sorted({r["date"] for r in db.rows(
            "SELECT date FROM daily_bars WHERE date>=? AND date<=? LIMIT 2000",
            (start, end))})
    if not all_dates:
        return {"error": "区间内无交易日数据, 请先同步历史数据"}

    # 候选股(全市场筛选范围: 沪深主板/科创/创业)
    uni = universe_list()
    screen_boards = (vparams.get("screen") or {}).get("boards") or SCREEN_BOARDS
    codes = [r["code"] for r in uni if (r.get("board") or classify(r["code"])) in screen_boards]
    code_names = {r["code"]: r["name"] for r in uni}
    from datetime import timedelta
    look_start = (parse_date(start) - timedelta(days=LOOKBACK_CAL)).isoformat()
    loader = _Loader(look_start, end, vparams)

    cash = capital
    realized = 0.0
    positions: Dict[str, dict] = {}
    trades: List[dict] = []
    equity_curve: List[dict] = []
    watch: Dict[str, int] = {}          # code -> 最近有效信号日index
    gate_mode = "full"

    def env_for(d: str) -> dict:
        closes = [idx_close[x] for x in idx_dates if x <= d]
        if len(closes) >= 20:
            chg = (closes[-1] - closes[-20]) / closes[-20] * 100
            if chg >= num((vparams.get("env") or {}).get("index_20d_min_pct", -3.0)):
                return {"mode": "full"}
            return {"mode": "off"}
        return {"mode": "full"}

    def _realize(p: dict, fill: float, shares: int, why: str, d: str) -> None:
        """卖出部分/全部并结算; 剩余为0时记为一笔完整交易。"""
        nonlocal realized, cash
        shares = min(shares, p["shares"])
        if shares <= 0:
            return
        entry = p["entry_price"]
        fee = fill * shares * BT_FEE_SELL + (shares / max(p["shares0"], 1)) * p["entry_fee"]
        pnl = (fill - entry) * shares - fee
        p["realized_pnl"] = p.get("realized_pnl", 0.0) + pnl
        realized += pnl
        cash += fill * shares - fee
        p["shares"] -= shares
        if p["shares"] <= 0:
            pnl_pct = p["realized_pnl"] / p["entry_amount"] * 100 if p["entry_amount"] else 0.0
            trades.append({"code": p["code"], "name": p["name"], "board": p["board"],
                           "entry_dt": p["entry_dt"], "exit_dt": d,
                           "entry_price": round(entry, 3), "exit_price": round(fill, 3),
                           "shares": p["shares0"], "pnl": round(p["realized_pnl"], 2),
                           "pnl_pct": round(pnl_pct, 2), "exit_reason": why,
                           "signal": p["signal"],
                           "entry_reason": p.get("entry_reason", "")})

    def do_sells(d: str) -> None:
        for code in list(positions.keys()):
            p = positions[code]
            # A股T+1: 当日买入不可当日卖出(买入在当日收盘执行, 卖出最早次日)
            if (p.get("entry_dt") or "")[:10] >= d:
                continue
            bd = loader.get(code)
            bar = bd.bar(d) if bd else None
            if not bar:
                continue
            hi, lo, cl = num(bar["high"]), num(bar["low"]), num(bar["close"])
            entry = p["entry_price"]
            p["peak"] = max(p.get("peak", entry), hi, cl)
            # 1 止损(硬/形态)
            if lo <= p["stop_price"]:
                fill = min(p["stop_price"], max(lo, num(bar["open"])))
                _realize(p, fill * (1 - slippage), p["shares"],
                         "止损离场(硬止损/形态止损)", d)
                if p["shares"] <= 0:
                    del positions[code]
                continue
            # 2 目标分批止盈
            if not p["partial_done"] and hi >= p["target_price"]:
                n_partial = int(p["shares"] * partial_ratio / 100) * 100
                n_partial = max(100, min(n_partial, p["shares"]))
                if n_partial >= p["shares"]:
                    _realize(p, p["target_price"] * (1 - slippage), p["shares"],
                             f"目标止盈+{target:.0f}%", d)
                    del positions[code]
                    continue
                _realize(p, p["target_price"] * (1 - slippage), n_partial,
                         f"目标止盈+{target:.0f}%分批({partial_ratio * 100:.0f}%仓位)", d)
                p["partial_done"] = True
                p["stop_price"] = entry          # 剩余仓位保本止损
                if p["shares"] <= 0:
                    del positions[code]
                    continue
            # 3 移动止盈
            peak = p.get("peak", entry)
            peak_pct = (peak - entry) / entry * 100 if entry else 0
            if peak_pct >= trail_act and lo <= peak * (1 - trail / 100):
                fill = min(peak * (1 - trail / 100), cl)
                _realize(p, fill * (1 - slippage), p["shares"],
                         f"移动止盈(高点{peak:.2f}回撤{trail:.0f}%)", d)
                if p["shares"] <= 0:
                    del positions[code]

    def do_buys(d: str, signals: List[dict]) -> None:
        nonlocal cash
        if env_for(d)["mode"] == "off":
            return
        if len(positions) >= max_pos:
            return
        cand = [s for s in signals if s["kind"] in ("b1", "b2") and s["date"] == d]
        cand = [s for s in cand if s["code"] not in positions]
        cand.sort(key=lambda s: -float(s.get("score", 0)))
        for sig in cand[:max_day]:
            if len(positions) >= max_pos:
                break
            bd = loader.get(sig["code"])
            bar = bd.bar(d) if bd else None
            if not bar:
                continue
            ref = num(sig.get("ref_price"))
            cl = num(bar["close"])
            if sig["kind"] == "b2" and ref > 0 and cl > ref * (1 + chase / 100):
                continue
            fill = cl * (1 + slippage)
            eq = capital + realized
            amount_allowed = eq * pos_pct
            shares = int(amount_allowed / fill / 100) * 100
            if shares < 100:
                continue
            amount = fill * shares
            if amount > cash:
                shares = int(cash / fill / 100) * 100
                if shares < 100:
                    continue
                amount = fill * shares
            fee = amount * BT_FEE_BUY
            cash -= (amount + fee)
            hard_stop = fill * (1 - hard / 100)
            pattern_stop = num(sig.get("zone_low")) * tol
            stop = max(hard_stop, pattern_stop) if pattern_stop < fill else hard_stop
            positions[sig["code"]] = {
                "code": sig["code"], "name": sig.get("name", ""),
                "board": sig.get("board", ""), "shares0": shares, "shares": shares,
                "entry_price": fill, "entry_amount": amount, "entry_fee": fee,
                "entry_dt": d + " 15:00:00", "stop_price": stop,
                "target_price": fill * (1 + target / 100),
                "partial_done": False, "peak": fill, "signal": sig["kind"],
                "realized_pnl": 0.0, "exit": None,
                "entry_reason": f"回测买入 v{params.get('version_no')}: "
                                f"{'B2放量突破' if sig['kind'] == 'b2' else 'B1回踩企稳'}"
                                f" 信号日{sig['date']}"}

    # ---------- 主循环 ----------
    watch_days = int((vparams.get("pullback") or {}).get("max_days", 6)) + 2
    n_days = len(idx_dates)
    seen_signal_codes = set()
    # 待扫描集合: 前一日大波动/当日watch(点火后窗口内随时可能出买点)
    for i, d in enumerate(idx_dates):
        if cancel and cancel():
            db.execute("UPDATE backtests SET status='cancelled', updated_at=? WHERE id=?",
                       (now_str(), job_id))
            return {"cancelled": True}
        do_sells(d)
        # 清理过期watch
        for code in list(watch.keys()):
            if (i - watch[code]) > watch_days:
                del watch[code]
        # 当日扫描候选 = watch中代码 ∪ 当日|涨跌幅|≥阈值代码(SQL按日取, 避免全市场逐日遍历)
        scan_codes = set(watch.keys())
        if codes:
            qmarks = ",".join("?" * len(codes))
            try:
                big = db.rows(
                    f"SELECT code FROM daily_bars WHERE date=? AND code IN ({qmarks}) "
                    f"AND ABS(pct_chg)>=? AND vol_shares>0", (d, *codes, pct_threshold))
                scan_codes.update(r["code"] for r in big)
            except Exception as e:  # noqa: BLE001
                log.warning("回测预筛查询失败 %s: %s", d, e)
        signals: List[dict] = []
        for code in scan_codes:
            bd = loader.get(code)
            if not bd or d not in bd.pos:
                continue
            s = bd.series_to(d)
            if not s:
                continue
            sig = scanmod.evaluate_code(code, code_names.get(code, code),
                                        classify(code), vparams, s)
            if sig:
                signals.append(sig.to_dict())
                watch[code] = i
                seen_signal_codes.add(code)
        do_buys(d, signals)
        # 日终权益(现金+持仓按收盘)
        mtm = cash
        for code, p in positions.items():
            bd = loader.get(code)
            bar = bd.bar(d) if bd else None
            price = num(bar["close"]) if bar else p["entry_price"]
            mtm += price * p["shares"]
        equity_curve.append({"date": d, "equity": round(mtm, 2)})
        if progress and (i + 1) % max(1, n_days // 20) == 0:
            progress((i + 1) / n_days)
    # 结尾未平仓按最后收盘计未实现
    final_mtm = equity_curve[-1]["equity"] if equity_curve else capital
    unrealized = 0.0
    for p in positions.values():
        bd = loader.get(p["code"])
        last = bd.bar(idx_dates[-1]) if bd else None
        price = num(last["close"]) if last else p["entry_price"]
        unrealized += (price - p["entry_price"]) * p["shares"]
    total_pnl = final_mtm - capital
    realized_disp = round(sum(num(t["pnl"]) for t in trades), 2)
    if abs(realized_disp - round(realized, 2)) > 10.0:
        log.warning("回测记账差异提醒: 流水realized=%.2f vs 平仓合计%.2f (部分未平仓费用所致)",
                    realized, realized_disp)
    unrealized_disp = round(total_pnl - realized_disp, 2)
    eqs = [c["equity"] for c in equity_curve]
    total_pnl = realized + unrealized
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    winrate = len(wins) / n * 100 if n else 0
    avg_w = mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_l = mean([abs(t["pnl_pct"]) for t in losses]) if losses else 0
    rr = avg_w / avg_l if avg_l > 0 else 0
    mdd = max_drawdown(eqs) if eqs else 0
    summary = {
        "start": start, "end": end, "days": n_days,
        "n_trades": n, "wins": len(wins), "losses": len(losses),
        "winrate": round(winrate, 2), "profit_loss_ratio": round(rr, 3),
        "avg_win_pct": round(avg_w, 2), "avg_loss_pct": round(avg_l, 2),
        "realized": realized_disp, "unrealized": unrealized_disp,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / capital * 100, 2),
        "max_drawdown_pct": round(mdd, 2),
        "final_equity": round(final_mtm, 2),
        "capital": capital, "open_at_end": len(positions),
        "version_no": params.get("version_no"),
        "signal_codes": len(seen_signal_codes),
    }
    return {"summary": summary, "trades": trades, "equity": equity_curve}


def start_backtest(start: str, end: str, version_id: Optional[int] = None,
                   capital: float = 1_000_000, params_over: Optional[dict] = None) -> int:
    """创建回测任务并后台运行。返回 job id。"""
    ver = get_version(version_id) if version_id else current_version()
    if not ver:
        raise ValueError("无可用交易系统版本")
    p = params_over or {}
    p.update({"start": start, "end": end, "capital": capital,
              "version_params": ver["params"], "version_id": ver["id"],
              "version_no": ver["version_no"],
              "engine_cfg": {"slippage_pct": 0.1}})
    jid = db.execute(
        "INSERT INTO backtests(params,status,progress,created_at,updated_at) "
        "VALUES(?,?,0,?,?)", (json.dumps(p, ensure_ascii=False), "running", now_str(), now_str()))
    state = {"cancel": False}

    def worker() -> None:
        def prog(x: float) -> None:
            db.execute("UPDATE backtests SET progress=? WHERE id=?", (round(x * 100, 1), jid))

        def cancel() -> bool:
            return state["cancel"]
        try:
            res = _run(p, jid, progress=prog, cancel=cancel)
            db.execute("UPDATE backtests SET status=?, summary=?, trades=?, equity=?, "
                       "progress=100, updated_at=? WHERE id=?",
                       ("done" if not res.get("cancelled") else "cancelled",
                        json.dumps(res.get("summary", {}), ensure_ascii=False),
                        json.dumps(res.get("trades", []), ensure_ascii=False),
                        json.dumps(res.get("equity", []), ensure_ascii=False),
                        now_str(), jid))
        except Exception as e:  # noqa: BLE001
            db.execute("UPDATE backtests SET status='failed', error=?, updated_at=? WHERE id=?",
                       (str(e)[:1000], now_str(), jid))
            log.exception("回测任务失败")
    t = threading.Thread(target=worker, daemon=True, name=f"backtest-{jid}")
    t.start()
    return jid


def cancel_backtest(jid: int) -> bool:
    st = db.row("SELECT status FROM backtests WHERE id=?", (jid,))
    if not st or st["status"] != "running":
        return False
    db.execute("UPDATE backtests SET status='cancelling', updated_at=? WHERE id=?",
               (now_str(), jid))
    return True
