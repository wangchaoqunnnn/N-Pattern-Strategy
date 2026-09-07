# -*- coding: utf-8 -*-
"""交易系统自优化引擎。

触发条件:
- rule5_auto: 连续5个交易日"近5日窗口"统计均满足 盈亏比<0.6 且 成功率<40%(样本≥3笔) → 系统自行优化并切换;
- monthly: 月度复盘判定需要优化 → 自动优化;
- manual: 用户手动发起。
优化方向由复盘失败归因(tally)驱动, 规则表见 _PLAN; 每次优化生成新版本并记录 diff。
"""
from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional

from ..core import db
from ..core.util import get_logger, now_str
from . import stats as statsmod
from . import strategy as strat

log = get_logger("optimizer")

RULE5_WINRATE_BAD = 40.0
RULE5_RRR_BAD = 0.6
RULE5_MIN_TRADES = 3
RULE5_DAYS = 5


def rule5_window_state() -> dict:
    """近5个交易日, 每个交易日窗口(含当日往回5个交易日)的统计。返回是否"连续5日不达标"。"""
    wins = statsmod.session_windows(RULE5_DAYS)
    if len(wins) < RULE5_DAYS:
        return {"ok": False, "trigger": False, "reason": f"交易日样本<{RULE5_DAYS}, 无法评估",
                "windows": wins, "evaluated": []}
    evaluated = []
    for w in wins:
        bad = (w["n"] >= RULE5_MIN_TRADES and
               w["profit_loss_ratio"] < RULE5_RRR_BAD and
               w["winrate"] < RULE5_WINRATE_BAD)
        evaluated.append({"window_end": w["window_end"], "n": w["n"],
                          "winrate": w["winrate"], "rrr": w["profit_loss_ratio"], "bad": bad})
    all_bad = all(x["bad"] for x in evaluated)
    reason = ("连续5个交易日 近5日窗口均满足 盈亏比<0.6 且 成功率<40% → 触发系统自我优化"
              if all_bad else "近5日窗口未全部满足触发条件")
    return {"ok": True, "trigger": all_bad, "reason": reason,
            "windows": wins, "evaluated": evaluated}


_PLAN = []  # 由 _mutate 函数实现规则表


def _mutate(p: dict, dominant: str, share: float) -> List[str]:
    """按失败归因主导方向调整参数, 返回变更说明列表。"""
    changed: List[str] = []
    env = p.setdefault("env", {})
    ig = p.setdefault("ignite", {})
    pb = p.setdefault("pullback", {})
    buy = p.setdefault("buy", {})
    b1 = buy.setdefault("b1", {})
    b2 = buy.setdefault("b2", {})
    sell = p.setdefault("sell", {})
    risk = p.setdefault("risk", {})
    misc = p.setdefault("misc", {})

    def setv(d: dict, k: str, v, label: str) -> None:
        old = d.get(k)
        if old is None or abs(float(old) - float(v)) > 1e-9:
            changed.append(f"{label}: {old} → {v}")
            d[k] = v

    if dominant == "fake_breakout" and share >= 0.4:
        # 假突破多 → 提高突破确认门槛
        setv(b2, "vol_mult", min(2.0, float(b2.get("vol_mult", 1.2)) + 0.3),
             "突破量能倍数(vol_mult)↑")
        setv(b2, "min_pct", min(8.0, float(b2.get("min_pct", 5.0)) + 0.5),
             "突破日最低涨幅(min_pct)↑")
        setv(buy, "chase_guard_pct", min(6.0, float(buy.get("chase_guard_pct", 8.0)) - 0.5),
             "追高保护幅度(chase_guard_pct)↓")
    elif dominant in ("early_entry", "pattern_break") and share >= 0.4:
        # 买点过早/形态破位 → 回踩买点更谨慎
        if b1.get("enable", True):
            nd = min(int(pb.get("max_days", 6)) - 1,
                     max(int(pb.get("min_days", 2)) + 1, 3))
            setv(pb, "min_days", nd, "回调最短天数(min_days)↑")
            setv(b1, "stop_pct_max", min(1.8, float(b1.get("stop_pct_max", 2.2)) - 0.2),
                 "止跌判定幅度(stop_pct_max)↓")
            setv(b1, "ma_band_pct", max(0.02, float(b1.get("ma_band_pct", 0.035)) - 0.005),
                 "均线支撑带宽(ma_band_pct)↓")
        setv(misc, "require_close_above_ma20", True, "强制收盘站上MA20")
        setv(misc, "ma20_slope_min_stock",
             min(0.2, float(misc.get("ma20_slope_min_stock", 0.0)) + 0.05),
             "个股MA20斜率要求↑")
    elif dominant == "bad_environment" and share >= 0.3:
        # 大盘拖累 → 收紧环境闸门
        setv(env, "index_20d_min_pct", min(0.0, float(env.get("index_20d_min_pct", -3.0)) + 1.0),
             "环境闸门: 指数近20日下限↑(更严)")
        setv(env, "ma20_slope_min", min(0.3, float(env.get("ma20_slope_min", 0.05)) + 0.05),
             "环境闸门: 指数MA20斜率要求↑")
    elif dominant == "stop_too_tight" and share >= 0.3:
        setv(sell, "hard_stop_pct", max(9.0, float(sell.get("hard_stop_pct", 8.0)) + 1.0),
             "硬止损放宽(避免被洗)→保留形态止损")
    else:
        # 归因不明显/其他 → 温和收紧风险与止损
        setv(risk, "position_pct", max(0.10, float(risk.get("position_pct", 0.2)) - 0.02),
             "单票仓位(position_pct)↓")
        setv(sell, "hard_stop_pct", max(5.0, float(sell.get("hard_stop_pct", 8.0)) - 0.5),
             "硬止损收紧(hard_stop_pct)↓")
        setv(b1, "stop_pct_max", min(2.0, float(b1.get("stop_pct_max", 2.2)) - 0.1),
             "止跌判定幅度(stop_pct_max)↓")
    # 一致性约束
    md = int(pb.get("min_days", 2)); xd = int(pb.get("max_days", 6))
    if md >= xd:
        pb["max_days"] = md + 1
        changed.append(f"回调窗口一致性: max_days → {md + 1}")
    if not changed:
        changed.append("参数已达边界, 本次无有效调整(触发记录但不产生新版本)")
    return changed


def auto_optimize(trigger: str = "rule5_auto", reason: str = "", force: bool = False) -> dict:
    """执行一次自优化: 基于当前执行版本 + 失败归因生成新版本并切换。"""
    cur = strat.current_version()
    if not cur:
        return {"ok": False, "error": "无当前交易系统版本"}
    last_auto = db.meta_get("auto_opt_ts") or ""
    today = now_str()[:10]
    if trigger != "manual" and not force:
        if last_auto[:10] == today:
            return {"ok": False, "error": "今日已执行过自动优化, 冷却中"}
    failures = _tally_dominant()
    dominant, share = failures.get("dominant"), failures.get("share", 0)
    base = copy.deepcopy(cur["params"])
    changed = _mutate(base, dominant or "other", share or 0)
    if not changed or changed == ["参数已达边界, 本次无有效调整(触发记录但不产生新版本)"]:
        db.log_event("WARN", f"自优化触发({trigger})但无有效参数调整")
        return {"ok": False, "error": "参数已达边界, 无有效调整"}
    name = f"N字战法·{trigger}优化{vint(cur['version_no']) + 1}"
    extra = reason or (f"连续5日窗口不达标(盈亏比<0.6且成功率<40%) 自动优化" if trigger == "rule5_auto"
                       else f"{trigger} 触发自优化")
    vid = strat.create_version(name, "auto", extra + f"; 归因={dominant or '其他'}占比{share:.0%}; "
                                f"调整: " + "; ".join(changed),
                               base, trigger=trigger, activate=True)
    db.execute(
        "INSERT INTO optimizations(old_version_id,new_version_id,trigger,reason,stats_before,changed,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (cur["id"], vid, trigger,
         f"{extra}; 归因{dominant or '其他'}({share:.0%}); 变更: " + "; ".join(changed),
         json.dumps({"n": statsmod.stats_of(statsmod.closed_list())["n"],
                     "failures": failures}, ensure_ascii=False),
         "; ".join(changed), now_str()))
    db.meta_set("auto_opt_ts", now_str())
    newv = strat.get_version(vid)
    db.log_event("INFO",
                 f"系统自我优化完成: v{cur['version_no']} → v{newv['version_no'] if newv else '?'} "
                 f"「{name}」已激活; 变更: " + "; ".join(changed))
    return {"ok": True, "old_version": cur["version_no"],
            "new_version_id": vid, "new_version_no": newv["version_no"] if newv else None,
            "changed": changed, "dominant": dominant}


def vint(x) -> int:
    try:
        return int(x)
    except Exception:
        return 0


def _tally_dominant() -> dict:
    from . import review
    tally = review.failure_tally(30)
    if not tally:
        return {"tally": {}, "dominant": None, "share": 0}
    dominant = max(tally, key=tally.get)
    total = sum(tally.values())
    return {"tally": tally, "dominant": dominant, "share": tally[dominant] / total if total else 0}


def check_rule5_and_optimize() -> dict:
    """收盘后规则5检查: 连续5日不达标→自行优化并切换。"""
    st = rule5_window_state()
    if st["trigger"]:
        return auto_optimize("rule5_auto")
    return {"ok": True, "trigger": False, "reason": st["reason"]}


def monthly_optimize(ym: str = "") -> dict:
    """月度复盘后判定需要→自动优化。"""
    ym = ym or now_str()[:7]
    from . import review
    res = review.run_monthly_review(ym)
    if res["need_optimize"]:
        return auto_optimize("monthly",
                             reason=f"{ym} 月度复盘判定需要优化(成功率{res['stats']['winrate']:.1f}% "
                                    f"盈亏比{res['stats']['profit_loss_ratio']:.2f})")
    return {"ok": True, "trigger": False, "judge": res["judge"]}


def list_optimizations() -> List[dict]:
    return db.rows("SELECT * FROM optimizations ORDER BY id DESC LIMIT 100")
