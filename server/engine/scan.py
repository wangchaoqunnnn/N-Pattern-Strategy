# -*- coding: utf-8 -*-
"""N字战法形态扫描引擎(可编程量化版)。

完整N字结构: 点火阳线(倍量起涨) → 缩量回调(2-6天, 不破点火低点, 深度≤50%) → 二次放量突破(买点B2)
或 回调末端缩量止跌贴均线(买点B1)。扫描范围: 沪深主板/科创板/创业板(配置可调)。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..core.util import num
from . import indicators as ind

MAX_LOOKBACK = 12        # 点火阳线需出现在最近 N 根内(10交易日窗口+缓冲)


def env_gate(params: dict, index_closes: List[dict]) -> dict:
    """大盘环境闸门。index_closes: 指数收盘序列(升序)。返回 full/half/off 之一及说明。"""
    e = params.get("env") or {}
    use_gate = bool(e.get("use_gate", True))
    closes = [num(x["close"]) for x in index_closes if x.get("close")]
    if not use_gate or len(closes) < 30:
        return {"mode": "full", "reason": "环境闸门未开启或指数数据不足", "index_20d": None,
                "ma20_slope": None}
    last20 = closes[-20:]
    chg20 = (last20[-1] - last20[0]) / last20[0] * 100 if last20[0] else 0.0
    ma20_now = ind.ma(closes, 20)
    ma20_pre = ind.ma(closes, 20, len(closes) - 6)
    slope = (ma20_now - ma20_pre) / ma20_pre * 100 if ma20_pre else 0.0
    need_slope = num(e.get("ma20_slope_min", 0.05))
    need_chg = num(e.get("index_20d_min_pct", -3.0))
    half = bool(e.get("half_mode", True))
    if slope >= need_slope and chg20 >= need_chg:
        mode, reason = "full", f"环境正常: 沪深300近20日{chg20:+.2f}%, MA20斜率{slope:+.3f}%"
    elif half and chg20 >= need_chg:
        mode, reason = "half", f"环境震荡: MA20斜率不足但近20日{chg20:+.2f}%≥{need_chg}% → 半仓运行"
    else:
        mode, reason = "off", f"大盘退潮: 近20日{chg20:+.2f}% < {need_chg}% → 暂停开新仓"
    return {"mode": mode, "reason": reason, "index_20d": round(chg20, 2),
            "ma20_slope": round(slope, 4)}


class Signal:
    def __init__(self, code: str, name: str, board: str, kind: str):
        self.code = code
        self.name = name
        self.board = board
        self.kind = kind            # b1 | b2 | watch
        self.ignite_date = ""
        self.ignite_pct = 0.0
        self.ignite_vol_ratio = 0.0
        self.pull_days = 0
        self.zone_low = 0.0
        self.zone_high = 0.0
        self.ref_price = 0.0
        self.rsi = None
        self.matched: List[dict] = []
        self.score = 0.0
        self.date = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "board": self.board,
                "kind": self.kind, "ignite_date": self.ignite_date,
                "ignite_pct": round(self.ignite_pct, 2),
                "ignite_vol_ratio": round(self.ignite_vol_ratio, 2),
                "pull_days": self.pull_days, "zone_low": self.zone_low,
                "zone_high": self.zone_high, "ref_price": self.ref_price,
                "rsi": round(self.rsi, 1) if self.rsi is not None else None,
                "matched": self.matched, "score": round(self.score, 2),
                "date": self.date}


class _Ctx:
    """单次扫描上下文: 预计算序列与参数, 提供各类信号构建。"""

    def __init__(self, code: str, name: str, board: str, params: dict, s: dict):
        self.code, self.name, self.board = code, name, board
        self.p = params
        self.s = s
        self.dates = s["dates"]
        self.close, self.high, self.low = s["close"], s["high"], s["low"]
        self.vol, self.pct = s["vol"], s["pct"]
        self.n = len(self.close)
        self.ma5 = _ma(self.close, 5)
        self.ma10 = _ma(self.close, 10)
        self.ma20 = _ma(self.close, 20)

    # ---------- 参数 ----------
    def ign(self) -> dict:
        return self.p.get("ignite") or {}

    def pb(self) -> dict:
        return self.p.get("pullback") or {}

    def buy(self) -> dict:
        return self.p.get("buy") or {}

    def misc(self) -> dict:
        return self.p.get("misc") or {}

    # ---------- 点火判定 ----------
    def ignite_ok(self, a: int) -> Optional[float]:
        """返回量比; 不满足点火条件返回 None。"""
        ign = self.ign()
        if self.pct[a] < num(ign.get("min_pct", 7.0)):
            return None
        k = int(ign.get("vol_ma_days", 5))
        if a - k < 0:
            return None
        avg = sum(self.vol[a - k:a]) / k
        vr = self.vol[a] / avg if avg > 0 else 0.0
        if vr < num(ign.get("vol_ratio", 2.0)):
            return None
        rng = self.high[a] - self.low[a]
        if rng <= 0 or (self.high[a] - self.close[a]) / rng > 0.35:
            return None
        return vr

    # ---------- 回调区间校验 ----------
    def zone_low_of(self, a: int, e: int, need_avg: bool,
                   need_depth: bool) -> Optional[float]:
        pb = self.pb()
        seg = list(range(a + 1, e + 1))
        if not seg:
            return None
        zlow = min(self.low[i] for i in seg)
        base = self.low[a]
        if zlow < base * num(pb.get("low_breach_tol", 0.99)) - 1e-9:
            return None
        day_max = num(pb.get("day_max_abs_pct", 5.0))
        if any(abs(self.pct[i]) > day_max for i in seg):
            return None
        if need_avg:
            avg_v = sum(self.vol[i] for i in seg) / len(seg)
            if self.vol[a] > 0 and avg_v > num(pb.get("avg_vol_ratio_max", 0.5)) * self.vol[a]:
                return None
        if need_depth:
            prev = self.close[a - 1] if a > 0 else self.close[a]
            rally = max(self.close[a] - prev, 1e-9)
            if (self.close[a] - zlow) / rally > num(pb.get("depth_max", 0.5)) + 1e-9:
                return None
        return zlow

    def ma20_ok(self, i: int) -> bool:
        misc = self.misc()
        slope_min = num(misc.get("ma20_slope_min_stock", 0.0))
        if self.ma20[i] is None or i < 5 or self.ma20[i - 5] is None:
            return False
        slope = (self.ma20[i] - self.ma20[i - 5]) / self.ma20[i - 5] * 100.0
        if slope < slope_min:
            return False
        if misc.get("require_close_above_ma20", True) and self.close[i] < self.ma20[i]:
            return False
        return True

    # ---------- 三种信号 ----------
    def _base(self, a: int, vr: float, kind: str) -> Signal:
        sig = Signal(self.code, self.name, self.board, kind)
        sig.ignite_date = self.dates[a]
        sig.ignite_pct = self.pct[a]
        sig.ignite_vol_ratio = vr
        sig.date = self.dates[-1]
        return sig

    def build_b2(self, a: int, vr: float) -> Optional[Signal]:
        b2 = self.buy().get("b2") or {}
        today = self.n - 1
        pb_count = today - a - 1
        pb = self.pb()
        if not (int(pb.get("min_days", 2)) <= pb_count <= int(pb.get("max_days", 6))):
            return None
        zlow = self.zone_low_of(a, today - 1, need_avg=True, need_depth=True)
        if zlow is None:
            return None
        zhigh = max(self.high[a + 1:today])
        if self.close[today] <= zhigh:
            return None
        if self.pct[today] < num(b2.get("min_pct", 5.0)):
            return None
        v5 = self.vol[max(0, today - 5):today]
        avg_v5 = sum(v5) / len(v5) if v5 else 0.0
        if avg_v5 <= 0 or self.vol[today] < num(b2.get("vol_mult", 1.2)) * avg_v5:
            return None
        r = ind.rsi(self.close[:today + 1], int(self.misc().get("rsi_window", 14)))
        if r is not None and r > num(b2.get("rsi_max", 80)):
            return None
        if not self.ma20_ok(today):
            return None
        sig = self._base(a, vr, "b2")
        sig.pull_days = pb_count
        sig.zone_low = zlow
        sig.zone_high = zhigh
        sig.ref_price = self.close[today]
        sig.rsi = r
        sig.matched.append({"name": "点火阳线(倍量起涨)", "ok": True,
                            "detail": f"{sig.ignite_date} 涨幅{self.pct[a]:+.2f}% 量比{vr:.2f}"})
        sig.matched.append({"name": "缩量回调", "ok": True,
                            "detail": f"回调{pb_count}天, 区间均量/点火量="
                                      f"{sum(self.vol[a + 1:today]) / (today - a - 1) / self.vol[a] if (today - a - 1) else 0:.2f}"})
        sig.matched.append({"name": "不破点火低点", "ok": True,
                            "detail": f"回调最低{zlow:.2f} ≥ 点火低点{self.low[a]:.2f}"})
        sig.matched.append({"name": "二次放量突破", "ok": True,
                            "detail": f"今涨{self.pct[today]:+.2f}% 突破{zhigh:.2f} 量比5日均{self.vol[today] / avg_v5:.2f}"})
        sig.matched.append({"name": "MA20向上", "ok": True, "detail": "均线结构健康"})
        sig.score = self.pct[today] + self.vol[today] / avg_v5 * 3 + (7 - pb_count) * 0.5
        return sig

    def build_b1_watch(self, a: int, vr: float) -> Optional[Signal]:
        """回调末端: 若今日止跌贴均线→B1, 否则结构未破则返回watch。"""
        b1 = self.buy().get("b1") or {}
        pb = self.pb()
        today = self.n - 1
        pb_count = today - a
        if not (int(pb.get("min_days", 2)) <= pb_count <= int(pb.get("max_days", 6))):
            return None
        zlow = self.zone_low_of(a, today, need_avg=True, need_depth=True)
        if zlow is None:
            return None
        r = ind.rsi(self.close[:today + 1], int(self.misc().get("rsi_window", 14)))

        def conds(sig: Signal):
            sig.matched.append({"name": "点火阳线(倍量起涨)", "ok": True,
                                "detail": f"{sig.ignite_date} 涨幅{self.pct[a]:+.2f}% 量比{vr:.2f}"})
            sig.matched.append({"name": "回调天数2-6", "ok": True,
                                "detail": f"点火后第{pb_count}天"})
            sig.matched.append({"name": "不破点火低点", "ok": True,
                                "detail": f"区间最低{zlow:.2f} ≥ 点火低点{self.low[a]:.2f}"})
            sig.matched.append({"name": "缩量", "ok": True,
                                "detail": f"区间均量/点火量="
                                          f"{sum(self.vol[a + 1:today + 1]) / pb_count / self.vol[a]:.2f}"})

        stop_ok = abs(self.pct[today]) <= num(b1.get("stop_pct_max", 2.2))
        shrink_ok = self.vol[today] <= num(b1.get("vol_ratio_max", 0.8)) * self.vol[a] if self.vol[a] > 0 else False
        sup_ok, sup_desc = False, ""
        for mn in b1.get("ma_support", [10, 20]):
            mv = self.ma10[today] if mn == 10 else self.ma20[today]
            if mv:
                band = num(b1.get("ma_band_pct", 0.035))
                if abs(self.close[today] - mv) / mv <= band:
                    sup_ok, sup_desc = True, f"贴近MA{mn}支撑({mv:.2f})"
        rsi_ok = r is not None and num(b1.get("rsi_min", 30)) <= r <= num(b1.get("rsi_max", 70))
        ma_ok = self.ma20_ok(today)

        if stop_ok and shrink_ok and sup_ok and rsi_ok and ma_ok and bool(b1.get("enable", True)):
            sig = self._base(a, vr, "b1")
            sig.pull_days = pb_count
            sig.zone_low = zlow
            sig.zone_high = max(self.high[a + 1:today + 1])
            sig.ref_price = self.close[today]
            sig.rsi = r
            conds(sig)
            sig.matched.append({"name": "止跌企稳", "ok": True,
                                "detail": f"今日{self.pct[today]:+.2f}% {sup_desc}"})
            sig.matched.append({"name": "量能/RSI", "ok": True,
                                "detail": f"RSI={r:.1f}"})
            sig.score = 12 - pb_count + (0.5 - min(1.0, (self.close[a] - zlow) / max(self.close[a] - (self.close[a - 1] if a else self.close[a]), 1e-9))) * 4
            return sig
        # 观察状态
        sig = self._base(a, vr, "watch")
        sig.pull_days = pb_count
        sig.zone_low = zlow
        sig.zone_high = max(self.high[a + 1:today + 1])
        sig.ref_price = self.close[today]
        sig.rsi = r
        conds(sig)
        misses = []
        if not stop_ok:
            misses.append(f"今日未止跌({self.pct[today]:+.2f}%)")
        if not shrink_ok:
            misses.append("量能未缩")
        if not sup_ok:
            misses.append("偏离均线支撑")
        if not rsi_ok:
            misses.append(f"RSI={r:.1f}超区间")
        if not ma_ok:
            misses.append("MA20结构未达标")
        sig.matched.append({"name": "买点未触发", "ok": False,
                            "detail": "等待: " + ("、".join(misses) if misses else "结构完好")})
        sig.score = 4.0
        return sig


def _ma(vals, n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    s = 0.0
    for i, c in enumerate(vals):
        s += c
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def evaluate_code(code: str, name: str, board: str, params: dict, s: dict) -> Optional[Signal]:
    """对单只股票求最近有效信号。s: series dict(升序数组, 含当日bar)。优先级 B2 > B1 > watch, 同类取最新点火日。"""
    if len(s["close"]) < int((params.get("misc") or {}).get("min_listed_bars", 60)):
        return None
    ctx = _Ctx(code, name, board, params, s)
    today = ctx.n - 1
    best: Dict[str, Optional[Signal]] = {"b2": None, "b1": None, "watch": None}
    for a in range(today - 2, max(today - MAX_LOOKBACK - 1, 0), -1):
        vr = ctx.ignite_ok(a)
        if vr is None:
            continue
        if best["b2"] is None:
            cand = ctx.build_b2(a, vr)
            if cand:
                best["b2"] = cand
                break                    # 已有最新B2, 优先级最高, 无需再看更早点火
        if best["b1"] is None and best["watch"] is None:
            cand = ctx.build_b1_watch(a, vr)
            if cand and cand.kind == "b1" and best["b1"] is None:
                best["b1"] = cand
            elif cand and cand.kind == "watch" and best["b1"] is None:
                best["watch"] = cand
    return best["b2"] or best["b1"] or best["watch"]


def append_live(s: dict, bar: dict) -> dict:
    """追加/更新当日实时bar(盘中扫描用), 返回新series。"""
    out = {k: list(v) for k, v in s.items()}
    if not bar:
        return out
    if not out["dates"]:
        return out
    if out["dates"][-1] == bar["date"]:
        i = len(out["dates"]) - 1
        out["high"][i] = max(out["high"][i], bar["high"], bar["close"])
        if bar["low"]:
            out["low"][i] = min(out["low"][i], bar["low"])
        out["close"][i] = bar["close"]
        out["vol"][i] = bar["vol_shares"] or out["vol"][i]
        prev = out["close"][i - 1] if i > 0 else out["open"][i]
        out["pct"][i] = (bar["close"] - prev) / prev * 100 if prev else 0.0
    else:
        out["dates"].append(bar["date"])
        out["open"].append(bar["open"])
        out["high"].append(max(bar["high"], bar["close"]))
        out["low"].append(min(bar["low"], bar["close"]) if bar["low"] else bar["close"])
        out["close"].append(bar["close"])
        out["vol"].append(bar["vol_shares"] or 0.0)
        prev = out["close"][-2]
        out["pct"].append((bar["close"] - prev) / prev * 100 if prev else 0.0)
    return out
