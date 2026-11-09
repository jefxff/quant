"""共享执行内核：回测与模拟盘**同一份**撮合 / 估值 / 下单逻辑。

T-003 每个交易日固定事件顺序：
  1. 撮合所有 <= 当日的 pending 订单（T+1 开盘价）
  2. 按当日收盘价 mark-to-market，写权益
  3. 生成 T 日信号（仅用截至 T 日数据）
  4. 风控调整
  5. 目标权重 -> pending 订单（股数按 T 日收盘价估算）

回测 / 模拟盘只传入本地 SQLite 的 bars，本模块从不发起网络请求（R-004）。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

from ..risk.position import RiskStateMachine, apply_risk, drawdown
from ..strategy.base import Strategy, StrategyContext, to_qfq
from .costs import compute_fill_costs, max_affordable_shares
from .rules import decide_fill, decide_suspension, is_suspended, round_lot, target_shares

log = logging.getLogger("quant.execution")

__all__ = ["Bar", "Position", "PendingOrder", "ExecutionContext"]


@dataclass
class Bar:
    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    adj_factor: float | None
    prev_close: float
    source: str = ""

    @classmethod
    def from_row(cls, row: Any, prev_close: float | None) -> "Bar":
        adj = row["adj_factor"] if "adj_factor" in row else None
        try:
            adj = None if adj is None or pd.isna(adj) else float(adj)
        except (TypeError, ValueError):
            adj = None
        return cls(
            symbol=str(row["symbol"]), date=str(row["date"])[:10],
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]), amount=float(row["amount"]),
            adj_factor=adj,
            prev_close=float(prev_close) if prev_close is not None else float(row["open"]),
            source=str(row.get("source", "")),
        )


@dataclass
class Position:
    qty: int = 0
    avg_cost: float = 0.0
    last_price: float = 0.0
    last_price_date: str = ""
    available_qty: int = 0


@dataclass
class PendingOrder:
    order_id: str
    trade_date: str
    strategy: str
    symbol: str
    side: str
    qty: int
    valid_from: str | None
    status: str = "pending"
    reason: str = ""
    attempts: int = 0
    est_price: float | None = None
    created_at: str = ""
    order_type: str = "MKT_OPEN_NEXT"

    def as_record(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "trade_date": self.trade_date,
            "strategy": self.strategy, "symbol": self.symbol, "side": self.side,
            "qty": self.qty, "order_type": self.order_type, "status": self.status,
            "reason": self.reason, "created_at": self.created_at,
            "placed_date": self.trade_date, "valid_from": self.valid_from,
            "attempts": self.attempts, "est_price": self.est_price,
        }


class ExecutionContext:
    """Stateful, deterministic, single-source-of-truth execution state."""

    def __init__(
        self,
        cfg: Any,
        strategy: Strategy,
        strategy_name: str,
        initial_cash: float,
        calendar_next: Callable[[str], str | None] | None = None,
        store: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.strategy_name = strategy_name
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.peak_equity = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.orders: list[PendingOrder] = []
        self.trades: list[dict[str, Any]] = []
        self.equity_records: list[dict[str, Any]] = []
        self.risk = RiskStateMachine(cfg)
        self.calendar_next = calendar_next
        self.store = store
        self.prev_closes: dict[str, float] = {}
        self.history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=strategy.required_history() + 5)
        )
        self.last_target_weights: dict[str, float] = {}
        self.warnings: list[str] = []
        self.messages: list[str] = []
        self.suspended_today: set[str] = set()
        self._approx_first_day = False

    # ------------------------------------------------------------------ #
    # bars
    # ------------------------------------------------------------------ #
    def load_bars(
        self, date: str, symbols: Sequence[str], page_size: int = 500, store: Any | None = None
    ) -> dict[str, Bar]:
        """R-013: paged read of ONE trading day, never a full-market frame."""
        db = store or self.store
        if db is None:
            raise RuntimeError("ExecutionContext 需要 store 才能读取行情")
        bars: dict[str, Bar] = {}
        for page in db.iter_daily_on_date(date, symbols, page_size):
            for _, row in page.iterrows():
                sym = str(row["symbol"])
                prev = self.prev_closes.get(sym)
                if prev is None and not self._approx_first_day:
                    self.warnings.append(
                        "样本首日无昨收价：涨跌停价以当日开盘价近似（交易规则近似 D-002）"
                    )
                    self._approx_first_day = True
                bars[sym] = Bar.from_row(row, prev)
        return bars

    def append_history(self, date: str, bars: dict[str, Bar]) -> None:
        """Step 3 input: append ONLY bars up to and including ``date``."""
        if not bars:
            return
        frame = pd.DataFrame([
            {
                "symbol": b.symbol, "date": b.date, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume,
                "amount": b.amount, "adj_factor": b.adj_factor,
            }
            for b in bars.values()
        ])
        qfq, skipped = to_qfq(frame)
        for sym in skipped:
            self.warnings.append(f"adj_factor 缺失: {date} {sym} 当日不生成信号")
        for row in qfq.to_dict("records"):
            self.history[str(row["symbol"])].append(row)

    def history_frame(self) -> pd.DataFrame:
        rows = [r for dq in self.history.values() for r in dq]
        if not rows:
            return pd.DataFrame(columns=["symbol", "date", "qfq_open", "qfq_high",
                                         "qfq_low", "qfq_close", "volume", "amount"])
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ #
    # step 1: settlement
    # ------------------------------------------------------------------ #
    def settle(self, date: str, bars: dict[str, Bar]) -> list[dict[str, Any]]:
        """撮合所有 valid_from <= date 的 pending 订单，按 T+1 开盘价。"""
        self.suspended_today = set()
        events: list[dict[str, Any]] = []
        # R-006: valid_from=None 表示「T+1 尚不在交易日历中」，绝不回退成 T 日，
        # 否则未来某次重放会在信号当日以 T 日开盘价成交（未来函数）。
        due = [
            o for o in self.orders
            if o.status == "pending" and o.valid_from is not None and o.valid_from <= date
        ]
        # 确定性顺序：先卖后买（释放现金），同向内按代码排序
        due.sort(key=lambda o: (0 if o.side == "SELL" else 1, o.symbol, o.order_id))
        for order in due:
            bar = bars.get(order.symbol)
            verdict = decide_suspension(bar, order.attempts, self.cfg)
            if verdict == "expired":
                order.status = "expired"
                order.reason = "suspended"
                order.attempts += 1
                events.append({"date": date, "order_id": order.order_id, "event": "expired",
                               "reason": "suspended", "symbol": order.symbol})
                continue
            if verdict == "deferred":
                order.attempts += 1
                order.reason = "suspended"
                order.valid_from = self.calendar_next(date) if self.calendar_next else None
                self.suspended_today.add(order.symbol)
                events.append({"date": date, "order_id": order.order_id, "event": "deferred",
                               "reason": "suspended", "symbol": order.symbol,
                               "attempt": order.attempts})
                continue

            assert bar is not None
            fill = decide_fill(bar.open, order.side, bar.prev_close, self.cfg)
            if fill.blocked:
                order.status = "rejected"
                order.reason = fill.blocked
                events.append({"date": date, "order_id": order.order_id, "event": "rejected",
                               "reason": fill.blocked, "symbol": order.symbol})
                continue

            qty = round_lot(order.qty, self.cfg)
            if order.side == "BUY":
                affordable = max_affordable_shares(self.cash, bar.open, "BUY", self.cfg)
                qty = round_lot(min(qty, affordable), self.cfg)
                # T-004: T 日估算股数，T+1 按实际现金修正，不做二次调仓
                while qty > 0 and not self._can_afford(fill.price, qty, "BUY"):
                    qty -= int(self.cfg.get("rules.lot_size", 100))
            else:
                held = self.positions.get(order.symbol)
                qty = round_lot(min(qty, held.qty if held else 0), self.cfg)

            if qty <= 0:
                order.status = "rejected"
                order.reason = "insufficient_cash" if order.side == "BUY" else "no_position"
                events.append({"date": date, "order_id": order.order_id, "event": "rejected",
                               "reason": order.reason, "symbol": order.symbol})
                continue

            costs = compute_fill_costs(fill.price, bar.open, qty, order.side, self.cfg)
            notional = fill.price * qty
            if order.side == "BUY":
                total = notional + costs.commission + costs.stamp_tax
                self.cash -= total
                pos = self.positions.setdefault(order.symbol, Position())
                new_qty = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + total) / new_qty
                pos.qty = new_qty
            else:
                self.cash += notional - costs.commission - costs.stamp_tax
                pos = self.positions.get(order.symbol)
                if pos is not None:
                    pos.qty -= qty
                    if pos.qty <= 0:
                        self.positions.pop(order.symbol, None)
                    else:
                        pos.available_qty = pos.qty
            pos_now = self.positions.get(order.symbol)
            if pos_now is not None:
                pos_now.last_price = bar.close
                pos_now.last_price_date = date

            order.status = "filled"
            order.reason = ""
            trade = {
                "trade_id": f"{order.order_id}@{date}",
                "order_id": order.order_id, "fill_date": date, "symbol": order.symbol,
                "side": order.side, "qty": int(qty), "price": float(fill.price),
                "raw_open": float(bar.open), "limit_down": fill.lower, "limit_up": fill.upper,
                "commission": float(costs.commission), "stamp_tax": float(costs.stamp_tax),
                "slippage_cost": float(costs.slippage_cost),
                "fee_total": float(costs.total), "strategy": self.strategy_name,
            }
            self.trades.append(trade)
            events.append({"date": date, "order_id": order.order_id, "event": "filled",
                           "symbol": order.symbol, "qty": int(qty), "price": float(fill.price)})
        return events

    def _can_afford(self, price: float, qty: int, side: str) -> bool:
        from .costs import estimate_fee  # local import avoids a cycle in type checkers
        notional = price * qty
        return notional + estimate_fee(notional, side, self.cfg) <= self.cash + 1e-9

    # ------------------------------------------------------------------ #
    # step 2: mark-to-market
    # ------------------------------------------------------------------ #
    def mark_to_market(self, date: str, bars: dict[str, Bar]) -> dict[str, Any]:
        stale = False
        for sym, pos in self.positions.items():
            bar = bars.get(sym)
            if bar is not None and not is_suspended(bar) and bar.close > 0:
                pos.last_price = bar.close
                pos.last_price_date = date
            else:
                stale = True  # 持仓按最后有效收盘价估值（T-010）
        market_value = sum(p.qty * p.last_price for p in self.positions.values())
        equity = self.cash + market_value
        self.peak_equity = max(self.peak_equity, equity)
        dd = drawdown(self.peak_equity, equity)
        self.risk.update(dd, date)
        record = {
            "strategy": self.strategy_name, "date": date, "cash": float(self.cash),
            "market_value": float(market_value), "equity": float(equity),
            "drawdown": float(dd), "stale_price": int(stale),
            "risk_mode": self.risk.mode, "total_cap": self.risk.total_cap(),
        }
        self.equity_records.append(record)
        # T+1 可卖：今日买入的股份在收盘后变为可卖
        for pos in self.positions.values():
            pos.available_qty = pos.qty
        # 更新昨收（仅有效交易日），供 T+1 涨跌停价使用
        for sym, bar in bars.items():
            if not is_suspended(bar) and bar.close > 0:
                self.prev_closes[sym] = bar.close
        return record

    # ------------------------------------------------------------------ #
    # steps 3-4: signals + risk
    # ------------------------------------------------------------------ #
    def target_weights(self, date: str, symbols: Sequence[str], equity: float) -> dict[str, float]:
        frame = self.history_frame()
        ctx = StrategyContext(
            date=date, symbols=list(symbols), cash=self.cash, equity=equity,
            positions={s: p.qty for s, p in self.positions.items()},
        )
        raw = self.strategy.generate_signals(frame, date, symbols, ctx)
        merged = dict(self.last_target_weights)
        merged.update({str(k): float(v) for k, v in raw.items()})
        # 停牌标的保留上期权重，不新开仓
        for sym in self.suspended_today:
            merged[sym] = self.last_target_weights.get(sym, 0.0)
        adjusted = apply_risk(merged, self.cfg, self.risk, equity=equity)
        self.last_target_weights = dict(adjusted)
        return adjusted

    # ------------------------------------------------------------------ #
    # step 5: target weights -> pending orders
    # ------------------------------------------------------------------ #
    def place_orders(
        self, date: str, bars: dict[str, Bar], weights: dict[str, float],
        equity: float, *, skip_existing_dates: bool = True,
    ) -> list[PendingOrder]:
        next_day = self.calendar_next(date) if self.calendar_next else None
        created: list[PendingOrder] = []
        existing = {(o.trade_date, o.symbol, o.side) for o in self.orders} if skip_existing_dates else set()
        lot = int(self.cfg.get("rules.lot_size", 100))
        for symbol in sorted(set(list(weights) + list(self.positions))):
            bar = bars.get(symbol)
            weight = float(weights.get(symbol, 0.0))
            if bar is None or is_suspended(bar):
                continue  # 无行情/停牌：不下新单
            desired = target_shares(equity, weight, bar.close, self.cfg)
            current = self.positions[symbol].qty if symbol in self.positions else 0
            delta = desired - current
            if abs(delta) < lot:
                continue
            side = "BUY" if delta > 0 else "SELL"
            key = (date, symbol, side)
            if key in existing:
                continue  # 幂等：同日重复运行不重复下单
            qty = round_lot(abs(delta), self.cfg)
            if qty <= 0:
                continue
            order_id = f"{self.strategy_name}:{date}:{symbol}:{side}"
            order = PendingOrder(
                order_id=order_id, trade_date=date, strategy=self.strategy_name,
                symbol=symbol, side=side, qty=int(qty), valid_from=next_day,
                est_price=float(bar.close),
                created_at=f"{date}T00:00:00+00:00",
                reason="waiting_for_next_bar",
            )
            self.orders.append(order)
            existing.add(key)
            created.append(order)
        if created and next_day is None:
            self.messages.append("waiting_for_next_bar: 交易日历中无 T+1，pending 订单保留")
        elif created:
            self.messages.append(f"waiting_for_next_bar: {len(created)} 个 pending 订单等待 {next_day} 开盘")
        return created

    # ------------------------------------------------------------------ #
    # reporting helpers
    # ------------------------------------------------------------------ #
    def orders_frame(self) -> pd.DataFrame:
        cols = ["order_id", "trade_date", "strategy", "symbol", "side", "qty",
                "order_type", "status", "reason", "attempts", "valid_from", "est_price"]
        return pd.DataFrame([o.as_record() for o in self.orders], columns=cols)

    def trades_frame(self) -> pd.DataFrame:
        cols = ["trade_id", "order_id", "fill_date", "symbol", "side", "qty", "price",
                "raw_open", "limit_down", "limit_up", "commission", "stamp_tax",
                "slippage_cost", "fee_total", "strategy"]
        return pd.DataFrame(self.trades, columns=cols)

    def equity_frame(self) -> pd.DataFrame:
        cols = ["strategy", "date", "cash", "market_value", "equity", "drawdown",
                "stale_price", "risk_mode", "total_cap"]
        return pd.DataFrame(self.equity_records, columns=cols)

    def positions_frame(self) -> pd.DataFrame:
        rows = [
            {"strategy": self.strategy_name, "symbol": s, "qty": p.qty,
             "avg_cost": p.avg_cost, "last_price": p.last_price,
             "market_value": p.qty * p.last_price, "last_price_date": p.last_price_date}
            for s, p in sorted(self.positions.items())
        ]
        return pd.DataFrame(rows, columns=["strategy", "symbol", "qty", "avg_cost",
                                          "last_price", "market_value", "last_price_date"])

    def costs_frame(self) -> pd.DataFrame:
        t = self.trades_frame()
        commission = float(t["commission"].sum()) if not t.empty else 0.0
        stamp = float(t["stamp_tax"].sum()) if not t.empty else 0.0
        slip = float(t["slippage_cost"].sum()) if not t.empty else 0.0
        turnover = float((t["price"] * t["qty"]).sum()) if not t.empty else 0.0
        return pd.DataFrame([{
            "initial_cash": self.initial_cash,
            "commission": commission, "stamp_tax": stamp, "slippage_cost": slip,
            "total_cost": commission + stamp + slip,
            "cost_ratio": (commission + stamp + slip) / self.initial_cash,
            "turnover": turnover, "trades": len(t),
        }])

    def snapshot(self) -> dict[str, Any]:
        last = self.equity_records[-1] if self.equity_records else {"equity": self.initial_cash,
                                                                     "drawdown": 0.0, "cash": self.cash}
        return {
            "cash": self.cash, "peak_equity": self.peak_equity,
            "equity": last["equity"], "drawdown": last["drawdown"],
            **self.risk.snapshot(),
        }
