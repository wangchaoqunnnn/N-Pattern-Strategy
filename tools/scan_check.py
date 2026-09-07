# -*- coding: utf-8 -*-
"""扫描引擎全量遍历自检 + 合成N字形态单元验证。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import db  # noqa: E402
from server.core.market import market  # noqa: E402
from server.engine import scan as scanmod  # noqa: E402
from server.engine import strategy as strat  # noqa: E402

db.init_db()
strat.ensure_initial_version({})
market.load_from_db()
params = strat.current_version()["params"]

# ---- 遍历实盘缓存扫描(以最后根bar为当日) ----
kinds = {"b1": 0, "b2": 0, "watch": 0}
errors = 0
examples = {}
for code in list(market.bars.keys()):
    if len(code) != 6:
        continue
    s = market.series(code)
    if not s or len(s["close"]) < 70:
        continue
    try:
        sig = scanmod.evaluate_code(code, code, "板", params, s)
    except Exception as e:  # noqa: BLE001
        errors += 1
        if errors <= 3:
            print("ERR", code, e)
        continue
    if sig:
        kinds[sig.kind] = kinds.get(sig.kind, 0) + 1
        examples.setdefault(sig.kind, sig.to_dict())
print("遍历结果:", kinds, "异常:", errors)
for k, ex in examples.items():
    print("样例", k, {kk: ex[kk] for kk in ("ignite_date", "ignite_pct", "ignite_vol_ratio",
                                             "pull_days", "zone_low", "ref_price", "score")})

# ---- 合成N字验证 B2 ----
base = 10.0
dates, o, h, l, c, v, pct = [], [], [], [], [], [], []
px = base
for i in range(86):
    prev = px
    px = prev * 1.004 if i % 3 == 0 else prev * 0.998
    dates.append(f"2025-01-{i % 28 + 1:02d}")
    o.append(prev); h.append(max(prev, px) * 1.002); l.append(min(prev, px) * 0.998)
    c.append(px); v.append(100000 + i); pct.append((px / prev - 1) * 100)
prev = px
px = prev * 1.098
dates.append("2025-03-01"); o.append(prev); h.append(px * 1.005); l.append(prev * 0.995)
c.append(px); v.append(5000000); pct.append(9.8)
for k in range(3):
    prev = px
    px = px * (1 - 0.008 - 0.003 * k)
    dates.append(f"2025-03-0{2 + k}")
    o.append(prev); h.append(max(prev, px) * 1.002); l.append(min(prev, px) * 0.997)
    c.append(px); v.append(200000); pct.append((px / prev - 1) * 100)
prev = px
px = prev * 1.06
dates.append("2025-03-06"); o.append(prev); h.append(px * 1.004); l.append(prev * 0.999)
c.append(px); v.append(2500000); pct.append(6.0)
s = {"dates": dates, "open": o, "high": h, "low": l, "close": c, "vol": v, "pct": pct}
sig = scanmod.evaluate_code("999999", "合成测试", "沪主板", params, s)
print("合成N字(B2预期):", sig.to_dict() if sig else "无信号")
assert sig and sig.kind == "b2", "B2合成形态未识别!"
print("B2合成验证通过")
print("DONE")
