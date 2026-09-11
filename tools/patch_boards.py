# -*- coding: utf-8 -*-
"""把历史交易系统版本的选股范围补齐为"全部交易所"(含北交所/沪B/深B), 幂等。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import db
from server.core.market import SCREEN_BOARDS
from server.engine import strategy as strat

n = 0
for v in db.rows("SELECT id,params FROM versions"):
    p = json.loads(v["params"] or "{}")
    sc = p.setdefault("screen", {})
    if sc.get("boards") != SCREEN_BOARDS:
        sc["boards"] = list(SCREEN_BOARDS)
        db.execute("UPDATE versions SET params=?, readable=? WHERE id=?",
                   (json.dumps(p, ensure_ascii=False), strat.readable_rules(p), v["id"]))
        n += 1
print(f"已更新 {n} 个版本的选股范围 -> {SCREEN_BOARDS}")
