# -*- coding: utf-8 -*-
"""SQLite 数据访问层: 建表/连接/基础读写。使用WAL, 每次操作独立短连接+全局写锁, 供多线程调度器与API共用。"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable, Optional

from .util import DB_PATH, get_logger

log = get_logger("db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS universe (
    code TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    board TEXT,
    float_shares REAL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    vol_shares REAL, pct_chg REAL, turnover REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER,
    name TEXT,
    source TEXT,
    params TEXT,
    readable TEXT,
    reason TEXT,
    trigger TEXT,
    created_at TEXT,
    is_active INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, name TEXT, board TEXT,
    source TEXT,             -- auto | manual
    signal TEXT,             -- b1 | b2 | watch
    stage TEXT,              -- 当前阶段描述
    matched TEXT,            -- 命中条件 JSON
    reason TEXT,
    signal_date TEXT,
    ignite_date TEXT,
    pull_days INTEGER,
    ref_price REAL,
    zone_low REAL,
    zone_high REAL,
    signal_ts TEXT,
    status TEXT DEFAULT 'in',  -- in | out
    created_at TEXT,
    updated_at TEXT,
    removed_at TEXT,
    removed_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pool_status ON pool(code, status);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, name TEXT, board TEXT,
    status TEXT DEFAULT 'open',        -- open | closed
    entry_dt TEXT, entry_price REAL, entry_shares INTEGER, entry_amount REAL,
    entry_reason TEXT, signal TEXT,
    stop_price REAL, target_price REAL, partial_done INTEGER DEFAULT 0,
    version_id INTEGER, version_no TEXT,
    peak_high REAL, peak_dt TEXT,
    closed_dt TEXT, exit_reason TEXT,
    realized_pnl REAL DEFAULT 0, realized_pnl_pct REAL DEFAULT 0,
    holding_days INTEGER DEFAULT 0,
    failure TEXT, failure_note TEXT, note TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_code ON positions(code, status);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pos_id INTEGER, code TEXT, name TEXT, side TEXT,   -- buy | sell
    dt TEXT, price REAL, shares INTEGER, amount REAL, fee REAL,
    reason TEXT, tags TEXT,
    version_id INTEGER, version_no TEXT,
    mode TEXT DEFAULT 'system',          -- system | manual
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exe_code ON executions(code, dt);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rtype TEXT,                 -- daily | monthly
    date TEXT,
    title TEXT,
    content TEXT,
    stats TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review ON reviews(rtype, date);
CREATE TABLE IF NOT EXISTS optimizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_version_id INTEGER, new_version_id INTEGER,
    trigger TEXT,               -- rule5_auto | monthly | manual
    reason TEXT,
    stats_before TEXT,
    changed TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    params TEXT, status TEXT DEFAULT 'running',
    progress REAL DEFAULT 0,
    summary TEXT, trades TEXT, equity TEXT, candidates TEXT,
    error TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS engine_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, msg TEXT
);
CREATE TABLE IF NOT EXISTS watch_quotes (
    code TEXT PRIMARY KEY,
    name TEXT, price REAL, pct_chg REAL, change REAL,
    open REAL, high REAL, low REAL, prev_close REAL,
    volume REAL, amount REAL, ts TEXT
);
"""

_WLOCK = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    # 兼容旧库: 补列
    ensure_columns("pool", {
        "signal_date": "TEXT", "ignite_date": "TEXT", "pull_days": "INTEGER",
        "ref_price": "REAL", "zone_low": "REAL", "zone_high": "REAL",
        "signal_ts": "TEXT"})
    log.info("数据库初始化完成: %s", DB_PATH)


def ensure_columns(table: str, cols: dict) -> None:
    """为已存在的表补充缺失列(轻量迁移)。"""
    conn = connect()
    try:
        exist = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, typ in cols.items():
            if name not in exist:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        conn.commit()
    finally:
        conn.close()


def rows(sql: str, args: Iterable = ()) -> list:
    conn = connect()
    try:
        cur = conn.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def row(sql: str, args: Iterable = ()):
    conn = connect()
    try:
        cur = conn.execute(sql, tuple(args))
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def scalar(sql: str, args: Iterable = (), default=None):
    conn = connect()
    try:
        cur = conn.execute(sql, tuple(args))
        r = cur.fetchone()
        return r[0] if r else default
    finally:
        conn.close()


def execute(sql: str, args: Iterable = ()) -> int:
    with _WLOCK:
        conn = connect()
        try:
            cur = conn.execute(sql, tuple(args))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def executemany(sql: str, seq: Iterable) -> None:
    with _WLOCK:
        conn = connect()
        try:
            conn.executemany(sql, [tuple(x) for x in seq])
            conn.commit()
        finally:
            conn.close()


# ---------- meta 便捷 ----------
def meta_get(k: str, default=None):
    return scalar("SELECT v FROM meta WHERE k=?", (k,), default)


def meta_set(k: str, v) -> None:
    execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v) if not isinstance(v, str) else v))


def meta_del(k: str) -> None:
    execute("DELETE FROM meta WHERE k=?", (k,))


def log_event(level: str, msg: str) -> None:
    from .util import now_str
    execute("INSERT INTO engine_log(ts,level,msg) VALUES(?,?,?)", (now_str(), level, msg[:2000]))


def recent_logs(limit: int = 200) -> list:
    return rows("SELECT * FROM engine_log ORDER BY id DESC LIMIT ?", (limit,))
