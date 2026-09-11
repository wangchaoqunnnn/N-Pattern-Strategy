# -*- coding: utf-8 -*-
"""交易系统版本管理。每个版本保存完整参数快照(自包含), 买卖操作记录所用版本号, 用户可任选历史版本执行。"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..core import db
from ..core.util import get_logger, now_str

log = get_logger("strategy")

VERSION_NAMES = {
    "b1": "第一买点: N字回踩企稳低吸",
    "b2": "第二买点: 放量突破回调高点追涨",
}


def default_params_v1() -> dict:
    """初始版参数(依据《N字战法.md》《N字战法量化关系.md》量化而来)。"""
    return {
        "env": {"use_gate": True, "index": "sh000300", "index_20d_min_pct": -3.0,
                "ma20_slope_min": 0.05, "half_mode": True},
        "ignite": {"min_pct": 7.0, "limit_up_pct": 9.7, "vol_ratio": 2.0,
                   "vol_ma_days": 5, "min_turnover": None, "max_turnover": None},
        "pullback": {"min_days": 2, "max_days": 6, "avg_vol_ratio_max": 0.5,
                     "depth_max": 0.5, "low_breach_tol": 0.99,
                     "day_max_abs_pct": 5.0},
        "buy": {"b1": {"enable": True, "stop_pct_max": 2.2, "vol_ratio_max": 0.8,
                       "ma_support": [10, 20], "ma_band_pct": 0.035,
                       "rsi_min": 30, "rsi_max": 70},
                "b2": {"enable": True, "min_pct": 5.0, "vol_mult": 1.2, "rsi_max": 80},
                "chase_guard_pct": 8.0},
        "sell": {"hard_stop_pct": 8.0, "pattern_stop_tol": 0.99, "target_pct": 25.0,
                 "partial_ratio": 0.5, "trail_pct": 15.0, "trail_activate_pct": 5.0,
                 "tail_stop": False, "t1": True},
        "risk": {"max_positions": 5, "position_pct": 0.20, "max_buys_per_day": 3,
                 "cooldown_days": 10},
        "misc": {"min_listed_bars": 60, "rsi_window": 14,
                 "ma20_slope_min_stock": 0.0, "require_close_above_ma20": True},
        "screen": {"boards": ["沪主板", "深主板", "科创板", "创业板", "北交所", "沪B", "深B"],
                   "min_price": 2.0, "max_price": 1000.0},
    }


def readable_rules(params: dict) -> str:
    """把参数渲染为人类可读规则说明(用于交易系统展示)。"""
    e, i, p, b, s, r, m = (params.get("env") or {}, params.get("ignite") or {},
                           params.get("pullback") or {}, params.get("buy") or {},
                           params.get("sell") or {}, params.get("risk") or {},
                           params.get("misc") or {})
    b1 = b.get("b1") or {}
    b2 = b.get("b2") or {}
    lines = []
    lines.append("【一、大盘环境闸门】")
    lines.append(f"  沪深300近20日涨跌幅≥{e.get('index_20d_min_pct', -3)}%且MA20斜率>="
                 f"{e.get('ma20_slope_min', 0.05)}%为『正常』; 环境闸门开启={e.get('use_gate')}"
                 f"(开启时主跌期暂停开新仓; half_mode={e.get('half_mode')} 时震荡期减半仓位)。")
    lines.append("【二、选股(点火阳线)】")
    lines.append(f"  覆盖范围(全部交易所, 无遗漏): {', '.join((params.get('screen') or {}).get('boards') or [])}")
    lines.append(f"  排除ST/退市/次新(上市<{m.get('min_listed_bars', 60)}根K线); 价格区间"
                 f"[{params.get('screen', {}).get('min_price')},{params.get('screen', {}).get('max_price')}]元。")
    lines.append(f"  近10个交易日内出现涨幅≥{i.get('min_pct')}%的光头大阳线/涨停, "
                 f"当日成交量≥前{i.get('vol_ma_days')}日均量的{i.get('vol_ratio')}倍(倍量起涨), 收盘贴近最高。")
    lines.append("【三、回调洗盘确认(2-N天)】")
    lines.append(f"  回调{p.get('min_days')}-{p.get('max_days')}天; 回调最低不破点火阳线最低价"
                 f"(容差{(1 - p.get('low_breach_tol', 0.99)) * 100:.1f}%); "
                 f"回调日均量≤点火量的{p.get('avg_vol_ratio_max') * 100:.0f}%(缩量洗盘); "
                 f"回调深度≤第一波涨幅的{p.get('depth_max') * 100:.0f}%; "
                 f"单日跌幅≤{p.get('day_max_abs_pct')}%(排除放量暴跌型假回调)。")
    lines.append("【四、买点】")
    if b1.get("enable"):
        lines.append(f"  B1 回踩低吸: 回调2天以上后出现缩量止跌(当日|涨跌|≤{b1.get('stop_pct_max')}%、"
                     f"量≤点火量{b1.get('vol_ratio_max') * 100:.0f}%), 价格贴近MA{b1.get('ma_support')}支撑"
                     f"(±{b1.get('ma_band_pct') * 100:.1f}%), RSI∈[{b1.get('rsi_min')},{b1.get('rsi_max')}]。")
    if b2.get("enable"):
        lines.append(f"  B2 突破追涨: 回调结束后当日涨幅≥{b2.get('min_pct')}%、"
                     f"放量≥5日均量{b2.get('vol_mult')}倍并收盘突破回调期间最高点, RSI≤{b2.get('rsi_max')}。")
    lines.append(f"  追高保护: 相对买点信号价涨幅>{b.get('chase_guard_pct')}%时放弃追入。")
    lines.append("【五、卖出】")
    lines.append(f"  T+1硬约束: 当日买入的持仓当日禁止卖出, 最早下一交易日方可卖出。")
    lines.append(f"  ① 硬止损: 收盘/现价≤买入价-{s.get('hard_stop_pct')}%无条件离场; "
                 f"② 形态止损: 跌破N字结构低点(第二拉升阳底/N型回调最低)离场; "
                 f"③ 目标止盈: 盈利+{s.get('target_pct')}%时先了结{s.get('partial_ratio') * 100:.0f}%仓位, "
                 f"剩余部分移动止盈; ④ 移动止盈: 最高点回撤{s.get('trail_pct')}%离场; "
                 f"⑤ 尾盘破昨收离场开关={s.get('tail_stop')}。")
    lines.append("【六、仓位与纪律】")
    lines.append(f"  虚拟资金运作; 单票≤资金{(r.get('position_pct') or 0.2) * 100:.0f}%, "
                 f"同时持仓≤{r.get('max_positions', 5)}只, 每日新开仓≤{r.get('max_buys_per_day', 3)}只, "
                 f"同股冷却{r.get('cooldown_days', 10)}个交易日。")
    return "\n".join(lines)


def create_version(name: str, source: str, reason: str, params: dict,
                   trigger: str = "manual", activate: bool = False,
                   readable: Optional[str] = None) -> int:
    """创建新版本(参数快照)。activate=True 时设为当前执行版本。"""
    max_no = db.scalar("SELECT COALESCE(MAX(version_no),0) FROM versions", (), 0) or 0
    vno = int(max_no) + 1
    if readable is None:
        readable = readable_rules(params)
    vid = db.execute(
        "INSERT INTO versions(version_no,name,source,params,readable,reason,trigger,created_at,is_active) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (vno, name, source, json.dumps(params, ensure_ascii=False), readable,
         reason, trigger, now_str(), 1 if activate else 0))
    if activate:
        db.execute("UPDATE versions SET is_active=0 WHERE id<>?", (vid,))
    db.log_event("INFO", f"创建交易系统版本 v{vno}「{name}」 source={source} trigger={trigger}")
    return vid


def set_active(vid: int) -> bool:
    v = db.row("SELECT * FROM versions WHERE id=?", (vid,))
    if not v:
        return False
    db.execute("UPDATE versions SET is_active=0 WHERE id<>?", (vid,))
    db.execute("UPDATE versions SET is_active=1 WHERE id=?", (vid,))
    db.log_event("INFO", f"切换当前执行版本 -> v{v['version_no']}「{v['name']}」")
    return True


def current_version() -> Optional[dict]:
    v = db.row("SELECT * FROM versions WHERE is_active=1 ORDER BY id DESC LIMIT 1")
    if not v:  # 兜底: 初始版本
        v0 = db.row("SELECT * FROM versions ORDER BY version_no LIMIT 1")
    if not v:
        return None
    v["params"] = json.loads(v["params"] or "{}")
    return v


def get_version(vid: int) -> Optional[dict]:
    v = db.row("SELECT * FROM versions WHERE id=?", (vid,))
    if v:
        v["params"] = json.loads(v["params"] or "{}")
    return v


def list_versions() -> List[dict]:
    vs = db.rows("SELECT * FROM versions ORDER BY version_no DESC")
    for v in vs:
        v["params"] = json.loads(v["params"] or "{}")
    return vs


def ensure_initial_version(cfg: dict) -> None:
    """首次运行时用配置文件写入 v1。"""
    if db.scalar("SELECT COUNT(*) FROM versions", (), 0) or 0:
        return
    params = default_params_v1()
    readable = cfg.get("strategy_v1_readable") or readable_rules(params)
    create_version("N字战法·初始版", "initial",
                   "依据N字战法两份策略文档量化的初始交易系统", params,
                   trigger="initial", activate=True, readable=readable)
    db.log_event("INFO", "初始化交易系统 v1 完成")
