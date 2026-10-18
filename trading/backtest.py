"""Backtest engine, robustness tests and the promotion gate.

Nothing reaches the live book by passing a pretty equity curve. A strategy must
clear every gate check on the validation data set, survive a 4-fold walk-forward
stability test and a 2000-path Monte Carlo bootstrap before its status becomes
``VALIDATED``.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.config import cfg
from trading.strategies import BaseStrategy, Ctx


@dataclass
class Trade:
    strategy: str
    symbol: str
    side: str
    entry: float
    exit: float
    sl: float
    tp: float
    r: float
    pnl: float
    open_t: int
    close_t: int
    bars_held: int
    reason: str
    tag: str
    exit_reason: str

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy, "symbol": self.symbol, "side": self.side,
            "entry": round(self.entry, 6), "exit": round(self.exit, 6),
            "sl": round(self.sl, 6), "tp": round(self.tp, 6),
            "r": round(self.r, 3), "pnl": round(self.pnl, 2),
            "open_t": self.open_t, "close_t": self.close_t,
            "bars": self.bars_held, "reason": self.reason, "tag": self.tag,
            "exit_reason": self.exit_reason,
        }


@dataclass
class Result:
    strategy: str
    symbol: str
    source: str
    trades: List[Trade] = field(default_factory=list)
    equity: List[List[float]] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    folds: List[dict] = field(default_factory=list)
    monte_carlo: Dict[str, float] = field(default_factory=dict)
    checks: List[dict] = field(default_factory=list)
    score: float = 0.0
    sampled_from: int = 0
    sample_size: int = 0
    status: str = "FAILED"
    generated_at: int = 0

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy, "symbol": self.symbol, "source": self.source,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "folds": self.folds, "monte_carlo": {k: round(v, 4) for k, v in self.monte_carlo.items()},
            "checks": self.checks, "score": round(self.score, 1), "status": self.status,
            "equity": self.equity[-400:],
            "trades": [t.as_dict() for t in self.trades[-60:]],
            "trade_count": len(self.trades),
            "generated_at": self.generated_at,
        }


def _max_drawdown(equity: List[float]) -> float:
    peak = equity[0] if equity else 0.0
    dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak * 100.0)
    return dd


def _sharpe(rs: List[float], bars_span: int, bars_per_year: int = 24000) -> float:
    if len(rs) < 5:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    trades_per_year = len(rs) / max(bars_span, 1) * bars_per_year
    return mean / sd * math.sqrt(max(trades_per_year, 1.0))


def run_backtest(strategy: BaseStrategy, ctx: Ctx, source: str = "synthetic",
                 risk_pct: Optional[float] = None, balance: Optional[float] = None) -> Result:
    risk_pct = risk_pct if risk_pct is not None else float(cfg("trading.risk_per_trade_pct", 0.5))
    strategy.prepare(ctx)
    equity_now = balance if balance is not None else float(cfg("trading.initial_balance", 100000.0))
    start_balance = equity_now
    inst = ctx.inst
    n = len(ctx.c)
    res = Result(strategy=strategy.key, symbol=ctx.symbol, source=source)
    equity_curve: List[float] = [equity_now]

    i = 60
    while i < n - 2:
        sig = strategy.signal(ctx, i)
        if sig is None:
            i += 1
            continue
        a = ctx.atr_at(i)
        entry = ctx.o[i + 1] + (a * 0.04 if sig.side == "buy" else -a * 0.04)
        risk_dist = abs(entry - sig.sl)
        if risk_dist <= 1e-9 or sig.rr(entry) < 1.0:
            i += 1
            continue
        risk_amount = equity_now * risk_pct / 100.0
        units = risk_amount / risk_dist
        cost = inst.cost_pips * inst.pip * units

        exit_price = None
        exit_reason = "TIME"
        bars_held = 0
        max_bars = 120
        for j in range(i + 2, min(i + 2 + max_bars, n)):
            bars_held = j - i
            if sig.side == "buy":
                if ctx.l[j] <= sig.sl:
                    exit_price, exit_reason = sig.sl, "SL"
                    break
                if ctx.h[j] >= sig.tp:
                    exit_price, exit_reason = sig.tp, "TP"
                    break
            else:
                if ctx.h[j] >= sig.sl:
                    exit_price, exit_reason = sig.sl, "SL"
                    break
                if ctx.l[j] <= sig.tp:
                    exit_price, exit_reason = sig.tp, "TP"
                    break
        if exit_price is None:
            j = min(i + 2 + max_bars, n) - 1
            exit_price = ctx.c[j]
            bars_held = j - i

        direction = 1.0 if sig.side == "buy" else -1.0
        pnl = (exit_price - entry) * units * direction - cost
        r = pnl / risk_amount if risk_amount else 0.0
        equity_now += pnl
        equity_curve.append(equity_now)
        res.trades.append(Trade(strategy.key, ctx.symbol, sig.side, entry, exit_price, sig.sl, sig.tp,
                                r, pnl, ctx.bars[i + 1].t, ctx.bars[min(i + 1 + bars_held, n - 1)].t,
                                bars_held, sig.reason, sig.tag, exit_reason))
        res.equity.append([ctx.bars[i + 1].t, round(equity_now, 2)])
        i = i + 1 + bars_held  # no pyramiding: one position per strategy/instrument

    # ---- keep the 50 most relevant trades: the most recent ones, because they
    # describe the regime the strategy would actually face next. Everything the
    # gate reports (win rate, PF, folds, Monte Carlo) is computed on this sample.
    sample_n = int(cfg("gate.sample_trades", 50) or 0)
    total_found = len(res.trades)
    if sample_n and total_found > sample_n:
        res.trades = res.trades[-sample_n:]
        equity_now = start_balance
        equity_curve = [equity_now]
        res.equity = []
        for t in res.trades:
            equity_now += t.pnl
            equity_curve.append(equity_now)
            res.equity.append([t.close_t, round(equity_now, 2)])
        res.sampled_from = total_found
        res.sample_size = sample_n

    trades = res.trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    rs = [t.r for t in trades]
    span = (ctx.bars[-1].t - ctx.bars[0].t) / 900 if len(ctx.bars) > 1 else 1

    res.metrics = {
        "trades": float(len(trades)),
        "sample_of": float(total_found),
        "win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "net_profit": equity_now - start_balance,
        "return_pct": (equity_now / start_balance - 1.0) * 100.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (gross_win and 99.0) or 0.0,
        "expectancy_r": (sum(rs) / len(rs)) if rs else 0.0,
        "avg_win_r": (sum(t.r for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_r": (sum(t.r for t in losses) / len(losses)) if losses else 0.0,
        "max_drawdown_pct": _max_drawdown(equity_curve),
        "sharpe": _sharpe(rs, int(span)),
        "avg_bars": (sum(t.bars_held for t in trades) / len(trades)) if trades else 0.0,
        "final_equity": equity_now,
    }

    # ---- walk-forward stability: 4 chronological folds of realised trades
    if len(trades) >= 8:
        per = max(1, len(trades) // 4)
        for k in range(4):
            chunk = trades[k * per : (k + 1) * per] if k < 3 else trades[3 * per :]
            if not chunk:
                continue
            gw = sum(t.pnl for t in chunk if t.pnl > 0)
            gl = abs(sum(t.pnl for t in chunk if t.pnl <= 0))
            res.folds.append({
                "fold": k + 1,
                "trades": len(chunk),
                "net_r": round(sum(t.r for t in chunk), 3),
                "profit_factor": round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else 0.0),
                "win_rate": round(len([t for t in chunk if t.pnl > 0]) / len(chunk) * 100, 1),
            })

    # ---- Monte Carlo bootstrap of the R sequence
    if len(rs) >= 10:
        rng = random.Random(7717 + len(rs))
        totals: List[float] = []
        dds: List[float] = []
        for _ in range(2000):
            sample = [rs[rng.randrange(len(rs))] for _ in range(len(rs))]
            eq = 1.0
            peak = 1.0
            worst = 0.0
            for r in sample:
                eq += r * 0.005  # each trade risks 0.5% of equity
                peak = max(peak, eq)
                worst = max(worst, (peak - eq) / peak * 100.0)
            totals.append(sum(sample))
            dds.append(worst)
        totals.sort()
        dds.sort()
        res.monte_carlo = {
            "prob_profit": sum(1 for t in totals if t > 0) / len(totals),
            "median_total_r": totals[len(totals) // 2],
            "p05_total_r": totals[int(len(totals) * 0.05)],
            "p95_max_dd_pct": dds[int(len(dds) * 0.95)],
            "paths": float(len(totals)),
        }
    return res


def evaluate_gate(res: Result) -> Result:
    """Apply every promotion rule. A single failed check keeps the strategy in sim."""
    g = cfg("gate", {})
    m = res.metrics
    passing_folds = sum(1 for f in res.folds if f["net_r"] > 0 and f["profit_factor"] > 1.0)
    checks = [
        {"key": "trades", "label": "50 trade terakhir (paling relevan)",
         "value": m.get("trades", 0),
         "threshold": g.get("min_trades", 50), "unit": "trades", "cmp": ">="},
        {"key": "profit_factor", "label": "Profit factor", "value": m.get("profit_factor", 0),
         "threshold": g.get("min_profit_factor", 1.25), "unit": "x", "cmp": ">="},
        {"key": "sharpe", "label": "Sharpe (annualised)", "value": m.get("sharpe", 0),
         "threshold": g.get("min_sharpe", 1.0), "unit": "", "cmp": ">="},
        {"key": "max_dd", "label": "Max drawdown", "value": m.get("max_drawdown_pct", 100),
         "threshold": g.get("max_drawdown_pct", 18.0), "unit": "%", "cmp": "<="},
        {"key": "win_rate", "label": "Win rate", "value": m.get("win_rate", 0),
         "threshold": g.get("min_win_rate", 38.0), "unit": "%", "cmp": ">="},
        {"key": "expectancy", "label": "Expectancy", "value": m.get("expectancy_r", 0),
         "threshold": 0.05, "unit": "R", "cmp": ">="},
        {"key": "oos_folds", "label": "Walk-forward folds passed", "value": passing_folds,
         "threshold": g.get("min_oos_folds_passing", 3), "unit": "/4", "cmp": ">="},
        {"key": "monte_carlo", "label": "Monte Carlo P(profit)", "value": res.monte_carlo.get("prob_profit", 0),
         "threshold": g.get("min_mc_probability", 0.90), "unit": "", "cmp": ">="},
    ]
    for ch in checks:
        v, t = ch["value"], ch["threshold"]
        ch["pass"] = (v >= t) if ch["cmp"] == ">=" else (v <= t)
    res.checks = checks
    res.score = sum(1 for c in checks if c["pass"]) / len(checks) * 100.0
    all_pass = all(c["pass"] for c in checks)
    if all_pass:
        res.status = "SIM-VALIDATED" if res.source == "synthetic" else "LIVE-ELIGIBLE"
    elif res.score >= 62:
        res.status = "PROBATION"
    else:
        res.status = "FAILED"
    return res


def validate(strategy: BaseStrategy, ctx: Ctx, source: str = "synthetic") -> Result:
    res = run_backtest(strategy, ctx, source=source)
    return evaluate_gate(res)
