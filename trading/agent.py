"""ATLAS - the trading agent.

Responsibilities
----------------
1. Validate every (strategy, instrument) pair through the backtest gate.
2. Trade only pairs that cleared the gate - a strategy that failed or is still
   on probation is never allowed to touch the book.
3. Size every position from risk, enforce portfolio heat, daily loss limit and
   a kill switch.
4. Publish live account/PnL state for the dashboard.
"""
from __future__ import annotations

import functools
import threading
import time
from collections import Counter, deque
from typing import Deque, Dict, List, Optional, Tuple

from core.config import INSTRUMENTS, cfg, register_instrument
from core.store import STORE
from trading import backtest as bt
from trading.broker import PaperBroker, build_broker
from trading.datafeed import MarketData
from trading.mt5link import MT5Link
from trading.strategies import STRATEGY_CLASSES, build_ctx, make_strategy

Pair = Tuple[str, str]  # (strategy_key, symbol)
TRADEABLE = ("SIM-VALIDATED", "LIVE-ELIGIBLE")


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class TradingAgent:
    def __init__(self, security=None) -> None:
        self._lock = threading.RLock()
        self.security = security
        self.symbols: List[str] = list(INSTRUMENTS.keys())
        self.md = MarketData(self.symbols, bars=3200)
        # live MT5 account feed (read-only unless mt5.trade_enabled=true)
        self.mt5: Optional[MT5Link] = MT5Link() if cfg("mt5.enabled", False) else None
        self._mt5_seeded = False
        self.broker, self.mode, self.mode_note = build_broker(self.mt5)
        self.broker_symbols: Dict[str, str] = {}   # configured -> broker name
        self.universe_note = ""
        self.real_data = False
        self.blocked_by: Dict[str, str] = {}   # symbol -> why no entry was opened
        self.history_bars = int(cfg("mt5.history_bars", 3000))
        self._last_reject: Dict[Tuple[str, str], Tuple[str, int]] = {}
        # every instrument the broker quotes is analysed; only the classes below
        # may hold a position at the same time (0 = analysed, never traded)
        self.catalog: List[dict] = []
        self.catalog_names: List[str] = []
        self.by_class: Dict[str, int] = {}
        self.watch: List[str] = []             # symbols whose pair cleared the gate
        self.watch_max = int(cfg("trading.watch_max", 16))
        self.lot_override: Dict[str, float] = {}
        self.timeframes: Dict[str, str] = {}
        self._last_analyzed_at = 0.0
        # per-asset-class concurrency caps: 3 forex, 1 commodity by default
        self.max_per_class: Dict[str, int] = {
            str(k).upper(): int(v) for k, v in
            (cfg("trading.max_per_class", {}) or {"FOREX": 3, "COMMODITY": 1}).items()
        }
        for klass, lots in (cfg("trading.lot_override", {}) or {}).items():
            try:
                v = float(lots or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                self.lot_override[str(klass).upper()] = v
        self.risk_pct = float(cfg("trading.risk_per_trade_pct", 0.5))
        self.max_open = int(cfg("trading.max_open_positions", 6))
        self.max_heat = float(cfg("trading.max_portfolio_heat_pct", 4.0))
        self.daily_loss_limit = float(cfg("trading.daily_loss_limit_pct", 3.0))

        self.registry: Dict[Pair, bt.Result] = {}
        self.instances: Dict[Pair, object] = {}
        self.ctxs: Dict[str, object] = {}
        self.logs: Deque[dict] = deque(maxlen=90)
        self.signals: Deque[dict] = deque(maxlen=14)
        self.closed_log: Deque[dict] = deque(maxlen=24)
        self.equity_hist: Deque[List[float]] = deque(maxlen=260)
        self.pnl_hist: Deque[List[float]] = deque(maxlen=260)

        self.validating = False
        self.validate_progress = 0.0
        self._last_entry_bar: Dict[Tuple[str, str], int] = {}
        self.paused = False
        self.killed = False
        self.halt_reason = ""
        self.last_cycle_ts = 0.0
        self.error_count = 0
        self.started_at = time.time()
        self.last_bar_at = 0.0
        self.ticks = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.day_start_balance = float(self.broker.account().get("balance", 0) or 0)
        self._day = time.gmtime().tm_yday

        self.log("BOOT", f"Trading agent online. Execution mode: {self.mode}")
        self.log("BOOT", self.mode_note)
        if self.mt5 is not None:
            self.log("MT5", "Menunggu terminal MetaTrader 5 — buka MT5 dan login, "
                            "sistem akan menyambung sendiri tanpa restart")
        self._sync_universe()
        self.log("BOOT", f"Universe {len(self.symbols)} instrumen: {', '.join(self.symbols)}")
        self._restore()
        for sym in self.symbols:
            self.ctxs[sym] = build_ctx(sym, self.md.series[sym], self.md.factors)

    # ------------------------------------------------------------------ utils
    def log(self, level: str, msg: str) -> None:
        self.logs.appendleft({"ts": int(time.time() * 1000), "level": level, "msg": msg})

    def current_mode(self) -> Tuple[str, str]:
        """(mode, note) - what is actually executing right now."""
        live = self.mt5 is not None and self.mt5.live
        if live:
            acc = self.mt5.account() or {}
            who = f"{acc.get('login', '?')}@{acc.get('server', '?')}"
            if self.mt5.trade_enabled:
                return "MT5-LIVE", (f"Akun MT5 {who} — eksekusi otomatis ke broker, "
                                    f"PnL {acc.get('currency', '')}")
            return "MT5-FEED", (f"Akun MT5 {who} — PnL live, eksekusi tetap manual "
                                f"(trade_enabled=false)")
        if self.mt5 is not None:
            st = self.mt5.snapshot()
            last = self.mt5.last_account() or {}
            who = f"akun {last.get('login', '?')}" if last else "belum pernah tersambung"
            return "PAPER-SHADOW", (f"MT5 tertutup ({st['state']}) — shadow book tetap "
                                    f"berjalan ({who}); order nyata menunggu terminal hidup")
        return self.mode, self.mode_note

    def _account(self) -> dict:
        acc = self.broker.account()
        if self.mt5 is not None and self.mt5.live and not self._mt5_seeded:
            # first contact with the live account: the curve belongs to MT5 now
            self._mt5_seeded = True
            self.day_start_balance = float(acc.get("equity", 0) or 0)
            self.equity_hist.clear()
            self.pnl_hist.clear()
            self.log("MT5", f"Akun live terdeteksi: {acc.get('login')} "
                            f"{acc.get('server')} | equity {acc.get('equity')} "
                            f"{acc.get('currency')}")
        day = time.gmtime().tm_yday
        if self._day != day:
            self._day = day
            self.day_start_balance = acc.get("equity", 0) or self.day_start_balance
            if self.paused and self.halt_reason == "DAILY_LOSS":
                # a risk halt expires with the session; a manual pause or kill does not
                self.paused = False
                self.halt_reason = ""
                self.log("RISK", "New UTC session - daily loss halt released automatically")
        acc["day_pnl"] = round(acc.get("equity", 0) - self.day_start_balance, 2)
        acc["day_pnl_pct"] = round(
            (acc.get("equity", 0) / self.day_start_balance - 1) * 100, 2
        ) if self.day_start_balance else 0.0
        acc["day_start_balance"] = round(self.day_start_balance, 2)
        return acc

    def _on_account_change(self) -> None:
        """User switched MT5 account — reset everything so the agent follows."""
        acct = (self.mt5.account() or {}) if self.mt5 else {}
        who = f"{acct.get('login', '?')}@{acct.get('server', '?')}"
        self.log("MT5", f"Akun berganti -> {who} ({acct.get('currency')}) — reset penuh")
        # seeding will be re-triggered by _account() on the next cycle
        self._mt5_seeded = False
        self.equity_hist.clear()
        self.pnl_hist.clear()
        self.closed_log.clear()
        self.signals.clear()
        # flush any shadow/paper positions that belonged to the old account
        try:
            self.broker.close_all({}, reason="ACCOUNT_CHANGE")
        except Exception:
            pass
        # force universe rebuild from the new broker catalog
        self.broker_symbols = {}
        self.catalog = []
        self.catalog_names = []
        self.real_data = False
        self.md = MarketData([], bars=3200)
        self.ctxs = {}
        self.registry = {}
        self.instances = {}
        self.watch = []
        self._sync_universe()

    # ------------------------------------------------------------------ gate
    @_locked
    def validate_pair(self, key: str, symbol: str, source: str = "") -> bt.Result:
        strat = make_strategy(key)
        ctx = self.ctxs[symbol]
        src = source or ("broker" if self.real_data else "synthetic")
        res = bt.validate(strat, ctx, source=src)
        res.generated_at = int(time.time() * 1000)
        self.registry[(key, symbol)] = res
        self.instances[(key, symbol)] = strat
        return res

    def validate_all(self) -> None:
        self.validating = True
        self.validate_progress = 0.0
        pairs = [(k, s) for k in STRATEGY_CLASSES for s in self.symbols
                 if s in self.ctxs]
        if not pairs:
            self.validating = False
            return
        cleared_log = 0
        for n, (key, sym) in enumerate(pairs, start=1):
            try:
                res = self.validate_pair(key, sym)
                if res.status in TRADEABLE:
                    cleared_log += 1
                    m = res.metrics
                    self.log("GATE", f"{key.upper()} on {sym} cleared the gate "
                                     f"(PF {m['profit_factor']:.2f}, Sharpe {m['sharpe']:.2f}, "
                                     f"DD {m['max_drawdown_pct']:.1f}%) -> {res.status}")
            except Exception as exc:  # keep one bad pair from killing the boot
                self.log("ERROR", f"validation failed for {key}/{sym}: {exc}")
            self.validate_progress = n / len(pairs)
        self.validating = False
        self._last_analyzed_at = time.time()
        live = sum(1 for r in self.registry.values() if r.status in TRADEABLE)
        self.log("GATE", f"Analisa selesai: {len(self.symbols)} instrumen, "
                         f"{len(self.registry)} pasangan diuji, {live} lolos gate, "
                         f"{cleared_log} di antaranya baru")
        self._refresh_watch()

    # ------------------------------------------------------------------ runtime
    def start(self) -> None:
        if self.mt5 is not None:
            self.mt5.start()
        threading.Thread(target=self.validate_all, daemon=True).start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.mt5 is not None:
            self.mt5.stop()

    def _loop(self) -> None:
        bar_seconds = float(cfg("system.bar_seconds", 6.0))
        tick = float(cfg("system.tick_seconds", 1.0))
        while not self._stop.is_set():
            try:
                self._cycle(bar_seconds)
            except Exception as exc:
                self.error_count += 1
                self.log("ERROR", f"cycle error: {exc}")
            self._stop.wait(tick)

    @_locked
    def _cycle(self, bar_seconds: float) -> None:
        now = time.time()
        # account switch detection — must happen before anything else
        if self.mt5 is not None and self.mt5.live and self.mt5.account_changed():
            new_id = self.mt5.ack_account_change()
            self._on_account_change()
        new_bar = now - self.last_bar_at >= bar_seconds
        if new_bar:
            self.last_bar_at = now
            if self.real_data:
                self._refresh_real()
            else:
                for sym in self.symbols:
                    self.md.next_bar(sym)
                    self.ctxs[sym] = build_ctx(sym, self.md.series[sym], self.md.factors)

        live = self._live_symbols()
        prices: Dict[str, float] = {}
        for s in live:
            live_px = self.mt5.price(s) if self.real_data and self.mt5 else None
            prices[s] = live_px if live_px else self.md.price(s)
        ranges = {}
        for s in live:
            try:
                b = self.md.last_bar(s)
            except Exception:
                continue
            ranges[s] = (b.l, b.h, b.t)

        if self.ticks % 15 == 0:
            self._sync_universe()
            if not self.validating:
                self._deepen_history()

        for closed in self.broker.mark(prices, ranges):
            self.closed_log.appendleft(closed)
            self.log("FILL", f"closed {closed.get('symbol')} "
                             f"{str(closed.get('side', '')).upper()} "
                             f"{closed.get('exit_reason')} pnl {closed.get('pnl', 0):+.2f} "
                             f"({closed.get('r', 0):+.2f}R)")

        if new_bar and not self.killed:
            self._evaluate(prices)

        acc = self._account()
        ts = int(now * 1000)
        self.equity_hist.append([ts, round(acc.get("equity", 0), 2)])
        self.pnl_hist.append([ts, round(acc.get("day_pnl", 0), 2)])
        self.ticks += 1

        self.last_cycle_ts = time.time()
        if self.security is not None and self.ticks % 5 == 0:
            self.security.tick()

    # ------------------------------------------------------------------ universe
    def _sync_universe(self) -> None:
        """Rebuild the analysis universe from every instrument the broker quotes.

        The user's rule has two halves and this is where they meet:

        * **analyse everything** - the terminal's own symbol list is the source of
          truth, so a pair the broker adds shows up without a config edit;
        * **trade almost nothing** - `trading.max_per_class` decides how many
          positions a class may hold (FOREX 3, COMMODITY 1, everything else 0),
          so a symbol can be analysed, ranked, and still never be traded.

        Instruments the broker does not offer are never registered, so they can
        never appear on the dashboard either.
        """
        if self.mt5 is None or not self.mt5.live:
            return
        cat = self.mt5.catalog()
        if not cat:
            return
        names = [r["name"] for r in cat]
        if names != self.catalog_names:
            self.catalog = cat
            self.catalog_names = names
            for row in cat:
                register_instrument(row["name"], path=row["path"], digits=row["digits"],
                                    point=row["point"], spread_points=row["spread"],
                                    name=row["description"])
            self.broker_symbols = {n: n for n in names}
            self.symbols = list(names)
            counts = Counter(INSTRUMENTS[n].klass for n in names)
            self.by_class = dict(sorted(counts.items()))
            self.universe_note = " · ".join(f"{k} {v}" for k, v in self.by_class.items())
            tradable = ", ".join(f"{k} {v}" for k, v in self.max_per_class.items() if v > 0)
            self.log("MT5", f"{len(names)} instrumen dianalisa dari broker "
                            f"({self.universe_note}) — boleh dieksekusi: {tradable}")
            self._last_analyzed_at = 0.0
        # the mapping is persisted across restarts, so a restart must still be
        # able to (re)load the real bars - otherwise the gate silently falls back
        # to synthetic data and nothing can ever be LIVE-ELIGIBLE
        if not self.real_data:
            self._load_real_bars()
        if not self.registry and not self.validating:
            threading.Thread(target=self.validate_all, daemon=True).start()

    def _load_real_bars(self) -> None:
        """Run the strategies on real broker bars, not the simulator."""
        if self.mt5 is None or not self.mt5.live:
            return
        got = 0
        for s in self.symbols:
            bars = self.mt5.bars(s, self.history_bars)
            if bars and self.md.adopt(s, bars):
                got += 1
        if not got:
            return
        self.md.build_real_factors()
        self.real_data = True
        for s in self.symbols:
            self.ctxs[s] = build_ctx(s, self.md.series[s], self.md.factors)
        self.log("MT5", f"{got} instrumen dimuat dari data broker (M15, "
                        f"~{self._depth()} bar) — gate dihitung pada data nyata")

    def _depth(self) -> int:
        ref = self.watch or self.symbols
        return min((len(self.md.series.get(s) or []) for s in ref), default=0)

    def _deepen_history(self) -> None:
        """MT5 downloads history lazily: the first attach may hand back fewer bars.

        A short series makes the 50-trade gate sample unreachable (a pair with 33
        trades can never clear it), so as soon as the terminal can serve more
        history the series is re-adopted and the gate is re-run once.
        """
        if self.mt5 is None or not self.mt5.live or not self.real_data:
            return
        want = int(self.history_bars * 0.8)
        short = [s for s in (self.watch or self.symbols)
                 if len(self.md.series.get(s) or []) < want]
        if not short:
            return
        got = 0
        for s in short:
            bars = self.mt5.bars(s, self.history_bars)
            if bars and len(bars) > len(self.md.series.get(s) or []) and self.md.adopt(s, bars):
                got += 1
        if not got:
            return
        self.md.build_real_factors()
        for s in short:
            if s in self.md.series:
                self.ctxs[s] = build_ctx(s, self.md.series[s], self.md.factors)
        self.log("MT5", f"histori diperdalam: {got} instrumen kini ~{self._depth()} bar "
                        f"— gate dihitung ulang supaya sampel 50 trade terpenuhi")
        threading.Thread(target=self.validate_all, daemon=True).start()

    def _live_symbols(self) -> List[str]:
        """Symbols that need live prices right now: watchlist + anything held."""
        out = list(self.watch)
        try:
            for p in self.broker.open_positions():
                s = self._canon(p.get("symbol"))
                if s and s not in out:
                    out.append(s)
        except Exception:
            pass
        return [s for s in out if s in self.ctxs]

    def _refresh_watch(self) -> None:
        """Watchlist = symbols with a cleared pair, best score first."""
        best: Dict[str, float] = {}
        for (key, sym), res in self.registry.items():
            if res.status not in TRADEABLE:
                continue
            inst = INSTRUMENTS.get(sym)
            if inst is None or self.max_per_class.get(inst.klass, 0) <= 0:
                continue
            best[sym] = max(best.get(sym, 0.0), res.score)
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        watch = [s for s, _ in ranked[: self.watch_max]]
        if watch == self.watch:
            return
        self.watch = watch
        for s in watch:
            if s in self.md.series:
                self.ctxs[s] = build_ctx(s, self.md.series[s], self.md.factors)
        self._last_analyzed_at = time.time()
        self.log("GATE", f"{len(watch)} instrumen masuk watchlist eksekusi: "
                         f"{', '.join(watch) or '— (tidak ada yang lolos)'}")

    def _refresh_real(self) -> None:
        """Merge the newest bars for the symbols we actually trade.

        Rebuilding contexts for 56 instruments every bar would burn the CPU for
        nothing: only the watchlist (and anything already held) needs live data.
        """
        if self.mt5 is None or not self.mt5.live or not self.real_data:
            return
        for s in self._live_symbols():
            bars = self.mt5.bars(s, 3)
            if bars and self.md.append_live(s, bars):
                self.ctxs[s] = build_ctx(s, self.md.series[s], self.md.factors)

    # ------------------------------------------------------------------ decisions
    def _heat(self) -> float:
        return self.broker.heat()

    def _open_keys(self) -> set:
        return {(p.get("strategy"), self._canon(p.get("symbol")))
                for p in self.broker.open_positions()}

    def _canon(self, sym) -> str:
        """Broker symbol name -> configured symbol (XAUUSDc -> XAUUSD).

        MT5 returns the broker's own names, so a position opened by hand on
        XAUUSDc must still count as XAUUSD for the "one entry per instrument"
        and FOREX-3 / COMMODITY-1 rules.
        """
        if not sym:
            return ""
        sym = str(sym)
        if not self.broker_symbols or sym in self.broker_symbols:
            return sym
        for cfg_sym, broker_sym in self.broker_symbols.items():
            if broker_sym == sym:
                return cfg_sym
        up = sym.upper()
        best = ""
        for cfg_sym in self.broker_symbols:
            if up.startswith(cfg_sym) and len(cfg_sym) > len(best):
                best = cfg_sym
        return best or sym

    def _evaluate(self, prices: Dict[str, float]) -> None:
        acc = self._account()
        if self.paused:
            return
        if acc.get("day_pnl_pct", 0) <= -self.daily_loss_limit:
            if not self.paused:
                self.paused = True
                self.halt_reason = "DAILY_LOSS"
                self.log("RISK", f"Daily loss limit hit ({acc['day_pnl_pct']:.2f}%). Trading halted.")
            return
        open_keys = self._open_keys()
        book = self.broker.open_positions()
        held = {self._canon(p.get("symbol")) for p in book}
        per_class: Dict[str, int] = {}
        for p in book:
            inst = INSTRUMENTS.get(self._canon(p.get("symbol")))
            if inst is not None:
                per_class[inst.klass] = per_class.get(inst.klass, 0) + 1
        candidates = [(k, v) for k, v in self.registry.items() if v.status in TRADEABLE]
        candidates.sort(key=lambda kv: kv[1].score, reverse=True)
        blocked: Dict[str, str] = {}
        for (key, sym), res in candidates:
            if len(book) >= self.max_open:
                break
            if (key, sym) in open_keys:
                continue
            if sym in held:
                blocked[sym] = "sudah ada posisi di instrumen ini (1 entry / instrumen)"
                continue  # one position per instrument: no stacked exposure
            inst = INSTRUMENTS.get(sym)
            if inst is None:
                continue
            cap = self.max_per_class.get(inst.klass, 0)
            if cap <= 0:
                blocked[sym] = f"kelas {inst.klass} hanya dianalisa, tidak diperdagangkan"
                continue
            if per_class.get(inst.klass, 0) >= cap:
                blocked[sym] = f"batas {inst.klass} {cap} sudah penuh"
                continue  # forex max 3 at a time, commodity max 1
            if sym not in prices or sym not in self.ctxs:
                continue
            ctx = self.ctxs[sym]
            strat = self.instances.get((key, sym))
            if strat is None:
                continue
            if getattr(strat, "_ctx_len", -1) != len(ctx.c):
                strat.prepare(ctx)
                strat._ctx_len = len(ctx.c)
            i = len(ctx.c) - 1
            sig = strat.signal(ctx, i)
            if sig is None:
                continue
            heat = self._heat()
            if heat + self.risk_pct > self.max_heat:
                self.signals.appendleft({"ts": int(time.time() * 1000), "strategy": key,
                                         "symbol": sym, "side": sig.side, "reason": sig.reason,
                                         "status": "SKIPPED", "note": f"heat {heat:.1f}% >= cap"})
                continue
            price = prices[sym]
            if (sig.side == "buy" and not (sig.sl < price < sig.tp)) or \
               (sig.side == "sell" and not (sig.tp < price < sig.sl)):
                continue
            bar_t = self.md.last_bar(sym).t
            if self._last_entry_bar.get((key, sym)) == bar_t:
                continue   # one entry per pair per bar: no re-entry storm after a stop-out
            if self.mt5 is not None and self.mt5.live and not self.mt5.trade_enabled:
                # live account mirrored read-only: publish the signal, never send an order
                self.signals.appendleft({"ts": int(time.time() * 1000), "strategy": key,
                                         "symbol": sym, "side": sig.side, "reason": sig.reason,
                                         "status": "SIGNAL",
                                         "note": f"MT5 feed (read-only) | SL {sig.sl:.5f} "
                                                 f"TP {sig.tp:.5f} | RR {sig.rr(price):.2f}"})
                self.log("SIGNAL", f"{key.upper()} {sig.side.upper()} {sym} @ {price:.5f} "
                                   f"— eksekusi manual (mode feed MT5)")
                open_keys.add((key, sym))   # one alert per pair per bar
                continue
            fixed = self.lot_override.get(inst.klass) or None
            pos = self.broker.open(sym, sig.side, price, sig.sl, sig.tp, key, sig.reason,
                                   bar_t, lots=fixed)
            if pos:
                self._last_entry_bar[(key, sym)] = bar_t
                open_keys.add((key, sym))
                held.add(sym)
                per_class[inst.klass] = per_class.get(inst.klass, 0) + 1
                live_order = bool(self.broker.live) if hasattr(self.broker, "live") else False
                lots = pos.get("lots") if isinstance(pos, dict) else getattr(pos, "lots", None)
                src = f"lot tetap {fixed} (DIREKTUR)" if fixed else f"risk {self.risk_pct}%"
                self.signals.appendleft({"ts": int(time.time() * 1000), "strategy": key,
                                         "symbol": sym, "side": sig.side, "reason": sig.reason,
                                         "status": "TAKEN",
                                         "note": f"{'MT5' if live_order else 'paper'} "
                                                 f"{lots if lots else ''} lot | {src} "
                                                 f"| RR {sig.rr(price):.2f}".strip()})
                self.log("SIGNAL", f"{key.upper()} {sig.side.upper()} {sym} @ {price:.5f} "
                                   f"- {sig.reason[:70]}")
                if self.security is not None:
                    self.security.audit_event("ATLAS", "ORDER_OPEN", f"{sym}",
                                              f"{key} {sig.side} rr={sig.rr(price):.2f} "
                                              f"via={'MT5' if live_order else 'PAPER'}")
            else:
                err = getattr(self.broker, "last_error", "") or ""
                if err:
                    # the same rejection repeats every bar while the cause lasts
                    # (e.g. AutoTrading OFF): record it once, remind every 2 min
                    now_ms = int(time.time() * 1000)
                    prev = self._last_reject.get((key, sym))
                    if not prev or prev[0] != err or now_ms - prev[1] > 120_000:
                        self.signals.appendleft({"ts": now_ms, "strategy": key,
                                                 "symbol": sym, "side": sig.side,
                                                 "reason": sig.reason,
                                                 "status": "REJECTED", "note": err[:110]})
                        self._last_reject[(key, sym)] = (err, now_ms)
        self.blocked_by = blocked

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        return {
            "mode": self.mode, "broker": self.broker.to_state(),
            "broker_symbols": self.broker_symbols, "symbols": self.symbols,
            "paused": self.paused, "killed": self.killed, "halt_reason": self.halt_reason,
            "day": self._day, "day_start_balance": self.day_start_balance,
            "ticks": self.ticks, "started_at": self.started_at,
            "equity_hist": [list(x) for x in self.equity_hist][-140:],
            "pnl_hist": [list(x) for x in self.pnl_hist][-140:],
            "closed_log": list(self.closed_log),
            "lot_override": dict(self.lot_override),
            "timeframes": dict(self.timeframes),
        }

    def _restore(self) -> None:
        st = STORE.load_state().get("trading") or {}
        if not st:
            return
        if st.get("broker"):
            n = self.broker.restore(st["broker"])
            if n:
                self.log("BOOT", f"Shadow book restored: {n} open position(s) re-adopted")
        # the last broker-verified universe survives a restart while MT5 is closed
        saved_symbols = [s for s in (st.get("symbols") or []) if s in INSTRUMENTS]
        if saved_symbols:
            self.symbols = saved_symbols
        self.broker_symbols = dict(st.get("broker_symbols") or {})
        self.paused = bool(st.get("paused", False))
        self.killed = bool(st.get("killed", False))
        self.halt_reason = st.get("halt_reason", "")
        self._day = int(st.get("day", self._day) or self._day)
        self.day_start_balance = float(st.get("day_start_balance", self.day_start_balance) or 0)
        self.ticks = int(st.get("ticks", 0) or 0)
        self.started_at = float(st.get("started_at", self.started_at) or time.time())
        for ts, v in st.get("equity_hist") or []:
            self.equity_hist.append([ts, v])
        for ts, v in st.get("pnl_hist") or []:
            self.pnl_hist.append([ts, v])
        for rec in (st.get("closed_log") or [])[:24]:
            self.closed_log.append(rec)
        self.lot_override = dict(st.get("lot_override") or {})
        self.timeframes = dict(st.get("timeframes") or {})
        if self.killed:
            self.log("BOOT", "Kill switch was active before restart - entries stay blocked")

    # ------------------------------------------------------------------ controls
    @_locked
    def control(self, action: str, actor: str = "ADMIN") -> dict:
        if action == "pause":
            self.paused = True
            self.halt_reason = "MANUAL"
            self.log("CTRL", f"Trading paused by {actor}")
        elif action == "resume":
            self.paused = False
            self.killed = False
            self.halt_reason = ""
            self.log("CTRL", f"Trading resumed by {actor}")
        elif action == "kill":
            self.killed = True
            self.paused = True
            self.halt_reason = "KILL"
            prices = {s: self.md.price(s) for s in self.symbols}
            for c in self.broker.close_all(prices, "KILL_SWITCH"):
                self.closed_log.appendleft(c)
            self.log("CTRL", f"KILL SWITCH by {actor}. All positions flattened, entries blocked.")
        elif action == "revalidate":
            threading.Thread(target=self.validate_all, daemon=True).start()
            self.log("CTRL", f"Re-validation triggered by {actor}")
        else:
            return {"ok": False, "error": "unknown action"}
        if self.security is not None:
            self.security.audit_event(actor, f"TRADING_{action.upper()}", "atlas", "operator command")
        return {"ok": True, "paused": self.paused, "killed": self.killed}

    @_locked
    def set_lot_override(self, klass: str, lots: Optional[float]) -> dict:
        if lots is None or lots <= 0:
            self.lot_override.pop(klass, None)
            self.log("CONTROL", f"Lot override untuk {klass} dihapus")
            return {"klass": klass, "lots": None, "active": False}
        self.lot_override[klass] = lots
        self.log("CONTROL", f"Lot override {klass} = {lots} lot")
        return {"klass": klass, "lots": lots, "active": True}

    @_locked
    def set_timeframe(self, klass: str, tf: str) -> dict:
        if not tf:
            self.timeframes.pop(klass, None)
            self.log("CONTROL", f"Timeframe override untuk {klass} dihapus")
            return {"klass": klass, "timeframe": None, "active": False}
        self.timeframes[klass] = tf
        self.log("CONTROL", f"Timeframe {klass} = {tf}")
        return {"klass": klass, "timeframe": tf, "active": True}

    # ------------------------------------------------------------------ snapshot
    @_locked
    def snapshot(self) -> dict:
        acc = self._account()
        mt5_live = self.mt5 is not None and self.mt5.live
        positions = self.mt5.positions() if mt5_live else self.broker.open_positions()
        mode, mode_note = self.current_mode()
        groups = []
        for key, cls in STRATEGY_CLASSES.items():
            rows = []
            for sym in self.symbols:
                res = self.registry.get((key, sym))
                if res is None:
                    rows.append({"symbol": sym, "klass": INSTRUMENTS[sym].klass,
                                 "status": "PENDING", "score": 0, "metrics": {}, "checks": []})
                    continue
                m = res.metrics
                rows.append({
                    "symbol": sym, "klass": INSTRUMENTS[sym].klass, "status": res.status,
                    "score": round(res.score, 1),
                    "metrics": {
                        "pf": round(m.get("profit_factor", 0), 2),
                        "sharpe": round(m.get("sharpe", 0), 2),
                        "dd": round(m.get("max_drawdown_pct", 0), 1),
                        "trades": int(m.get("trades", 0)),
                        "win_rate": round(m.get("win_rate", 0), 1),
                        "expectancy": round(m.get("expectancy_r", 0), 2),
                        "return_pct": round(m.get("return_pct", 0), 2),
                    },
                    "checks": [{"key": c["key"], "pass": c["pass"], "label": c["label"],
                                "value": round(c["value"], 2), "threshold": c["threshold"]}
                               for c in res.checks],
                    "folds": res.folds,
                    "mc": {k: round(v, 3) for k, v in res.monte_carlo.items()},
                    "tradeable": res.status in TRADEABLE,
                    "generated_at": res.generated_at,
                })
            groups.append({"key": key, "label": cls.label if hasattr(cls, "label") else key,
                           "family": getattr(cls, "family", ""), "symbols": rows})

        total = len(self.registry)
        cleared = sum(1 for r in self.registry.values() if r.status in TRADEABLE)
        return {
            "agent": {
                "name": "ATLAS", "role": "Trading Agent",
                "status": "HALTED" if self.killed else ("PAUSED" if self.paused else "TRADING"),
                "mode": mode, "mode_note": mode_note,
                "validating": self.validating, "validate_progress": round(self.validate_progress, 2),
                "paused": self.paused, "killed": self.killed,
                "uptime_s": int(time.time() - self.started_at), "ticks": self.ticks,
                "risk_pct": self.risk_pct, "max_open": self.max_open,
                "heat_pct": round(self._heat(), 2), "max_heat_pct": self.max_heat,
                "daily_loss_limit_pct": self.daily_loss_limit,
                "pairs_total": total, "pairs_cleared": cleared,
                "pairs_rejected": total - cleared,
                "data_source": ("mt5-live" if (mt5_live and self.real_data)
                                else "synthetic-sim" if total else "n/a"),
                "history_bars": self.history_bars,
                "history_depth": self._depth() if self.real_data else 0,
                "universe": self.symbols,
                "universe_note": self.universe_note,
                "max_per_class": self.max_per_class,
                "broker_map": self.broker_symbols,
                "blocked_by": self.blocked_by,
                "lot_override": self.lot_override,
                "timeframes": self.timeframes,
            },
            "mt5": self.mt5.snapshot() if self.mt5 is not None else None,
            "account_source": "MT5" if mt5_live else "PAPER",
            "account": acc,
            "positions": positions,
            "strategies": groups,
            "signals": list(self.signals),
            "trades": list(self.closed_log)[:14],
            "equity": [list(x) for x in self.equity_hist],
            "pnl": [list(x) for x in self.pnl_hist],
            "log": list(self.logs)[:22],
        }
