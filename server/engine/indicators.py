# -*- coding: utf-8 -*-
"""技术指标工具(纯Python, 小样本向量)。"""
from __future__ import annotations

from typing import List, Optional


def ma(vals: List[float], n: int, idx: Optional[int] = None) -> Optional[float]:
    if idx is None:
        idx = len(vals) - 1
    if idx < n - 1 or n <= 0:
        return None
    seg = vals[idx - n + 1: idx + 1]
    return sum(seg) / n


def ma_slope_pct(vals: List[float], n: int, lookback: int = 5) -> Optional[float]:
    """MA(n) 在最近 lookback 根内的斜率(%). None表示数据不足。"""
    if len(vals) < n + lookback:
        return None
    m0 = ma(vals, n, len(vals) - lookback)
    m1 = ma(vals, n, len(vals) - 1)
    if not m0 or not m1 or m0 == 0:
        return None
    return (m1 - m0) / m0 * 100.0


def rsi(closes: List[float], n: int = 14) -> Optional[float]:
    """Wilder RSI。"""
    if len(closes) < n + 1:
        return None
    gains = []
    losses = []
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / n
    al = sum(losses) / n
    if ag + al == 0:
        return 50.0
    return 100.0 - 100.0 / (1.0 + ag / al) if al > 0 else 100.0


def mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def max_drawdown(equity: List[float]) -> float:
    """最大回撤(%)."""
    peak = -1e18
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak)
    return mdd * 100.0
