"""Execution layer: paper broker (always available) and MetaTrader 5 bridge.

The dashboard reports exactly which one is live. There is no silent fallback:
if MT5 is requested but unavailable, ``mode`` stays ``PAPER`` and the reason is
surfaced to the operator.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.config import INSTRUMENTS, cfg


@dataclass
class Position:
    id: int
    symbol: str
    side: str
    lots: float
    units: float
    entry: float
    sl: float
    tp: float
    risk_amount: float
    strategy: str
    reason: str
    open_t: int
    entry_bar_t: int = 0
    price: float = 0.0
    pnl: float = 0.0
    r: float = 0.0

    def mark(self, price: float) -> None:
        d = 1.0 if self.side == "buy" else -1.0
        self.price = price
        self.pnl = (price - self.entry) * self.units * d
        self.r = self.pnl / self.risk_amount if self.risk_amount else 0.0

    def to_state(self) -> dict:
        return {"id": self.id, "symbol": self.symbol, "side": self.side, "lots": self.lots,
                "units": self.units, "entry": self.entry, "sl": self.sl, "tp": self.tp,
                "risk_amount": self.risk_amount, "strategy": self.strategy,
                "reason": self.reason, "open_t": self.open_t,
                "entry_bar_t": self.entry_bar_t, "price": self.price,
                "pnl": self.pnl, "r": self.r}

    def as_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "side": self.side,
            "lots": round(self.lots, 2), "entry": round(self.entry, 6),
            "sl": round(self.sl, 6), "tp": round(self.tp, 6),
            "price": round(self.price, 6), "pnl": round(self.pnl, 2),
            "r": round(self.r, 2), "strategy": self.strategy,
            "reason": self.reason, "open_t": self.open_t,
        }


class PaperBroker:
    mode = "PAPER"

    def __init__(self, balance: float, risk_pct: float):
        self.start_balance = balance
        self.balance = balance
        self.equity = balance
        self.risk_pct = risk_pct
        self.positions: Dict[int, Position] = {}
        self.closed: List[dict] = []
        self._ids = itertools.count(1001)
        self.realized = 0.0

    # ------------------------------------------------------------------ orders
    def open(self, symbol: str, side: str, price: float, sl: float, tp: float,
             strategy: str, reason: str, bar_t: int = 0) -> Optional[Position]:
        risk_amount = self.equity * self.risk_pct / 100.0
        dist = abs(price - sl)
        if dist <= 0:
            return None
        units = risk_amount / dist
        notional = units * price
        pos = Position(next(self._ids), symbol, side, notional / 100_000.0, units,
                       price, sl, tp, risk_amount, strategy, reason, int(time.time()), bar_t)
        pos.mark(price)
        self.positions[pos.id] = pos
        return pos

    def close(self, pid: int, price: float, reason: str) -> Optional[dict]:
        pos = self.positions.pop(pid, None)
        if not pos:
            return None
        pos.mark(price)
        self.balance += pos.pnl
        self.realized += pos.pnl
        rec = pos.as_dict()
        rec.update(exit=round(price, 6), exit_reason=reason, close_t=int(time.time()),
                   pnl=round(pos.pnl, 2), r=round(pos.r, 2))
        self.closed.append(rec)
        del self.closed[:-200]
        return rec

    def close_all(self, prices: Dict[str, float], reason: str = "FLATTEN") -> List[dict]:
        out = []
        for pid in list(self.positions):
            pos = self.positions[pid]
            out.append(self.close(pid, prices.get(pos.symbol, pos.entry), reason))
        return [o for o in out if o]

    # ------------------------------------------------------------------ marks
    def mark(self, prices: Dict[str, float], ranges: Optional[Dict[str, tuple]] = None) -> List[dict]:
        """Mark to market; auto-close on SL/TP using intrabar extremes."""
        ranges = ranges or {}
        closed = []
        for pid, pos in list(self.positions.items()):
            px = prices.get(pos.symbol, pos.entry)
            lo, hi, bar_t = ranges.get(pos.symbol, (px, px, 0))
            if bar_t and pos.entry_bar_t == bar_t:
                pos.mark(px)   # entered on this bar: no intrabar trigger yet
                continue
            if pos.side == "buy":
                if lo <= pos.sl:
                    closed.append(self.close(pid, pos.sl, "SL"))
                    continue
                if hi >= pos.tp:
                    closed.append(self.close(pid, pos.tp, "TP"))
                    continue
            else:
                if hi >= pos.sl:
                    closed.append(self.close(pid, pos.sl, "SL"))
                    continue
                if lo <= pos.tp:
                    closed.append(self.close(pid, pos.tp, "TP"))
                    continue
            pos.mark(px)
        self.equity = self.balance + sum(p.pnl for p in self.positions.values())
        return [c for c in closed if c]

    def account(self) -> dict:
        unreal = sum(p.pnl for p in self.positions.values())
        return {
            "mode": self.mode, "balance": round(self.balance, 2),
            "equity": round(self.balance + unreal, 2),
            "unrealized": round(unreal, 2),
            "realized_today": round(self.realized, 2),
            "margin_used": round(sum(abs(p.units * p.price) for p in self.positions.values()) / 30.0, 2),
            "currency": "USD",
        }

    def open_positions(self) -> List[dict]:
        return [p.as_dict() for p in self.positions.values()]

    def heat(self) -> float:
        """Open risk as a percentage of equity (portfolio heat)."""
        risk = sum(p.risk_amount for p in self.positions.values())
        return risk / (self.equity or 1) * 100.0

    # ------------------------------------------------------------- persistence
    def to_state(self) -> dict:
        return {"balance": self.balance, "start_balance": self.start_balance,
                "realized": self.realized, "closed": self.closed[-60:],
                "positions": [p.to_state() for p in self.positions.values()]}

    def restore(self, st: dict) -> int:
        if not st:
            return 0
        self.balance = float(st.get("balance", self.balance))
        self.start_balance = float(st.get("start_balance", self.start_balance))
        self.realized = float(st.get("realized", 0.0))
        self.closed = list(st.get("closed") or [])[-200:]
        for d in st.get("positions") or []:
            try:
                pos = Position(**d)
            except TypeError:
                continue
            self.positions[pos.id] = pos
        if self.positions:
            self._ids = itertools.count(max(self.positions) + 1)
        self.equity = self.balance + sum(p.pnl for p in self.positions.values())
        return len(self.positions)


class MT5Broker:
    """Real-money bridge. Requires the MetaTrader5 package and a logged-in terminal."""

    mode = "MT5"

    def __init__(self, terminal_path: str = "", login: int = 0, password: str = "",
                 server: str = "", suffix: str = "", magic: int = 771001):
        self.available = False
        self.error = ""
        self.suffix = suffix
        self.magic = magic
        self.risk_pct = float(cfg("trading.risk_per_trade_pct", 0.5))
        self.allow_min_lot = bool(cfg("mt5.allow_min_lot", False))
        self._resolved: Dict[str, str] = {}
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            self.error = f"MetaTrader5 not installed ({exc.__class__.__name__})"
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

    # ------------------------------------------------------------------ symbols
    def resolve(self, symbol: str) -> Optional[str]:
        """Map a configured symbol to the name this broker actually quotes."""
        if symbol in self._resolved:
            return self._resolved[symbol]
        if not self.available:
            return None
        mt5 = self.mt5
        for cand in (symbol + self.suffix if self.suffix else symbol, symbol):
            if mt5.symbol_info(cand) is not None:
                self._resolved[symbol] = cand
                return cand
        best = None
        for s in mt5.symbols_get() or []:
            if s.name.startswith(symbol) and (best is None or len(s.name) < len(best)):
                best = s.name
        if best:
            self._resolved[symbol] = best
        return best

    def account(self) -> dict:
        if not self.available:
            return {"mode": "MT5-OFFLINE", "error": self.error, "balance": 0, "equity": 0,
                    "unrealized": 0, "realized_today": 0, "margin_used": 0, "currency": "USD"}
        info = self.mt5.account_info()
        if info is None:
            return {"mode": "MT5-OFFLINE", "error": str(self.mt5.last_error()), "balance": 0,
                    "equity": 0, "unrealized": 0, "realized_today": 0, "margin_used": 0,
                    "currency": "USD"}
        return {
            "mode": "MT5", "balance": round(float(info.balance), 2),
            "equity": round(float(info.equity), 2),
            "unrealized": round(float(info.equity - info.balance), 2),
            "realized_today": 0.0, "margin_used": round(float(info.margin), 2),
            "currency": info.currency, "login": int(info.login), "server": info.server,
        }

    def open_positions(self) -> List[dict]:
        if not self.available:
            return []
        out = []
        for p in self.mt5.positions_get() or []:
            if p.magic != self.magic:
                continue
            out.append({
                "id": int(p.ticket), "symbol": p.symbol,
                "side": "buy" if p.type == 0 else "sell", "lots": round(float(p.volume), 2),
                "entry": float(p.price_open), "sl": float(p.sl), "tp": float(p.tp),
                "price": float(p.price_current), "pnl": round(float(p.profit), 2),
                "r": 0.0, "strategy": "mt5", "reason": p.comment, "open_t": int(p.time),
            })
        return out

    def open(self, symbol: str, side: str, price: float, sl: float, tp: float,
             strategy: str, reason: str, bar_t: int = 0) -> Optional[dict]:
        """Send a real market order, sized from the LIVE account balance.

        Sizing uses the broker's own ``order_calc_profit`` rather than a contract
        size guess, so a cent account or an unusual instrument still risks the
        configured percentage. If the minimum lot would exceed the risk budget the
        order is refused instead of quietly risking 20x what was intended.
        """
        if not self.available:
            return None
        mt5 = self.mt5
        try:
            term = mt5.terminal_info()
            if term is not None and not bool(getattr(term, "trade_allowed", True)):
                self.error = ("AutoTrading di terminal MT5 masih OFF — klik tombol "
                              "'Algo Trading' di toolbar MT5, lalu order jalan sendiri")
                return None
        except Exception:
            pass
        full = self.resolve(symbol)
        if not full:
            self.error = f"{symbol} tidak tersedia di broker"
            return None
        info = mt5.symbol_info(full)
        if info is None:
            return None
        if not info.visible:
            mt5.symbol_select(full, True)
            info = mt5.symbol_info(full)
        tick = mt5.symbol_info_tick(full)
        if tick is None:
            return None
        price = tick.ask if side == "buy" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        sl_dist = abs(price - sl)
        if sl_dist <= 0:
            return None

        acct = mt5.account_info()
        if acct is None:
            return None
        risk_amount = float(acct.balance) * self.risk_pct / 100.0
        per_lot = 0.0
        try:
            per_lot = abs(float(mt5.order_calc_profit(order_type, full, 1.0, price, sl) or 0))
        except Exception:
            per_lot = 0.0
        if per_lot <= 0:
            contract = float(info.trade_contract_size or 100000)
            per_lot = sl_dist * contract

        step = float(info.volume_step or 0.01)
        min_lot = float(info.volume_min or step)
        max_lot = float(info.volume_max or 100.0)
        lots = risk_amount / per_lot
        lots = math.floor(lots / step) * step
        if lots < min_lot:
            if not self.allow_min_lot:
                self.error = (f"modal {acct.balance:.2f} {acct.currency} tidak cukup: "
                              f"lot minimum {min_lot} berisiko {per_lot * min_lot:.2f}, "
                              f"budget {risk_amount:.2f}")
                return None
            hard = float(cfg("mt5.max_risk_pct_hard", 2.0))
            min_risk = per_lot * min_lot
            if min_risk > float(acct.balance) * hard / 100.0:
                self.error = (f"lot minimum {min_lot} berisiko {min_risk:.2f} "
                              f"({min_risk / float(acct.balance) * 100:.1f}% akun), "
                              f"di atas batas keras {hard:.1f}%")
                return None
            lots = min_lot
        lots = min(lots, max_lot)

        margin = 0.0
        try:
            margin = float(mt5.order_calc_margin(order_type, full, lots, price) or 0)
        except Exception:
            margin = 0.0
        if margin and margin > float(acct.margin_free) * 0.8:
            self.error = (f"margin tidak cukup untuk {lots} lot {full} "
                          f"(butuh {margin:.2f}, bebas {acct.margin_free:.2f})")
            return None

        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": full, "volume": lots,
            "type": order_type, "price": price, "sl": sl, "tp": tp, "deviation": 20,
            "magic": self.magic, "comment": f"{strategy}:{reason[:20]}",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            code = getattr(res, "retcode", None)
            if code == 10027:
                self.error = ("AutoTrading di terminal MT5 masih OFF — klik tombol "
                              "'Algo Trading' di toolbar MT5")
            else:
                self.error = f"order_send failed: {code} {getattr(res, 'comment', '')}"
            return None
        self.error = ""
        return {"id": int(res.order), "symbol": symbol, "side": side, "lots": lots,
                "entry": price, "sl": sl, "tp": tp, "retcode": int(res.retcode)}

    def close(self, pid: int, price: float, reason: str) -> Optional[dict]:
        if not self.available:
            return None
        pos = self.mt5.positions_get(ticket=pid)
        if not pos:
            return None
        p = pos[0]
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
            "type": self.mt5.ORDER_TYPE_SELL if p.type == 0 else self.mt5.ORDER_TYPE_BUY,
            "position": pid, "price": self.mt5.symbol_info_tick(p.symbol).bid if p.type == 0
            else self.mt5.symbol_info_tick(p.symbol).ask,
            "deviation": 20, "magic": self.magic, "comment": reason[:20],
            "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        res = self.mt5.order_send(req)
        return {"id": pid, "exit_reason": reason, "retcode": getattr(res, "retcode", None)}

    def close_all(self, prices: Dict[str, float], reason: str = "FLATTEN") -> List[dict]:
        out = []
        for p in self.mt5.positions_get() or []:
            if int(getattr(p, "magic", 0)) != self.magic:
                continue
            r = self.close(int(p.ticket), float(p.price_current), reason)
            if r:
                out.append({"id": int(p.ticket), "symbol": p.symbol,
                            "side": "buy" if p.type == 0 else "sell",
                            "pnl": round(float(p.profit), 2), "exit_reason": reason,
                            "r": 0.0, "lots": round(float(p.volume), 2)})
        return out

    def mark(self, prices: Dict[str, float], ranges=None) -> List[dict]:
        """SL/TP are held server-side at the broker, so nothing to simulate."""
        return []

    def heat(self) -> float:
        return 0.0

    def to_state(self) -> dict:
        return {}

    def restore(self, st: dict) -> int:
        return 0


class RouterBroker:
    """Paper book by default, real MT5 orders whenever the terminal is live.

    This is what makes "keep trading even when MT5 is closed" true: the agent
    always has an execution venue. When the terminal is down it keeps working on
    a shadow book seeded with the last live balance; when the terminal comes back
    the shadow book is flattened and routing moves to the broker.
    """

    def __init__(self, paper: "PaperBroker", link, factory) -> None:
        self.paper = paper
        self.link = link
        self._factory = factory
        self._live: Optional["MT5Broker"] = None
        self.shadow_seeded = False
        self.last_error = ""

    # ------------------------------------------------------------------ state
    @property
    def live(self) -> bool:
        return bool(self.link is not None and self.link.live and self.link.trade_enabled)

    def _ensure_live(self) -> Optional["MT5Broker"]:
        if self._live is not None and self._live.available:
            return self._live
        self._live = self._factory()
        return self._live if self._live and self._live.available else None

    def account(self) -> dict:
        if self.link is not None and self.link.live:
            acc = self.link.account()
            if acc:
                return acc
        if not self.shadow_seeded and self.link is not None:
            last = self.link.last_account()
            if last:
                # keep the numbers continuous instead of jumping to the paper default
                self.paper.balance = float(last.get("balance", self.paper.balance))
                self.paper.equity = float(last.get("equity", self.paper.equity))
                self.shadow_seeded = True
        acc = self.paper.account()
        if self.link is not None:
            acc = dict(acc)
            acc["mode"] = "PAPER-SHADOW"
            acc["shadow_of"] = "MT5"
        return acc

    def open_positions(self) -> List[dict]:
        if self.link is not None and self.link.live:
            return self.link.positions()
        return self.paper.open_positions()

    # ------------------------------------------------------------------ orders
    def open(self, symbol: str, side: str, price: float, sl: float, tp: float,
             strategy: str, reason: str, bar_t: int = 0):
        if self.live:
            lb = self._ensure_live()
            if lb is not None:
                res = lb.open(symbol, side, price, sl, tp, strategy, reason, bar_t)
                self.last_error = lb.error
                return res
            self.last_error = "MT5 broker tidak siap"
            return None
        return self.paper.open(symbol, side, price, sl, tp, strategy, reason, bar_t)

    def close(self, pid: int, price: float, reason: str):
        if self.live:
            lb = self._ensure_live()
            return lb.close(pid, price, reason) if lb else None
        return self.paper.close(pid, price, reason)

    def close_all(self, prices: Dict[str, float], reason: str = "FLATTEN") -> List[dict]:
        if self.live:
            lb = self._ensure_live()
            return lb.close_all(prices, reason) if lb else []
        return self.paper.close_all(prices, reason)

    def mark(self, prices: Dict[str, float], ranges=None) -> List[dict]:
        if self.live:
            if self.paper.positions:
                # shadow book is obsolete the moment real routing takes over
                closed = self.paper.close_all(prices, "SWITCH_TO_MT5")
                self.paper.positions.clear()
                return closed
            return []
        return self.paper.mark(prices, ranges)

    def heat(self) -> float:
        if self.live:
            return 0.0
        return self.paper.heat()

    def to_state(self) -> dict:
        return {} if self.live else self.paper.to_state()

    def restore(self, st: dict) -> int:
        return 0 if self.live else self.paper.restore(st)


def build_broker(link=None):
    """Return (broker, mode, note).

    Live MT5 PnL is handled by ``trading.mt5link.MT5Link`` on its own thread, so
    attaching here would block boot for up to 60s while MetaTrader is closed.
    ``trade_enabled`` turns on real order routing; the RouterBroker then decides
    per order whether it can reach the terminal.
    """
    m = cfg("mt5", {})
    paper = PaperBroker(float(cfg("trading.initial_balance", 100000.0)),
                        float(cfg("trading.risk_per_trade_pct", 0.5)))
    if m.get("enabled") and m.get("trade_enabled") and link is not None:
        def factory() -> MT5Broker:
            return MT5Broker(m.get("terminal_path", ""), m.get("login", 0),
                             m.get("password", ""), m.get("server", ""),
                             m.get("symbol_suffix", ""), m.get("magic", 771001))

        return (RouterBroker(paper, link, factory), "MT5-LIVE",
                "Eksekusi otomatis: order nyata dikirim ke MT5 saat terminal hidup, "
                "shadow book saat terminal tertutup")
    if m.get("enabled"):
        return paper, "PAPER", ("MT5 feed aktif (read-only) - sinyal saja, "
                                "set mt5.trade_enabled=true untuk eksekusi otomatis")
    return paper, "PAPER", "Paper broker (set mt5.enabled=true untuk feed akun MT5)"
