"""Core helpers: config loading, time, small math utilities."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


CFG = load_config()


def cfg(path: str, default: Any = None) -> Any:
    node: Any = CFG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


@dataclass
class Instrument:
    symbol: str
    klass: str
    pip: float
    spread_pips: float
    commission_pips: float
    digits: int

    @property
    def cost_pips(self) -> float:
        return self.spread_pips + self.commission_pips


def instruments() -> Dict[str, Instrument]:
    out: Dict[str, Instrument] = {}
    for row in cfg("trading.symbols", []):
        out[row["symbol"]] = Instrument(
            symbol=row["symbol"],
            klass=row["klass"],
            pip=row["pip"],
            spread_pips=row["spread_pips"],
            commission_pips=row["commission_pips"],
            digits=row["digits"],
        )
    return out


INSTRUMENTS = instruments()

# Asset classes are ordered by how they are allowed to trade; anything not in
# trading.max_per_class gets a cap of 0, i.e. "analysed but never traded".
CLASS_HINTS = (
    ("forex", "FOREX"),
    ("metal", "COMMODITY"),
    ("commodit", "COMMODITY"),
    ("energ", "COMMODITY"),
    ("oil", "COMMODITY"),
    ("indice", "INDEX"),
    ("index", "INDEX"),
    ("crypto", "CRYPTO"),
    ("stock", "STOCK"),
    ("share", "STOCK"),
    ("bond", "BOND"),
)


def classify(path: str, name: str = "") -> str:
    """Derive the asset class from the broker's own folder path.

    MT5 exposes e.g. ``ForexCent\\EURUSDc`` or
    ``Metals & Energies\\Spot\\Gold & Silver Cent\\XAUUSDc``, which is the most
    reliable signal available - better than guessing from the ticker.
    """
    p = (path or "").lower()
    for needle, klass in CLASS_HINTS:
        if needle in p:
            return klass
    n = (name or "").upper()
    if n.startswith(("XAU", "XAG", "XPT", "XPD")):
        return "COMMODITY"
    if len(n) >= 6 and n[:3].isalpha() and n[3:6].isalpha():
        return "FOREX"
    return "OTHER"


def pip_size(digits: int, point: float = 0.0) -> float:
    """1 pip in price units (5/3-digit quotes are fractional-pip priced)."""
    if digits in (3, 5):
        return (point or 10 ** -digits) * 10.0
    return point or 10 ** -digits


def register_instrument(symbol: str, klass: str = "", path: str = "", digits: int = 5,
                        point: float = 0.0, spread_points: float = 0.0,
                        name: str = "") -> Instrument:
    """Add (or refresh) an instrument discovered from the broker.

    The registry is shared by every module through ``INSTRUMENTS``, so a symbol
    becomes analysable and tradeable the moment the terminal reports it - no
    config edit, no restart.
    """
    klass = (klass or classify(path, name or symbol)).upper()
    pip = pip_size(int(digits or 5), float(point or 0.0))
    point = float(point or pip / 10.0)
    # broker spread is quoted in points; convert to pips for the cost model
    spread_pips = (float(spread_points or 0.0) * point / pip) if pip else 0.0
    if spread_pips <= 0:
        spread_pips = 1.0 if klass == "FOREX" else 2.0
    commission_pips = 0.3 if klass == "FOREX" else 0.0
    inst = Instrument(symbol=symbol, klass=klass, pip=pip,
                      spread_pips=round(spread_pips, 2),
                      commission_pips=commission_pips, digits=int(digits or 5))
    INSTRUMENTS[symbol] = inst
    return inst


def instrument(symbol: str) -> Instrument:
    """Look up an instrument, registering a sane default if it is new."""
    found = INSTRUMENTS.get(symbol)
    return found if found is not None else register_instrument(symbol)


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


@dataclass
class Ring:
    """Fixed-size rolling buffer used for sparklines / equity history."""

    size: int = 240
    data: List[float] = field(default_factory=list)

    def push(self, value: float) -> None:
        self.data.append(float(value))
        if len(self.data) > self.size:
            del self.data[: len(self.data) - self.size]

    def as_list(self) -> List[float]:
        return list(self.data)
