# -*- coding: utf-8 -*-
"""多源数据链路自检: 逐源探测 + 多源链路实测(行情/日K), 并打印健康与切换顺序。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.core import providers as P  # noqa: E402

SAMPLES = [("600519", "sh"), ("000001", "sz"), ("920000", "bj"),
           ("900901", "sh"), ("200012", "sz")]

print("== 链路顺序(带*为当前优先源) ==")
for chain in ("quote", "kline", "universe"):
    order = P.chain_order(chain)
    print(f"  {chain:<9}", " -> ".join(("*" + n if i == 0 else n) for i, n in enumerate(order)))

print("\n== 逐源探测 ==")
for name in ("sina_quote", "tencent_spot", "tencent_spot_alt", "eastmoney_spot"):
    fn = P._QUOTE_FETCHERS.get(name)
    try:
        r = fn([SAMPLES[0]]) if fn else {}
        print(f"  {name:<18}", "OK" if r else "不可用/空")
    except Exception as e:  # noqa: BLE001
        print(f"  {name:<18} 失败: {str(e)[:90]}")

print("\n== 多源行情链路实测(沪深/北交所/B股) ==")
quotes = P.fetch_spot_chain(SAMPLES)
print("  返回", len(quotes), "/", len(SAMPLES), "只")
for code, q in sorted(quotes.items()):
    print(f"  {code} {q['name']:<8} 价{q['price']:<9} {q['pct_chg']:+.2f}%  "
          f"时间{q['ts']}  源={q.get('src')}")

print("\n== 日K链路实测(北交所+沪B) ==")
for sym in ("bj920000", "sh900901", "sh600519"):
    rows = P.fetch_kline(sym, "2026-08-01", "2026-09-11", 10)
    last = rows[-1] if rows else None
    print(f"  {sym}: {len(rows)}根", last)

print("\n== 健康统计/当前优先源 ==")
print(" ", P.health())
