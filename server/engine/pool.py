# -*- coding: utf-8 -*-
"""推荐买入池: 自动筛选结果入库 + 用户手动增删。池内股票供交易引擎执行买卖决策。"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..core import db
from ..core.market import classify, market, universe_list
from ..core.util import get_logger, is_st_name, now_str

log = get_logger("pool")


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def stage_of(sig: dict) -> str:
    return {"b1": "回踩企稳·买点B1", "b2": "放量突破·买点B2", "watch": "缩量回调观察中"}.get(
        sig.get("kind"), "观察")


def upsert_auto(sig: dict) -> bool:
    """自动筛选结果写入/更新(仅status=in行)。返回是否新增。"""
    now = now_str()
    exist = db.row("SELECT * FROM pool WHERE code=? AND status='in'", (sig["code"],))
    board = sig.get("board") or classify(sig["code"])
    reason = (f"自动筛选: {stage_of(sig)} "
              f"(点火{sig.get('ignite_date')}涨{sig.get('ignite_pct'):+.2f}%, "
              f"回调{sig.get('pull_days')}天, 信号日{sig.get('date')})")
    if exist:
        # 信号时间=该信号首次检出时刻; 仅当信号类型变化(如watch→b1)时视为新信号而更新
        sig_ts = exist.get("signal_ts") or now
        if str(exist.get("signal") or "") != str(sig.get("kind")):
            sig_ts = now
        db.execute(
            "UPDATE pool SET name=?, board=?, source='auto', signal=?, stage=?, matched=?, "
            "reason=?, signal_date=?, ignite_date=?, pull_days=?, ref_price=?, zone_low=?, "
            "zone_high=?, signal_ts=?, updated_at=?, removed_at=NULL, removed_reason=NULL WHERE id=?",
            (sig.get("name", ""), board, sig.get("kind"), stage_of(sig),
             _json(sig.get("matched") or []), reason, sig.get("date"),
             sig.get("ignite_date"), sig.get("pull_days"), sig.get("ref_price"),
             sig.get("zone_low"), sig.get("zone_high"), sig_ts, now, exist["id"]))
        return False
    db.execute(
        "INSERT INTO pool(code,name,board,source,signal,stage,matched,reason,signal_date,"
        "ignite_date,pull_days,ref_price,zone_low,zone_high,signal_ts,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'in',?,?)",
        (sig["code"], sig.get("name", ""), board, "auto", sig.get("kind"),
         stage_of(sig), _json(sig.get("matched") or []), reason, sig.get("date"),
         sig.get("ignite_date"), sig.get("pull_days"), sig.get("ref_price"),
         sig.get("zone_low"), sig.get("zone_high"), now, now, now))
    db.log_event("INFO", f"自动入池: {sig.get('code')} {sig.get('name')} {stage_of(sig)}")
    return True


def expire_auto(code: str, sig_date: str, reason: str = "形态失效/信号超时") -> None:
    row = db.row("SELECT * FROM pool WHERE code=? AND status='in' AND source='auto'", (code,))
    if not row:
        return
    now = now_str()
    db.execute("UPDATE pool SET status='out', removed_at=?, removed_reason=? WHERE id=?",
               (now, f"{reason} (最后信号日{sig_date})", row["id"]))
    db.log_event("INFO", f"自动出池: {code} {row['name']} -> {reason}")


def manual_add(code: str, reason: str = "用户手动添加") -> dict:
    """手动加池(若已存在自动行则升级为手动, 防止自动过期移除用户关注股)。"""
    now = now_str()
    uni = db.row("SELECT * FROM universe WHERE code=?", (code,))
    if not uni:
        return {"ok": False, "error": "未在股票列表中查到该代码"}
    name = uni["name"]
    board = uni["board"] or classify(code)
    exist = db.row("SELECT * FROM pool WHERE code=? AND status='in'", (code,))
    if exist:
        db.execute("UPDATE pool SET source='manual', reason=?, updated_at=?, "
                   "removed_at=NULL, removed_reason=NULL WHERE id=?",
                   (reason, now, exist["id"]))
        return {"ok": True, "id": exist["id"], "mode": "existing-updated"}
    vid = db.execute(
        "INSERT INTO pool(code,name,board,source,signal,stage,matched,reason,status,created_at,updated_at) "
        "VALUES(?,?,?,?,NULL,'人工关注',?,?, 'in',?,?)",
        (code, name, board, "manual", "[]", reason, now, now))
    db.log_event("INFO", f"手动入池: {code} {name}")
    return {"ok": True, "id": vid, "mode": "new"}


def manual_remove(code: str, reason: str = "用户手动移除") -> bool:
    row = db.row("SELECT * FROM pool WHERE code=? AND status='in'", (code,))
    if not row:
        return False
    db.execute("UPDATE pool SET status='out', removed_at=?, removed_reason=? WHERE id=?",
               (now_str(), reason, row["id"]))
    db.log_event("INFO", f"手动出池: {code} {row['name']} -> {reason}")
    return True


def list_pool() -> List[dict]:
    """池内股票(带最新行情与持仓标记)。"""
    items = db.rows("SELECT * FROM pool WHERE status='in' ORDER BY updated_at DESC")
    quotes = market.quotes
    open_codes = {r["code"] for r in db.rows("SELECT code FROM positions WHERE status='open'")}
    out = []
    for it in items:
        q = quotes.get(it["code"]) or db.row("SELECT * FROM watch_quotes WHERE code=?",
                                             (it["code"],))
        price = (q or {}).get("price", 0) or 0
        pct = (q or {}).get("pct_chg", 0) or 0
        out.append({**it, "matched": json.loads(it.get("matched") or "[]"),
                    "price": round(float(price), 3), "pct_chg": round(float(pct), 3),
                    "change": round(float((q or {}).get("change") or 0), 3),
                    "prev_close": float((q or {}).get("prev_close") or 0),
                    "open_pos": it["code"] in open_codes})
    return out


def active_watch_codes() -> List[str]:
    return [r["code"] for r in db.rows("SELECT code FROM pool WHERE status='in'")]


def recent_signals(limit: int = 100) -> List[dict]:
    return db.rows("SELECT * FROM pool WHERE source='auto' ORDER BY updated_at DESC LIMIT ?",
                   (limit,))


def remove_outdated(scan_results: Dict[str, dict], fresh_codes: set,
                    max_stale_days: int = 4) -> None:
    """自动管理: 扫描新鲜数据下无信号则过期出池。fresh_codes: 当日数据有效(code)。
    scan_results: 本次扫描最新信号(code->sig dict or None)。"""
    rows = db.rows("SELECT * FROM pool WHERE status='in' AND source='auto'")
    for r in rows:
        code = r["code"]
        if code not in fresh_codes:
            continue                      # 数据未就绪, 不误删
        sig = scan_results.get(code)
        if sig is None:
            expire_auto(code, r.get("updated_at", "") or r.get("created_at", ""))
