"""Market data layer.

Three sources, one interface:

* ``synthetic``  - regime-switching simulator used for validation runs. Deterministic
  per symbol seed, so a strategy result is reproducible. It models volatility
  clustering, session liquidity, order-flow imbalance and stop-hunt sweeps.
* ``csv``        - real historical bars from disk (``data/<SYMBOL>.csv``).
* ``mt5``        - live/historical bars straight from the MetaTrader 5 terminal.

The validation gate is source-aware: a strategy validated on synthetic data is
flagged ``SIM-VALIDATED`` and is *not* allowed to trade real money until it also
passes on the broker feed.
"""
from __future__ import annotations

import csv
import math
import os
import random
import time
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.config import INSTRUMENTS, ROOT, Instrument

BAR_SECONDS = 900  # M15

REGIMES = ("UP", "DOWN", "RANGE", "SQUEEZE", "EXPAND")
TRANSITION = {
    "UP": {"UP": 0.90, "DOWN": 0.02, "RANGE": 0.05, "SQUEEZE": 0.01, "EXPAND": 0.02},
    "DOWN": {"UP": 0.02, "DOWN": 0.90, "RANGE": 0.05, "SQUEEZE": 0.01, "EXPAND": 0.02},
    "RANGE": {"UP": 0.06, "DOWN": 0.06, "RANGE": 0.82, "SQUEEZE": 0.04, "EXPAND": 0.02},
    "SQUEEZE": {"UP": 0.10, "DOWN": 0.10, "RANGE": 0.25, "SQUEEZE": 0.30, "EXPAND": 0.25},
    "EXPAND": {"UP": 0.14, "DOWN": 0.14, "RANGE": 0.22, "SQUEEZE": 0.05, "EXPAND": 0.45},
}
DRIFT = {"UP": 0.55, "DOWN": -0.55, "RANGE": 0.0, "SQUEEZE": 0.05, "EXPAND": 0.10}

BASE_PRICE = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "USDJPY": 152.30,
    "AUDUSD": 0.6620,
    "XAUUSD": 2380.0,
    "XAGUSD": 30.40,
    "WTIUSD": 78.60,
    "COPPER": 4.480,
}
# annualised-ish vol per bar, expressed as a fraction of price
BASE_VOL = {
    "EURUSD": 0.0011,
    "GBPUSD": 0.0013,
    "USDJPY": 0.0012,
    "AUDUSD": 0.0014,
    "XAUUSD": 0.0021,
    "XAGUSD": 0.0028,
    "WTIUSD": 0.0030,
    "COPPER": 0.0024,
}
# sensitivity to the dollar factor (negative = quoted in USD terms)
DXY_BETA = {
    "EURUSD": -1.0,
    "GBPUSD": -0.9,
    "USDJPY": 0.8,
    "AUDUSD": -0.7,
    "XAUUSD": -0.45,
    "XAGUSD": -0.40,
    "WTIUSD": -0.20,
    "COPPER": -0.25,
}
RISK_BETA = {
    "EURUSD": 0.35,
    "GBPUSD": 0.40,
    "USDJPY": -0.15,
    "AUDUSD": 0.70,
    "XAUUSD": 0.20,
    "XAGUSD": 0.45,
    "WTIUSD": 0.60,
    "COPPER": 0.65,
}


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float

    def as_list(self) -> List[float]:
        return [self.t, self.o, self.h, self.l, self.c, self.v]


def session_multiplier(hour_utc: int) -> float:
    if 7 <= hour_utc < 11:      # London open
        return 1.35
    if 12 <= hour_utc < 16:     # London / New York overlap
        return 1.60
    if 16 <= hour_utc < 20:     # New York
        return 1.20
    if 0 <= hour_utc < 4:       # Tokyo
        return 0.85
    return 0.60                  # dead zone


def _pick_regime(current: str, rng: random.Random) -> str:
    row = TRANSITION[current]
    r = rng.random()
    acc = 0.0
    for key, p in row.items():
        acc += p
        if r <= acc:
            return key
    return current


class MarketData:
    """Regime-switching synthetic market with cross-asset factors."""

    def __init__(self, symbols: List[str], bars: int = 3200, seed: int = 20261017,
                 end_time: Optional[int] = None):
        self.symbols = symbols
        self.n = bars
        self.seed = seed
        # anchor to 00:00 UTC so the session-volatility pattern (and therefore the
        # whole validation run) is reproducible instead of drifting with wall-clock hour
        self.end_time = end_time or int(time.time() // 86400 * 86400)
        self.start_time = self.end_time - bars * BAR_SECONDS
        self.series: Dict[str, List[Bar]] = {}
        self.factors: Dict[str, List[float]] = {"dxy": [], "yield": [], "risk": []}
        self._state: Dict[str, dict] = {}
        self._rng = random.Random(seed)
        self._build_factors()
        for sym in symbols:
            self.series[sym] = self._build_symbol(sym)

    # ------------------------------------------------------------------ real data
    def adopt(self, symbol: str, bars: List[Bar], min_bars: int = 300) -> bool:
        """Replace a synthetic series with real broker bars."""
        if not bars or len(bars) < min_bars:
            return False
        self.series[symbol] = list(bars)
        return True

    def build_real_factors(self) -> None:
        """Derive the cross-asset factors from real prices instead of the simulator."""
        ref = self.series.get("EURUSD") or []
        n = len(ref) or max((len(v) for v in self.series.values()), default=0)
        if n < 2 or len(ref) < 2:
            # no EURUSD series (partial universe): neutral factors beat a crash
            self.factors = {"dxy": [0.0] * n, "yield": [0.0] * n, "risk": [0.0] * n}
            return
        dxy = [0.0] * n
        risk = [0.0] * n
        for i in range(1, n):
            prev, cur = ref[i - 1].c, ref[i].c
            if prev:
                dxy[i] = -(cur / prev - 1.0)      # dollar strength = inverse EURUSD
        gold = self.series.get("XAUUSD") or []
        if len(gold) >= 2:
            for i in range(1, min(n, len(gold))):
                prev, cur = gold[i - 1].c, gold[i].c
                if prev:
                    risk[i] = cur / prev - 1.0
        self.factors = {"dxy": dxy, "yield": [0.0] * n, "risk": risk}

    def append_live(self, symbol: str, bars: List[Bar], keep: int = 4000) -> bool:
        """Merge freshly polled bars: update the forming bar, append new ones."""
        ser = self.series.get(symbol)
        if not ser or not bars:
            return False
        changed = False
        for b in bars:
            if b.t > ser[-1].t:
                ser.append(b)
                changed = True
            elif b.t == ser[-1].t:
                last = ser[-1]
                if (last.h, last.l, last.c) != (b.h, b.l, b.c):
                    ser[-1] = b
                    changed = True
        if len(ser) > keep:
            del ser[:len(ser) - keep]
        return changed

    # ------------------------------------------------------------------ factors
    def _build_factors(self) -> None:
        rng = random.Random(self.seed ^ 0x5F5F)
        state = {"regime": "RANGE", "vol": 1.0}
        for i in range(self.n):
            if rng.random() < 0.02:
                state["regime"] = _pick_regime(state["regime"], rng)
            state["vol"] = 0.93 * state["vol"] + 0.07 * (0.7 + rng.random() * 0.9)
            base = 0.00055 * state["vol"] * (1 + DRIFT[state["regime"]] * 0.5)
            dxy = base * rng.gauss(0, 1) + DRIFT[state["regime"]] * 0.00025
            yields = 0.0004 * state["vol"] * rng.gauss(0, 1) - dxy * 0.3
            risk = 0.0011 * state["vol"] * rng.gauss(0, 1) + DRIFT[state["regime"]] * 0.0006
            self.factors["dxy"].append(dxy)
            self.factors["yield"].append(yields)
            self.factors["risk"].append(risk)

    # ------------------------------------------------------------------ symbol
    def _build_symbol(self, sym: str) -> List[Bar]:
        rng = random.Random(self.seed ^ (zlib.crc32(sym.encode()) & 0xFFFFFF))
        price = BASE_PRICE.get(sym, 100.0)
        vol0 = BASE_VOL.get(sym, 0.0015)
        vol = vol0
        regime = "RANGE"
        bars: List[Bar] = []
        closes: List[float] = []
        for i in range(self.n):
            if rng.random() < 0.014:
                regime = _pick_regime(regime, rng)
            # volatility clustering + session effect
            ts = self.start_time + i * BAR_SECONDS
            hour = time.gmtime(ts).tm_hour
            sess = session_multiplier(hour)
            shock = 1.0 + max(0.0, rng.gauss(0, 1)) * 0.55 if regime == "EXPAND" else 1.0
            vol = 0.90 * vol + 0.10 * vol0 * (0.75 + rng.random() * 0.7)
            sigma = vol * sess * shock
            f = self.factors
            drift = DRIFT[regime] * vol0 * 0.62
            ret = (drift
                   + DXY_BETA.get(sym, -0.3) * f["dxy"][i] * 0.55
                   + RISK_BETA.get(sym, 0.3) * f["risk"][i] * 0.45
                   + sigma * rng.gauss(0, 1) + 0.16 * (closes[-1] / closes[-2] - 1 if len(closes) > 1 else 0.0))
            o = price
            c = max(o * (1 + ret), 1e-6)
            body = abs(c - o)
            wick = sigma * o * (0.35 + rng.random() * 0.9)
            h = max(o, c) + wick
            l = min(o, c) - wick

            # stop-hunt / liquidity sweep: spike through a 20-bar extreme, close back
            if len(closes) >= 20 and rng.random() < 0.025:
                window = closes[-20:]
                if rng.random() < 0.5:
                    h = max(h, max(window) * (1 + sigma * (0.5 + rng.random())))
                    c = min(c, max(window) * (1 - sigma * 0.15))
                else:
                    l = min(l, min(window) * (1 - sigma * (0.5 + rng.random())))
                    c = max(c, min(window) * (1 + sigma * 0.15))
                h = max(h, o, c)
                l = min(l, o, c)

            volume = 1000 * sess * (0.6 + rng.random() * 0.8) * (1 + 6 * body / max(o * vol0, 1e-9))
            bars.append(Bar(ts, round(o, 6), round(h, 6), round(l, 6), round(c, 6), round(volume, 1)))
            closes.append(c)
            price = c
        self._state[sym] = {"regime": regime, "vol": vol, "price": price, "rng": rng}
        return bars

    # ------------------------------------------------------------------- live
    def next_bar(self, sym: str) -> Bar:
        """Append one fresh bar, continuing the same stochastic process."""
        st = self._state[sym]
        rng: random.Random = st["rng"]
        if rng.random() < 0.06:
            st["regime"] = _pick_regime(st["regime"], rng)
        vol0 = BASE_VOL.get(sym, 0.0015)
        sess = session_multiplier(time.gmtime().tm_hour)
        st["vol"] = 0.90 * st["vol"] + 0.10 * vol0 * (0.75 + rng.random() * 0.7)
        sigma = st["vol"] * sess
        drift = DRIFT[st["regime"]] * vol0 * 0.30
        ret = drift + sigma * rng.gauss(0, 1)
        o = st["price"]
        c = max(o * (1 + ret), 1e-6)
        wick = sigma * o * (0.35 + rng.random() * 0.9)
        h = max(o, c) + wick
        l = min(o, c) - wick
        bar = Bar(int(time.time()), round(o, 6), round(h, 6), round(l, 6), round(c, 6),
                  round(1000 * sess * (0.6 + rng.random()), 1))
        self.series[sym].append(bar)
        if len(self.series[sym]) > self.n * 2:
            del self.series[sym][: len(self.series[sym]) - self.n]
        st["price"] = c
        return bar

    def closes(self, sym: str) -> List[float]:
        return [b.c for b in self.series[sym]]

    def price(self, sym: str) -> float:
        """Live tick inside the current bar (mean-reverting to the bar close)."""
        bar = self.series[sym][-1]
        st = self._state[sym]
        tick = st.get("tick", bar.c)
        step = (bar.h - bar.l) * 0.14
        tick = tick + step * st["rng"].gauss(0, 1) + (bar.c - tick) * 0.22
        tick = min(bar.h, max(bar.l, tick))
        st["tick"] = tick
        return tick

    def last_bar(self, sym: str) -> Bar:
        return self.series[sym][-1]


def load_csv(symbol: str) -> Optional[List[Bar]]:
    """Load ``data/<SYMBOL>.csv`` with header time,open,high,low,close,volume."""
    path = os.path.join(ROOT, "data", f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    bars: List[Bar] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                bars.append(Bar(int(float(row["time"])), float(row["open"]), float(row["high"]),
                                float(row["low"]), float(row["close"]), float(row.get("volume", 0) or 0)))
            except (KeyError, ValueError):
                continue
    return bars or None


class MT5Feed:
    """Thin wrapper over the MetaTrader5 python package (optional dependency)."""

    def __init__(self, terminal_path: str = "", login: int = 0, password: str = "",
                 server: str = "", suffix: str = ""):
        self.available = False
        self.error = ""
        self.suffix = suffix
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on host
            self.error = f"MetaTrader5 package not installed ({exc.__class__.__name__})"
            return
        self.mt5 = mt5
        kwargs = {}
        if terminal_path:
            kwargs["path"] = terminal_path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kwargs):
            self.error = f"initialize failed: {mt5.last_error()}"
            return
        self.available = True

    def bars(self, symbol: str, count: int = 3000) -> Optional[List[Bar]]:
        if not self.available:
            return None
        mt5 = self.mt5
        rates = mt5.copy_rates_from_pos(symbol + self.suffix, mt5.TIMEFRAME_M15, 0, count)
        if rates is None or len(rates) == 0:
            self.error = f"no rates for {symbol}: {mt5.last_error()}"
            return None
        return [Bar(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["tick_volume"])) for r in rates]

    def tick(self, symbol: str) -> Optional[float]:
        if not self.available:
            return None
        t = self.mt5.symbol_info_tick(symbol + self.suffix)
        if t is None:
            return None
        return float((t.bid + t.ask) / 2)

    def shutdown(self) -> None:
        if self.available:
            try:
                self.mt5.shutdown()
            except Exception:
                pass


def make_instrument_map() -> Dict[str, Instrument]:
    return dict(INSTRUMENTS)
