# -*- coding: utf-8 -*-
"""打印参与自动筛选的股票范围(按板块), 确认无遗漏。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core.market import auto_screen_candidates, SCREEN_BOARDS

cands = auto_screen_candidates()
c = Counter(x["board"] for x in cands)
print("自动筛选范围:", SCREEN_BOARDS)
print("候选股票总数:", len(cands))
for b, n in c.most_common():
    print(f"  {b}: {n}")
print("北交所样例:", [x["code"] + " " + x["name"] for x in cands if x["board"] == "北交所"][:5])
