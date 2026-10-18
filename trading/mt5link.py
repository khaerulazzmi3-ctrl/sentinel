"""Resilient read bridge to a locally running MetaTrader 5 terminal.

Why a separate worker thread
----------------------------
``mt5.initialize()`` blocks for up to 60 seconds when the terminal is closed
("Pipe server didn't answer"). Doing that inside the trading cycle would freeze
the dashboard, so every IPC call happens on a dedicated thread and the rest of
the system only ever reads the last good snapshot.

States
------
``OFF``        feature disabled in config
``WAITING``    package present, terminal not answering / not logged in yet
``CONNECTING`` an attach attempt is in flight
``LIVE``       account + positions are being refreshed
``ERROR``      package missing or a hard failure (retried with backoff)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from core.config import cfg


class MT5Link:
    def __init__(self) -> None:
        m = cfg("mt5", {}) or {}
        self.enabled = bool(m.get("enabled", False))
        self.path = m.get("terminal_path", "") or ""
        self.login = int(m.get("login", 0) or 0)
        self.password = m.get("password", "") or ""
        self.server = m.get("server", "") or ""
        self.suffix = m.get("symbol_suffix", "") or ""
        self.magic = int(m.get("magic", 771001))
        self.trade_enabled = bool(m.get("trade_enabled", False))
        self.poll = float(m.get("poll_seconds", 1.0) or 1.0)
        self.retry = float(m.get("retry_seconds", 15.0) or 15.0)

        self._lock = threading.RLock()
        self._mt5 = None
        self._state = "OFF" if not self.enabled else "WAITING"
        self._detail = "belum dicoba" if self.enabled else "mt5.enabled=false"
        self._acct: Optional[dict] = None
        self._pos: List[dict] = []
        self._realized = 0.0
        self._deals_day = -1
        self._last_ok = 0.0
        self._attempts = 0
        self._next_try = 0.0
        self._attached_at = 0.0
        self.trade_allowed = False
        self._last_acct_id: Optional[str] = None   # "login@server" last seen
        self._account_changed = False              # flipped on id mismatch -> agent resets
        self._map_cache: Dict[str, str] = {}
        self._map_ts = 0.0
        self._cat_cache: List[dict] = []
        self._cat_ts = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mt5-link")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._shutdown()

    def _shutdown(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
        self._mt5 = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                if self._state != "LIVE":
                    if time.time() < self._next_try:
                        continue
                    self._attach()
                else:
                    self._pull()
            except Exception as exc:
                self._fail(f"{exc.__class__.__name__}: {exc}")

    def _fail(self, detail: str) -> None:
        with self._lock:
            self._state = "WAITING"
            self._detail = detail
            self._next_try = time.time() + self.retry
            self._map_cache = {}
            self._map_ts = 0.0
            self._cat_cache = []
            self._cat_ts = 0.0
        self._shutdown()

    # ------------------------------------------------------------------ attach
    def _attach(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            with self._lock:
                self._state = "ERROR"
                self._detail = ("paket MetaTrader5 belum terpasang: "
                                "pip install MetaTrader5")
                self._next_try = time.time() + 120
            del exc
            return

        with self._lock:
            self._state = "CONNECTING"
            self._detail = "menghubungi terminal MT5…"
            self._attempts += 1

        kwargs: Dict[str, object] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=self.login, password=self.password, server=self.server)
        ok = False
        try:
            ok = bool(mt5.initialize(**kwargs))
        except Exception as exc:
            self._fail(f"initialize error: {exc.__class__.__name__}")
            return
        if not ok:
            self._fail(f"terminal tidak menjawab ({mt5.last_error()}). "
                       f"Buka MetaTrader 5 dan login, sistem akan menyambung sendiri.")
            return

        acct = mt5.account_info()
        if acct is None:
            self._fail("terminal jalan tapi belum ada akun yang login")
            return

        with self._lock:
            self._mt5 = mt5
            self._state = "LIVE"
            self._detail = "terhubung"
            self._attached_at = time.time()
            self._last_ok = time.time()
            self._deals_day = -1
        self._pull()

    # ------------------------------------------------------------------ read
    def _pull(self) -> None:
        mt5 = self._mt5
        if mt5 is None:
            return
        acct = mt5.account_info()
        if acct is None:
            self._fail("koneksi akun hilang")
            return
        self._refresh_realized()
        pos = mt5.positions_get() or []
        rows: List[dict] = []
        for p in pos:
            managed = int(getattr(p, "magic", 0)) == self.magic
            comment = (getattr(p, "comment", "") or "").strip()
            rows.append({
                "id": int(p.ticket), "symbol": p.symbol,
                "side": "buy" if p.type == 0 else "sell",
                "lots": round(float(p.volume), 2),
                "entry": float(p.price_open), "sl": float(p.sl), "tp": float(p.tp),
                "price": float(p.price_current), "pnl": round(float(p.profit), 2),
                "swap": round(float(getattr(p, "swap", 0) or 0), 2),
                "r": 0.0,
                "strategy": comment.split(":")[0] if managed and comment else "manual",
                "reason": comment or "posisi manual di terminal",
                "open_t": int(p.time), "managed": managed,
            })
        with self._lock:
            self._acct = {
                "mode": "MT5",
                "balance": round(float(acct.balance), 2),
                "equity": round(float(acct.equity), 2),
                "unrealized": round(float(acct.equity - acct.balance), 2),
                "realized_today": round(self._realized, 2),
                "margin_used": round(float(acct.margin), 2),
                "margin_free": round(float(getattr(acct, "margin_free", 0) or 0), 2),
                "currency": acct.currency, "login": int(acct.login), "server": acct.server,
                "leverage": int(getattr(acct, "leverage", 0) or 0),
            }
            self._pos = rows
            self._last_ok = time.time()
            self._state = "LIVE"
            self._detail = "terhubung"
            acct_id = f"{int(acct.login)}@{acct.server}"
            if self._last_acct_id is not None and self._last_acct_id != acct_id:
                self._account_changed = True
                self._detail = f"akun berganti {self._last_acct_id} -> {acct_id}"
            self._last_acct_id = acct_id
            try:
                term = mt5.terminal_info()
                self.trade_allowed = bool(getattr(term, "trade_allowed", False)) if term else False
            except Exception:
                self.trade_allowed = False
            if self.trade_enabled and not self.trade_allowed:
                self._detail = ("terhubung, tetapi AutoTrading OFF di terminal "
                                "(klik tombol Algo Trading)")

    def _refresh_realized(self) -> None:
        """Sum today's closed PnL once per UTC day (cheap, not per tick)."""
        day = time.gmtime().tm_yday
        if day == self._deals_day or self._mt5 is None:
            return
        self._deals_day = day
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        try:
            deals = self._mt5.history_deals_get(start, datetime.now(timezone.utc)) or []
            for d in deals:
                if int(getattr(d, "entry", 0)) == 1:      # DEAL_ENTRY_OUT
                    total += float(d.profit) + float(getattr(d, "commission", 0) or 0) \
                        + float(getattr(d, "swap", 0) or 0)
        except Exception:
            return
        with self._lock:
            self._realized = total

    # ------------------------------------------------------------------ api
    @property
    def live(self) -> bool:
        return self._state == "LIVE" and self._acct is not None

    def account(self) -> Optional[dict]:
        if not self.live:
            return None
        with self._lock:
            return dict(self._acct) if self._acct else None

    def last_account(self) -> Optional[dict]:
        """Last known account, even when the terminal is currently unreachable."""
        with self._lock:
            return dict(self._acct) if self._acct else None

    def account_changed(self) -> bool:
        """True once when the login@server pair has changed since last ack."""
        with self._lock:
            return self._account_changed

    def ack_account_change(self) -> Optional[str]:
        """Acknowledge and return the new account id (login@server)."""
        with self._lock:
            self._account_changed = False
            return self._last_acct_id

    def positions(self) -> List[dict]:
        if not self.live:
            return []
        with self._lock:
            return [dict(r) for r in self._pos]

    def symbol_map(self) -> Dict[str, str]:
        """Symbol -> real broker symbol, for every instrument the broker quotes.

        Cent/ECN accounts routinely suffix instruments (XAUUSDc, EURUSD.m), so the
        explicit suffix wins when set, otherwise the shortest broker symbol that
        starts with the base name is used. Broker names map to themselves, which
        is what makes "analyse every instrument in MT5" possible without a config
        list: the terminal is the source of truth.
        """
        if not self.live:
            return {}
        now = time.time()
        with self._lock:
            cached = self._map_cache
            fresh = now - self._map_ts < 30
        if cached and fresh:
            return dict(cached)
        out: Dict[str, str] = {}
        try:
            names = sorted((s.name for s in (self._mt5.symbols_get() or [])), key=len)
        except Exception:
            return dict(cached or {})
        for n in names:
            out.setdefault(n, n)
        for s in cfg("trading.symbols", []) or []:
            base = s.get("symbol", "")
            if not base:
                continue
            if self.suffix and (base + self.suffix) in names:
                out[base] = base + self.suffix
                continue
            for n in names:
                if n == base or n.startswith(base):
                    out[base] = n
                    break
        with self._lock:
            self._map_cache = dict(out)
            self._map_ts = now
        return out

    def catalog(self) -> List[dict]:
        """Every instrument the terminal offers, with the metadata we need.

        Used to build the analysis universe (asset class, pip size, spread, lot
        step) instead of a hard-coded symbol list.
        """
        if not self.live or self._mt5 is None:
            return []
        now = time.time()
        with self._lock:
            if self._cat_cache and now - self._cat_ts < 120:
                return [dict(r) for r in self._cat_cache]
        rows: List[dict] = []
        try:
            syms = self._mt5.symbols_get() or []
        except Exception:
            return []
        for s in syms:
            mode = int(getattr(s, "trade_mode", 4) or 0)
            if mode == 0:                      # SYMBOL_TRADE_MODE_DISABLED
                continue
            rows.append({
                "name": s.name,
                "path": getattr(s, "path", "") or "",
                "description": getattr(s, "description", "") or "",
                "digits": int(getattr(s, "digits", 5) or 5),
                "point": float(getattr(s, "point", 0) or 0),
                "spread": float(getattr(s, "spread", 0) or 0),
                "volume_min": float(getattr(s, "volume_min", 0.01) or 0.01),
                "volume_step": float(getattr(s, "volume_step", 0.01) or 0.01),
                "volume_max": float(getattr(s, "volume_max", 100.0) or 100.0),
                "contract": float(getattr(s, "trade_contract_size", 0) or 0),
                "visible": bool(getattr(s, "visible", False)),
                "trade_mode": mode,
            })
        rows.sort(key=lambda r: (r["path"], r["name"]))
        with self._lock:
            self._cat_cache = rows
            self._cat_ts = now
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ market data
    def price(self, symbol: str) -> Optional[float]:
        """Live mid price for a configured symbol (None when detached)."""
        if not self.live or self._mt5 is None:
            return None
        full = self.symbol_map().get(symbol)
        if not full:
            return None
        try:
            t = self._mt5.symbol_info_tick(full)
        except Exception:
            return None
        if t is None:
            return None
        return float((t.bid + t.ask) / 2.0) if (t.bid and t.ask) else float(t.last)

    def bars(self, symbol: str, count: int = 3000, timeframe=None):
        """Real OHLC bars from the terminal, so signals run on broker data."""
        if not self.live or self._mt5 is None:
            return None
        from trading.datafeed import Bar
        full = self.symbol_map().get(symbol) or symbol
        tf = timeframe if timeframe is not None else self._mt5.TIMEFRAME_M15
        try:
            rates = self._mt5.copy_rates_from_pos(full, tf, 0, count)
        except Exception:
            return None
        if rates is None or len(rates) == 0:
            return None
        return [Bar(int(r["time"]), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["tick_volume"]))
                for r in rates]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled, "state": self._state, "detail": self._detail,
                "terminal": self.path, "login": self.login or (self._acct or {}).get("login"),
                "server": self.server or (self._acct or {}).get("server"),
                "currency": (self._acct or {}).get("currency"),
                "trade_enabled": self.trade_enabled, "trade_allowed": self.trade_allowed,
                "positions": len(self._pos), "attempts": self._attempts,
                "last_ok_age_s": round(time.time() - self._last_ok, 1) if self._last_ok else None,
                "attached_for_s": round(time.time() - self._attached_at) if self._attached_at else None,
            }
