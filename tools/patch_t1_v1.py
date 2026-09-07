# -*- coding: utf-8 -*-
"""将已建 v1 版本快照的 sell.t1 与可读文案补齐(T+1)。幂等。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import db
from server.engine import strategy as strat

v = db.row("SELECT * FROM versions WHERE version_no=1 ORDER BY id LIMIT 1")
if v:
    params = json.loads(v["params"])
    params.setdefault("sell", {})["t1"] = True
    readable = strat.readable_rules(params)
    db.execute("UPDATE versions SET params=?, readable=? WHERE id=?",
               (json.dumps(params, ensure_ascii=False), readable, v["id"]))
    print("v1 已补齐 T+1 参数与规则文案")
else:
    print("未找到 v1")
