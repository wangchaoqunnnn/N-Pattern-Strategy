# -*- coding: utf-8 -*-
"""离线冒烟测试(不依赖HTTP): 扫描/交易/统计/复盘 引擎单元验证。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import db  # noqa: E402
from server.core.market import market  # noqa: E402
from server.engine import review as reviewmod  # noqa: E402
from server.engine import scan as scanmod  # noqa: E402
from server.engine import stats as statsmod  # noqa: E402
from server.engine import strategy as strat  # noqa: E402

db.init_db()
strat.ensure_initial_version({})
market.load_from_db()  # 本进程内存加载(服务器进程内由bootstrap加载)

idx = market.index_bars.get("sh000300")
print("指数K线:", len(idx or []))
n_codes = db.scalar("SELECT COUNT(DISTINCT code) FROM daily_bars WHERE code GLOB '[0-9]*'")
print("已有K线股票数:", n_codes)

# 1) 形态扫描引擎自测: 取一只数据较全的股票直接评估
row = db.row("SELECT code FROM daily_bars WHERE code GLOB '[0-9]*' "
             "GROUP BY code HAVING COUNT(*)>=100 LIMIT 1")
print("样例股票:", row)
ver = strat.current_version()
params = ver["params"]
if row:
    s = market.series(row["code"])
    sig = scanmod.evaluate_code(row["code"], "样例", "沪主板", params, s)
    print("扫描结果:", sig.to_dict() if sig else "无信号")

# 2) 统计模块自测
acc = statsmod.account()
print("账户统计:", acc["base"], acc["realized"], acc["n_trades"])
print("monthly:", statsmod.monthly_stats())

# 3) 环境闸门
print("env_gate:", scanmod.env_gate(params, market.index_closes("sh000300")))

# 4) 复盘自测(空数据应能运行)
r = reviewmod.run_daily_review("2026-09-04")
print("复盘ok:", r["date"], "stats:", r["stats"]["n"])
print("SMOKE OK")
