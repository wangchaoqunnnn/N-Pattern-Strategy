# -*- coding: utf-8 -*-
"""监控范围覆盖度自检: 各板块股票数与已缓存K线数。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import db

print("板块            universe  已有K线")
for r in db.rows("SELECT board, COUNT(*) n FROM universe GROUP BY board ORDER BY n DESC"):
    b = r["board"]
    have = db.scalar("SELECT COUNT(DISTINCT b.code) FROM daily_bars b "
                     "JOIN universe u ON u.code=b.code WHERE u.board=?", (b,)) or 0
    print(f"{b:<12} {r['n']:>8} {have:>9}")
print("日K股票总数:", db.scalar("SELECT COUNT(DISTINCT code) FROM daily_bars WHERE code GLOB '[0-9]*'"))
print("同步进度:", db.meta_get("sync_progress"))
