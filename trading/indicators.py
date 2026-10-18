"""Vector-ish indicator helpers operating on plain python lists (no numpy)."""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

Series = List[float]


def sma(values: Series, period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def ema(values: Series, period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if not values:
        return out
    k = 2.0 / (period + 1.0)
    prev = values[0]
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        if i >= period - 1:
            out[i] = prev
    return out


def true_range(high: Series, low: Series, close: Series) -> Series:
    out: Series = [0.0] * len(close)
    for i in range(len(close)):
        if i == 0:
            out[i] = high[i] - low[i]
        else:
            out[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return out


def atr(high: Series, low: Series, close: Series, period: int = 14) -> List[Optional[float]]:
    tr = true_range(high, low, close)
    out: List[Optional[float]] = [None] * len(tr)
    if len(tr) < period:
        return out
    run = sum(tr[:period]) / period
    out[period - 1] = run
    for i in range(period, len(tr)):
        run = (run * (period - 1) + tr[i]) / period
        out[i] = run
    return out


def rsi(values: Series, period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def stdev(values: Series) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


def swing_points(high: Series, low: Series, left: int = 3, right: int = 3) -> Tuple[List[int], List[int]]:
    """Return indices of confirmed swing highs and swing lows."""
    highs: List[int] = []
    lows: List[int] = []
    n = len(high)
    for i in range(left, n - right):
        window_h = high[i - left : i + right + 1]
        window_l = low[i - left : i + right + 1]
        if high[i] >= max(window_h):
            highs.append(i)
        if low[i] <= min(window_l):
            lows.append(i)
    return highs, lows


def slope(values: List[Optional[float]], idx: int, lookback: int = 10) -> float:
    j = idx - lookback
    if j < 0 or values[idx] is None or values[j] is None:
        return 0.0
    return (values[idx] - values[j]) / max(abs(values[j]), 1e-9)


def zscore(values: Series, idx: int, lookback: int = 50) -> float:
    start = max(0, idx - lookback + 1)
    window = values[start : idx + 1]
    sd = stdev(window)
    if sd == 0:
        return 0.0
    mean = sum(window) / len(window)
    return (values[idx] - mean) / sd
