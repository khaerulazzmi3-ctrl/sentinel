"""Trading strategies.

Every strategy implements ``prepare(ctx)`` once per data set and ``signal(ctx, i)``
per bar. A signal is a complete trade plan (entry, stop, target, reason) so the
risk engine never has to guess intent.

Instruments covered: FX majors and metals/energy (see config.json).
"""
from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.config import INSTRUMENTS, Instrument
from trading import indicators as ta
from trading.datafeed import Bar


@dataclass
class Signal:
    side: str                    # "buy" | "sell"
    sl: float
    tp: float
    reason: str
    tag: str = ""
    strength: float = 1.0
    meta: Dict[str, float] = field(default_factory=dict)

    def rr(self, entry: float) -> float:
        risk = abs(entry - self.sl)
        if risk <= 0:
            return 0.0
        return abs(self.tp - entry) / risk


@dataclass
class Ctx:
    symbol: str
    inst: Instrument
    bars: List[Bar]
    o: List[float]
    h: List[float]
    l: List[float]
    c: List[float]
    v: List[float]
    atr: List[Optional[float]]
    ema20: List[Optional[float]]
    ema50: List[Optional[float]]
    ema200: List[Optional[float]]
    rsi: List[Optional[float]]
    swing_h: List[int]
    swing_l: List[int]
    last_sh: List[Optional[float]]
    last_sl: List[Optional[float]]
    pdh: List[Optional[float]]
    pdl: List[Optional[float]]
    zones: List[dict]
    levels: List[float]
    macro: Optional[Dict[str, List[float]]] = None

    def atr_at(self, i: int, fallback: float = 0.0) -> float:
        a = self.atr[i]
        if a is None:
            return fallback or (self.c[i] * 0.001)
        return a

    def warm(self, i: int) -> bool:
        return i > 60 and self.atr[i] is not None and self.ema50[i] is not None


def _cluster(levels: List[float], tol: float) -> List[float]:
    if not levels:
        return []
    levels = sorted(levels)
    out = [levels[0]]
    bucket = [levels[0]]
    for lv in levels[1:]:
        if abs(lv - bucket[-1]) <= tol:
            bucket.append(lv)
        else:
            out.append(sum(bucket) / len(bucket))
            bucket = [lv]
    out.append(sum(bucket) / len(bucket))
    return out


def build_ctx(symbol: str, bars: List[Bar], macro: Optional[Dict[str, List[float]]] = None) -> Ctx:
    inst = INSTRUMENTS[symbol]
    o = [b.o for b in bars]
    h = [b.h for b in bars]
    l = [b.l for b in bars]
    c = [b.c for b in bars]
    v = [b.v for b in bars]
    n = len(bars)
    atr = ta.atr(h, l, c, 14)
    ema20 = ta.ema(c, 20)
    ema50 = ta.ema(c, 50)
    ema200 = ta.ema(c, 200)
    rsi = ta.rsi(c, 14)
    sh, sl = ta.swing_points(h, l, 3, 3)

    last_sh: List[Optional[float]] = [None] * n
    last_sl: List[Optional[float]] = [None] * n
    cur_h: Optional[float] = None
    cur_l: Optional[float] = None
    ptr_h = ptr_l = 0
    for i in range(n):
        while ptr_h < len(sh) and sh[ptr_h] + 3 <= i:
            cur_h = h[sh[ptr_h]]
            ptr_h += 1
        while ptr_l < len(sl) and sl[ptr_l] + 3 <= i:
            cur_l = l[sl[ptr_l]]
            ptr_l += 1
        last_sh[i] = cur_h
        last_sl[i] = cur_l

    # previous-day high / low (session liquidity)
    pdh: List[Optional[float]] = [None] * n
    pdl: List[Optional[float]] = [None] * n
    day_stats: Dict[int, Tuple[float, float]] = {}
    prev_day: Optional[Tuple[float, float]] = None
    cur_day = None
    d_hi = d_lo = 0.0
    for i, b in enumerate(bars):
        d = time.gmtime(b.t).tm_yday
        if d != cur_day:
            if cur_day is not None:
                prev_day = (d_hi, d_lo)
            cur_day = d
            d_hi, d_lo = b.h, b.l
        d_hi = max(d_hi, b.h)
        d_lo = min(d_lo, b.l)
        day_stats[d] = (d_hi, d_lo)
        if prev_day:
            pdh[i], pdl[i] = prev_day

    # SMC: break of structure -> order block / fair value gap zones
    zones: List[dict] = []
    for i in range(5, n):
        a = atr[i] or (c[i] * 0.001)
        prev_h, prev_l = last_sh[i - 1], last_sl[i - 1]
        if prev_h is not None and c[i] > prev_h and c[i - 1] <= prev_h:
            lo, hi = _order_block(bars, i, "bull")
            zones.append({"side": "buy", "lo": lo, "hi": hi, "born": i, "expiry": i + 28, "kind": "OB+BOS"})
        if prev_l is not None and c[i] < prev_l and c[i - 1] >= prev_l:
            lo, hi = _order_block(bars, i, "bear")
            zones.append({"side": "sell", "lo": lo, "hi": hi, "born": i, "expiry": i + 28, "kind": "OB+BOS"})
        # fair value gaps (3-bar imbalance)
        if i >= 2:
            if l[i] > h[i - 2] and (l[i] - h[i - 2]) > a * 0.25:
                zones.append({"side": "buy", "lo": h[i - 2], "hi": l[i], "born": i, "expiry": i + 18, "kind": "FVG"})
            if h[i] < l[i - 2] and (l[i - 2] - h[i]) > a * 0.25:
                zones.append({"side": "sell", "lo": h[i], "hi": l[i - 2], "born": i, "expiry": i + 18, "kind": "FVG"})

    # horizontal S/R levels from swing points
    levels = _cluster([h[i] for i in sh[-40:]] + [l[i] for i in sl[-40:]], (atr[-1] or c[-1] * 0.001) * 0.6)

    return Ctx(symbol, inst, bars, o, h, l, c, v, atr, ema20, ema50, ema200, rsi,
               sh, sl, last_sh, last_sl, pdh, pdl, zones, levels, macro)


def _order_block(bars: List[Bar], i: int, side: str) -> Tuple[float, float]:
    """Last opposite candle before the impulse leg that broke structure."""
    for j in range(i - 1, max(i - 14, 0), -1):
        b = bars[j]
        if side == "bull" and b.c < b.o:
            return b.l, b.h
        if side == "sell" and b.c > b.o:
            return b.l, b.h
    b = bars[i]
    return (b.l, b.h)


class BaseStrategy:
    key = "base"
    label = "Base"
    family = "GENERIC"
    min_rr = 1.3

    def prepare(self, ctx: Ctx) -> None:  # noqa: D401
        return None

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        raise NotImplementedError

    def _valid(self, sig: Signal, entry: float) -> Optional[Signal]:
        if sig.rr(entry) < self.min_rr:
            return None
        return sig


class SupportResistance(BaseStrategy):
    """Rejection at a level, plus breakout-retest continuation.

    Both setups are strictly causal: the retest branch looks *back* for a
    breakout that already happened, never forward.
    """

    key = "sr"
    label = "Support / Resistance"
    family = "PRICE-ACTION"
    min_rr = 1.4

    def prepare(self, ctx: Ctx) -> None:
        n = len(ctx.c)
        self._sup: List[Optional[float]] = [None] * n
        self._res: List[Optional[float]] = [None] * n
        self._breaks: Dict[int, List[Tuple[str, float]]] = {}
        levels = ctx.levels
        for i in range(n):
            a = ctx.atr_at(i)
            price = ctx.c[i]
            lo = hi = None
            for lv in levels:
                if lv < price - a * 0.1:
                    if lo is None or lv > lo:
                        lo = lv
                elif lv > price + a * 0.1:
                    if hi is None or lv < hi:
                        hi = lv
            self._sup[i], self._res[i] = lo, hi
            body = abs(ctx.c[i] - ctx.o[i])
            if i > 0 and body > a * 0.7:
                if self._res[i - 1] is not None and ctx.c[i] > self._res[i - 1] and ctx.c[i - 1] <= self._res[i - 1]:
                    self._breaks.setdefault(i, []).append(("buy", self._res[i - 1]))
                if self._sup[i - 1] is not None and ctx.c[i] < self._sup[i - 1] and ctx.c[i - 1] >= self._sup[i - 1]:
                    self._breaks.setdefault(i, []).append(("sell", self._sup[i - 1]))

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        if not ctx.warm(i):
            return None
        a = ctx.atr_at(i)
        price = ctx.c[i]
        support, resistance = self._sup[i], self._res[i]
        body = abs(ctx.c[i] - ctx.o[i])
        bull = ctx.c[i] > ctx.o[i]

        # A. rejection: wick pierces level, close returns above/below
        if support is not None and ctx.l[i] < support and ctx.c[i] > support and bull:
            sl = min(ctx.l[i], support) - a * 0.35
            tp = resistance if resistance and (resistance - price) > a * 1.2 else price + a * 3.0
            return self._valid(Signal("buy", sl, tp, f"Rejection at support {support:.5f}", "SR-REJ"), price)
        if resistance is not None and ctx.h[i] > resistance and ctx.c[i] < resistance and not bull:
            sl = max(ctx.h[i], resistance) + a * 0.35
            tp = support if support and (price - support) > a * 1.2 else price - a * 3.0
            return self._valid(Signal("sell", sl, tp, f"Rejection at resistance {resistance:.5f}", "SR-REJ"), price)

        # B. retest of a breakout that closed within the last 6 bars
        for j in range(i - 1, max(i - 7, 0), -1):
            for side, level in self._breaks.get(j, []):
                if side == "buy" and ctx.l[i] <= level + a * 0.15 and ctx.c[i] > level:
                    sl = level - a * 0.6
                    return self._valid(Signal("buy", sl, price + a * 3.2,
                                              f"Breakout-retest of {level:.5f}", "SR-BR"), price)
                if side == "sell" and ctx.h[i] >= level - a * 0.15 and ctx.c[i] < level:
                    sl = level + a * 0.6
                    return self._valid(Signal("sell", sl, price - a * 3.2,
                                              f"Break-retest of {level:.5f}", "SR-BR"), price)
        return None


class SMCStrategy(BaseStrategy):
    """Break of structure + order block / fair value gap retrace."""

    key = "smc"
    label = "Smart Money Concepts"
    family = "STRUCTURE"
    min_rr = 1.8

    def prepare(self, ctx: Ctx) -> None:
        self._zones: Dict[str, Tuple[List[int], List[dict]]] = {}
        for side in ("buy", "sell"):
            zs = [z for z in ctx.zones if z["side"] == side]
            self._zones[side] = ([z["born"] for z in zs], zs)

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        if not ctx.warm(i):
            return None
        a = ctx.atr_at(i)
        for side in ("buy", "sell"):
            borns, zs = self._zones[side]
            start = bisect.bisect_left(borns, i - 30)
            for k in range(start, len(zs)):
                z = zs[k]
                if z["born"] >= i:
                    break
                if i > z["expiry"]:
                    continue
                if side == "buy":
                    touched = z["lo"] - a * 0.5 <= ctx.l[i] <= z["hi"]
                    held = ctx.c[i] > z["lo"] and ctx.c[i] > ctx.o[i]
                    if touched and held:
                        sl = z["lo"] - a * 0.4
                        tp = ctx.c[i] + max((ctx.c[i] - sl) * 2.6, a * 2.5)
                        return self._valid(Signal("buy", sl, tp,
                                                  f"{z['kind']} demand zone {z['lo']:.5f}-{z['hi']:.5f}",
                                                  "SMC-" + z["kind"].split("+")[0]), ctx.c[i])
                else:
                    touched = z["lo"] <= ctx.h[i] <= z["hi"] + a * 0.5
                    held = ctx.c[i] < z["hi"] and ctx.c[i] < ctx.o[i]
                    if touched and held:
                        sl = z["hi"] + a * 0.4
                        tp = ctx.c[i] - max((sl - ctx.c[i]) * 2.6, a * 2.5)
                        return self._valid(Signal("sell", sl, tp,
                                                  f"{z['kind']} supply zone {z['lo']:.5f}-{z['hi']:.5f}",
                                                  "SMC-" + z["kind"].split("+")[0]), ctx.c[i])
        return None


class LiquidityStrategy(BaseStrategy):
    """Stop-hunt reversal after a sweep of session or equal highs/lows."""

    key = "liq"
    label = "Liquidity Sweep"
    family = "MICROSTRUCTURE"
    min_rr = 1.5

    def prepare(self, ctx: Ctx) -> None:
        # equal highs / lows from confirmed swings
        self._eq_h: List[float] = []
        self._eq_l: List[float] = []
        tol = (ctx.atr[-1] or ctx.c[-1] * 0.001) * 0.22
        for idx in range(1, len(ctx.swing_h)):
            p, q = ctx.h[ctx.swing_h[idx]], ctx.h[ctx.swing_h[idx - 1]]
            if abs(p - q) <= tol:
                self._eq_h.append(max(p, q))
        for idx in range(1, len(ctx.swing_l)):
            p, q = ctx.l[ctx.swing_l[idx]], ctx.l[ctx.swing_l[idx - 1]]
            if abs(p - q) <= tol:
                self._eq_l.append(min(p, q))

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        if not ctx.warm(i):
            return None
        a = ctx.atr_at(i)
        pdh, pdl = ctx.pdh[i], ctx.pdl[i]
        body = abs(ctx.c[i] - ctx.o[i])
        upper_wick = ctx.h[i] - max(ctx.c[i], ctx.o[i])
        lower_wick = min(ctx.c[i], ctx.o[i]) - ctx.l[i]

        # sweep above prior-day high or equal highs, then close back below
        for lvl in ([pdh] if pdh else []) + self._eq_h[-12:]:
            if lvl is None:
                continue
            if ctx.h[i] > lvl and ctx.c[i] < lvl and upper_wick > body * 0.9:
                sl = ctx.h[i] + a * 0.3
                tp = ctx.c[i] - max((sl - ctx.c[i]) * 2.0, a * 2.0)
                if pdl and pdl < ctx.c[i] - a:
                    tp = max(tp, pdl)
                return self._valid(Signal("sell", sl, tp, f"Liquidity sweep above {lvl:.5f}", "LIQ-SWEEP"), ctx.c[i])
        for lvl in ([pdl] if pdl else []) + self._eq_l[-12:]:
            if lvl is None:
                continue
            if ctx.l[i] < lvl and ctx.c[i] > lvl and lower_wick > body * 0.9:
                sl = ctx.l[i] - a * 0.3
                tp = ctx.c[i] + max((ctx.c[i] - sl) * 2.0, a * 2.0)
                if pdh and pdh > ctx.c[i] + a:
                    tp = min(tp, pdh) if tp > pdh else tp
                return self._valid(Signal("buy", sl, tp, f"Liquidity sweep below {lvl:.5f}", "LIQ-SWEEP"), ctx.c[i])
        return None


class MacroStrategy(BaseStrategy):
    """Higher-timeframe trend + dollar / risk-appetite regime filter."""

    key = "macro"
    label = "Macro Regime"
    family = "MACRO"
    min_rr = 1.6

    def prepare(self, ctx: Ctx) -> None:
        c = ctx.c
        n = len(c)
        # H4 proxy: 4 x M15
        h4: List[float] = [c[i] for i in range(3, n, 4)]
        e20 = ta.ema(h4, 20)
        e50 = ta.ema(h4, 50)
        self._h4_trend: List[int] = []
        for k in range(len(h4)):
            if e20[k] is None or e50[k] is None:
                self._h4_trend.append(0)
            else:
                self._h4_trend.append(1 if e20[k] > e50[k] else -1)
        self._macro_bias = 0
        if ctx.macro:
            dxy = ctx.macro.get("dxy", [])
            risk = ctx.macro.get("risk", [])
            if len(dxy) > 200 and len(risk) > 200:
                dxy_trend = 1 if sum(dxy[-50:]) > 0 else -1
                risk_trend = 1 if sum(risk[-50:]) > 0 else -1
                # weak dollar + risk-on => pro-cyclical longs
                self._macro_bias = (risk_trend * 1) + (-dxy_trend * 0.6)
                if INSTRUMENTS[ctx.symbol].klass == "COMMODITY":
                    self._macro_bias += (-dxy_trend) * 0.4

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        if not ctx.warm(i) or i < 220:
            return None
        a = ctx.atr_at(i)
        h4_idx = i // 4
        if h4_idx >= len(self._h4_trend):
            return None
        trend = self._h4_trend[h4_idx]
        if trend == 0:
            return None
        e20 = ctx.ema20[i]
        rsi = ctx.rsi[i]
        if e20 is None or rsi is None:
            return None
        # pullback into the 20 EMA in the direction of the H4 trend
        if trend > 0 and self._macro_bias >= -0.5 and ctx.l[i] <= e20 + a * 0.15 and ctx.c[i] > e20 and rsi < 62:
            sl = ctx.c[i] - a * 1.3
            tp = ctx.c[i] + a * 3.0
            return self._valid(Signal("buy", sl, tp, f"H4 uptrend pullback (bias {self._macro_bias:+.1f})", "MACRO-TREND"), ctx.c[i])
        if trend < 0 and self._macro_bias <= 0.5 and ctx.h[i] >= e20 - a * 0.15 and ctx.c[i] < e20 and rsi > 38:
            sl = ctx.c[i] + a * 1.3
            tp = ctx.c[i] - a * 3.0
            return self._valid(Signal("sell", sl, tp, f"H4 downtrend pullback (bias {self._macro_bias:+.1f})", "MACRO-TREND"), ctx.c[i])
        return None


class ConfluenceStrategy(BaseStrategy):
    """Voting ensemble: needs at least two independent edges on the same bar."""

    key = "confluence"
    label = "Confluence Ensemble"
    family = "ENSEMBLE"
    min_rr = 1.5

    def __init__(self) -> None:
        self.members = [SupportResistance(), SMCStrategy(), LiquidityStrategy(), MacroStrategy()]

    def prepare(self, ctx: Ctx) -> None:
        for m in self.members:
            m.prepare(ctx)

    def signal(self, ctx: Ctx, i: int) -> Optional[Signal]:
        votes: List[Signal] = []
        for m in self.members:
            s = m.signal(ctx, i)
            if s:
                votes.append(s)
        if len(votes) < 2:
            return None
        buys = [v for v in votes if v.side == "buy"]
        sells = [v for v in votes if v.side == "sell"]
        if len(buys) >= 2 and len(buys) >= len(sells):
            sl = min(v.sl for v in buys)
            tp = max(v.tp for v in buys)
            return self._valid(Signal("buy", sl, tp,
                                      " | ".join(v.reason for v in buys[:3]),
                                      "CONF-BUY", strength=len(buys)), ctx.c[i])
        if len(sells) >= 2 and len(sells) > len(buys):
            sl = max(v.sl for v in sells)
            tp = min(v.tp for v in sells)
            return self._valid(Signal("sell", sl, tp,
                                      " | ".join(v.reason for v in sells[:3]),
                                      "CONF-SELL", strength=len(sells)), ctx.c[i])
        return None


STRATEGY_CLASSES: Dict[str, type] = {
    "sr": SupportResistance,
    "smc": SMCStrategy,
    "liq": LiquidityStrategy,
    "macro": MacroStrategy,
    "confluence": ConfluenceStrategy,
}


def make_strategy(key: str) -> BaseStrategy:
    """Fresh instance per (strategy, instrument) - strategies hold per-data state."""
    return STRATEGY_CLASSES[key]()


STRATEGIES: Dict[str, BaseStrategy] = {k: cls() for k, cls in STRATEGY_CLASSES.items()}


def strategy_meta() -> List[dict]:
    return [
        {"key": s.key, "label": s.label, "family": s.family, "min_rr": s.min_rr}
        for s in STRATEGIES.values()
    ]
