"""绩效指标（公式写死，禁止为了让回测好看而调参）。

夏普        : mean(daily_ret) / std(daily_ret) * sqrt(252)，无风险利率默认 0
胜率        : 已平仓交易（FIFO 配对）中盈利笔数 / 平仓笔数
换手率      : 成交额 / 日均权益
成本占比    : 总费用 / 初始资金
累计/年化   : end/start - 1 ; (end/start)^(252/N) - 1
最大回撤    : max((peak - equity) / peak)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = ["closed_trades", "compute_metrics", "segment_metrics", "metrics_frame"]


def closed_trades(trades: pd.DataFrame) -> list[dict[str, Any]]:
    """FIFO pairing of BUY/SELL into realised round trips."""
    if trades is None or trades.empty:
        return []
    df = trades.sort_values(["fill_date", "trade_id"]).copy()
    lots: dict[str, list[list[float]]] = {}
    trips: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        symbol, side = str(row.symbol), str(row.side).upper()
        qty = float(row.qty)
        fees = float(getattr(row, "commission", 0.0)) + float(getattr(row, "stamp_tax", 0.0))
        price = float(row.price)
        if side == "BUY":
            lots.setdefault(symbol, []).append([qty, qty * price + fees])
            continue
        remaining = qty
        sell_net = qty * price - fees
        cost = 0.0
        filled = 0.0
        queue = lots.setdefault(symbol, [])
        while remaining > 0 and queue:
            lot_qty, lot_cost = queue[0]
            take = min(remaining, lot_qty)
            cost += lot_cost * (take / lot_qty) if lot_qty else 0.0
            filled += take
            remaining -= take
            if take >= lot_qty - 1e-9:
                queue.pop(0)
            else:
                queue[0][0] = lot_qty - take
                queue[0][1] = lot_cost * (1 - take / lot_qty)
        if filled <= 0:
            continue
        proceeds = sell_net * (filled / qty) if qty else 0.0
        trips.append({
            "symbol": symbol, "entry_cost": cost, "exit_proceeds": proceeds,
            "pnl": proceeds - cost, "qty": filled,
            "win": int((proceeds - cost) > 0),
        })
    return trips


def compute_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
    cfg: Any,
    label: str = "full",
) -> dict[str, Any]:
    if equity is None or equity.empty:
        return {
            "segment": label, "sample_days": 0, "cum_return": 0.0, "annual_return": 0.0,
            "max_drawdown": 0.0, "sharpe": 0.0, "win_rate": 0.0, "turnover": 0.0,
            "cost_ratio": 0.0, "trades": 0, "closed_trades": 0, "start_equity": initial_cash,
            "end_equity": initial_cash,
        }
    eq = equity.sort_values("date").copy()
    values = eq["equity"].astype(float).to_numpy()
    n = len(values)
    start, end = float(values[0]), float(values[-1])
    cum = end / start - 1.0 if start > 0 else 0.0
    annual = (end / start) ** (252.0 / n) - 1.0 if n > 0 and start > 0 else 0.0
    peak = np.maximum.accumulate(values)
    max_dd = float(np.max((peak - values) / peak)) if n else 0.0
    rets = np.diff(values) / values[:-1] if n > 1 else np.array([0.0])
    rf = float(cfg.get("backtest.risk_free_rate", 0.0))
    excess = rets - rf / 252.0
    std = float(np.std(excess, ddof=1)) if len(excess) > 1 else 0.0
    sharpe = float(np.mean(excess) / std * math.sqrt(252)) if std > 1e-12 else 0.0

    t = trades if trades is not None else pd.DataFrame()
    if not t.empty and "fill_date" in t.columns and "date" in eq.columns:
        lo, hi = str(eq["date"].iloc[0]), str(eq["date"].iloc[-1])
        t = t[(t["fill_date"] >= lo) & (t["fill_date"] <= hi)]
    trips = closed_trades(t)
    win_rate = (sum(x["win"] for x in trips) / len(trips)) if trips else 0.0
    turnover_notional = float((t["price"] * t["qty"]).sum()) if not t.empty else 0.0
    turnover = turnover_notional / float(np.mean(values)) if float(np.mean(values)) > 0 else 0.0
    fees = 0.0
    if not t.empty:
        for col in ("commission", "stamp_tax", "slippage_cost"):
            if col in t.columns:
                fees += float(t[col].sum())
    return {
        "segment": label, "sample_days": n, "cum_return": float(cum),
        "annual_return": float(annual), "max_drawdown": float(max_dd),
        "sharpe": float(sharpe), "win_rate": float(win_rate),
        "turnover": float(turnover), "cost_ratio": float(fees / initial_cash) if initial_cash else 0.0,
        "trades": int(len(t)), "closed_trades": len(trips),
        "start_equity": start, "end_equity": end, "total_fees": float(fees),
    }


def segment_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    initial_cash: float,
    cfg: Any,
    oos_start: str | None,
) -> dict[str, dict[str, Any]]:
    """固定参数跑全程后**仅分段报告**；这里不重新运行策略。"""
    eq = equity if equity is not None else pd.DataFrame()
    full = compute_metrics(eq, trades, initial_cash, cfg, "full")
    if not oos_start or eq.empty:
        return {
            "full": full,
            "in_sample": {**full, "segment": "in_sample"},
            "out_of_sample": {**full, "segment": "out_of_sample", "sample_days": 0},
        }
    ins = eq[eq["date"] < oos_start]
    oos = eq[eq["date"] >= oos_start]
    return {
        "full": full,
        "in_sample": compute_metrics(ins, trades, initial_cash, cfg, "in_sample"),
        "out_of_sample": compute_metrics(oos, trades, initial_cash, cfg, "out_of_sample"),
    }


def metrics_frame(metrics: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for name, m in metrics.items():
        if not m:
            continue
        rows.append({"segment": name, **{k: v for k, v in m.items() if k != "segment"}})
    return pd.DataFrame(rows)
