"""每日模拟盘：收盘后生成信号 -> 下一交易日开盘按规则模拟成交。

* 只写本地 SQLite，绝不发送真实委托（R-007）。
* ``--date latest`` 语义：先结算所有 <= latest 的 pending 订单，再基于
  latest 生成新 pending 订单，两步在同一事务内。
* ``latest`` = 本地库 ``daily_price`` 的最大交易日，不是自然日。
* 幂等：UNIQUE(trade_date,strategy,symbol,side) + UNIQUE(order_id,fill_date)
  + 已处理日期不再重放；同日重复运行不重复下单/成交。
* ``--dry-run`` 走完整流程但 rollback，不写库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from ..backtest.execution import ExecutionContext, PendingOrder, Position
from ..backtest.engine import build_strategy
from ..config import Config
from ..errors import DataUnavailableError
from ..risk.position import RiskStateMachine
from ..strategy.base import to_qfq

log = logging.getLogger("quant.paper")

__all__ = ["PaperEngine", "PaperRunResult"]


@dataclass
class PaperRunResult:
    date: str
    dry_run: bool
    strategy: str
    processed_days: list[str] = field(default_factory=list)
    orders_created: int = 0
    trades_created: int = 0
    equity_written: int = 0
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    waiting_for_next_bar: bool = False
    already_processed: bool = False
    snapshot: dict[str, Any] = field(default_factory=dict)
    pending_orders: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date, "dry_run": self.dry_run, "strategy": self.strategy,
            "processed_days": self.processed_days, "orders_created": self.orders_created,
            "trades_created": self.trades_created, "equity_written": self.equity_written,
            "messages": self.messages, "warnings": self.warnings,
            "waiting_for_next_bar": self.waiting_for_next_bar,
            "already_processed": self.already_processed,
            "snapshot": self.snapshot, "pending_orders": self.pending_orders,
        }


class PaperEngine:
    def __init__(self, cfg: Config, store: Any, strategy_name: str | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.strategy_name = strategy_name or str(cfg.get("paper.strategy", "dual_ma"))
        self.strategy = build_strategy(cfg, self.strategy_name)

    # ------------------------------------------------------------------ #
    def resolve_date(self, date: str | None) -> str:
        if date and str(date).lower() != "latest":
            return str(date)
        latest = self.store.latest_trade_date()
        if not latest:
            raise DataUnavailableError(
                ["local_cache:daily_price"], "ALL", ("latest", "latest"),
                detail="本地库无行情，latest 定义为 daily_price 的最大交易日（非自然日）",
            )
        return latest

    def _symbols(self) -> list[str]:
        limit = int(self.cfg.get("data.limit", 30))
        rows = self.store.get_universe(str(self.cfg.get("data.pool", "hs300")),
                                       limit=limit if limit > 0 else None, main_only=True)
        return rows["symbol"].tolist()

    # ------------------------------------------------------------------ #
    def _restore(self, ctx: ExecutionContext) -> None:
        """Rebuild cash / positions / pending orders / risk state from SQLite."""
        state = self.store.get_state(self.strategy_name)
        if state:
            ctx.cash = float(state["cash"])
            ctx.peak_equity = float(state["peak_equity"])
            ctx.risk = RiskStateMachine.restore(self.cfg, state)
        for row in self.store.get_positions(self.strategy_name):
            ctx.positions[row["symbol"]] = Position(
                qty=int(row["qty"]), avg_cost=float(row["avg_cost"]),
                last_price=float(row["last_price"]),
                available_qty=int(row["qty"]),
            )
        for row in self.store.pending_orders(self.strategy_name):
            valid_from = row["valid_from"]
            if valid_from is None:
                # 上次运行时交易日历还没有 T+1；新交易日落库后按日历解析，
                # 绝不回退到信号当日（R-006 / R-011）。
                valid_from = self.store.next_trading_day(row["trade_date"])
            ctx.orders.append(PendingOrder(
                order_id=row["order_id"], trade_date=row["trade_date"],
                strategy=row["strategy"], symbol=row["symbol"], side=row["side"],
                qty=int(row["qty"]), valid_from=valid_from,
                status=row["status"], reason=row["reason"],
                attempts=int(row["attempts"]), est_price=row["est_price"],
                created_at=row["created_at"], order_type=row["order_type"],
            ))
        equity = self.store.get_equity(self.strategy_name)
        if not equity.empty:
            last = equity.iloc[-1]
            denom = float(last["equity"]) or 1.0
            ctx.last_target_weights = {
                s: p.qty * p.last_price / denom for s, p in ctx.positions.items()
            }

    def _rebuild_history(
        self, ctx: ExecutionContext, symbols: Sequence[str], end_date: str, page_size: int
    ) -> None:
        """Paged, bounded-memory replay of qfq history up to ``end_date``."""
        if not end_date:
            return
        for page in self.store.iter_daily(symbols, "0000-01-01", end_date, page_size):
            qfq, _ = to_qfq(page)
            for row in qfq.to_dict("records"):
                ctx.history[str(row["symbol"])].append(row)
            for row in page.to_dict("records"):
                if float(row["volume"]) > 0 and float(row["close"]) > 0:
                    ctx.prev_closes[str(row["symbol"])] = float(row["close"])

    # ------------------------------------------------------------------ #
    def run(
        self,
        date: str | None = "latest",
        dry_run: bool = False,
        symbols: Sequence[str] | None = None,
    ) -> PaperRunResult:
        target = self.resolve_date(date)
        syms = list(symbols) if symbols else self._symbols()
        if not syms:
            raise DataUnavailableError(
                ["local_cache:universe"], "ALL", (target, target),
                detail="universe 中无 MAIN 标的（R-014 首版默认过滤非主板）",
            )
        page_size = int(self.cfg.get("data.page_size", 500))
        result = PaperRunResult(date=target, dry_run=bool(dry_run), strategy=self.strategy_name)

        self.store.begin()
        try:
            initial_cash = float(self.cfg.get("paper.initial_cash", 1_000_000.0))
            ctx = ExecutionContext(
                self.cfg, self.strategy, self.strategy_name, initial_cash,
                calendar_next=self.store.next_trading_day, store=self.store,
            )
            self._restore(ctx)

            last_done = self.store.last_equity_date(self.strategy_name)
            if last_done is None:
                prev = self._previous_trading_day(target)
                self._rebuild_history(ctx, syms, prev or "", page_size)
                start_day = target
            else:
                self._rebuild_history(ctx, syms, last_done, page_size)
                start_day = self.store.next_trading_day(last_done) or target

            all_days = self.store.trading_days(start_day, target)
            if last_done and target <= last_done:
                all_days = []
                result.already_processed = True
                result.messages.append(
                    f"{target} 已处理过（last_equity_date={last_done}），幂等跳过，不重复下单/成交"
                )

            trades_before = self.store.count_trades(self.strategy_name)

            for day in all_days:
                bars = ctx.load_bars(day, syms, page_size, self.store)
                ctx.settle(day, bars)                        # 1
                equity_rec = ctx.mark_to_market(day, bars)    # 2
                ctx.append_history(day, bars)                 # 3a
                weights = ctx.target_weights(day, syms, equity_rec["equity"])  # 3b + 4
                created = ctx.place_orders(day, bars, weights, equity_rec["equity"])  # 5
                result.orders_created += len(created)
                result.processed_days.append(day)

            # 结算所有 <= target 的 pending（含 catch-up 期间未到期之外的遗留单）
            # 已在逐日循环内完成；此处仅补充状态消息。
            self._persist(ctx, result)
            result.trades_created = max(0, self.store.count_trades(self.strategy_name) - trades_before)
            result.equity_written = len([d for d in result.processed_days])

            pending = [o.as_record() for o in ctx.orders if o.status == "pending"]
            result.pending_orders = pending
            next_day = self.store.next_trading_day(target)
            has_next_bar = bool(next_day and self.store.all_dates(next_day, next_day))
            if pending:
                result.waiting_for_next_bar = True
                result.messages.append(
                    "waiting_for_next_bar: %d 个 pending 订单等待 %s 开盘数据，不报错" %
                    (len(pending), next_day or "T+1")
                )
                if not has_next_bar:
                    result.messages.append("T+1 行情尚未落库（latest=本地最大交易日），pending 保留")
            result.messages = list(dict.fromkeys(result.messages + ctx.messages))
            result.warnings = list(dict.fromkeys(result.warnings + ctx.warnings))
            result.snapshot = ctx.snapshot()

            if dry_run:
                self.store.rollback()
                result.messages.append("dry-run: 完整流程已执行，事务已回滚，未写库")
            else:
                self.store.commit()
            return result
        except Exception:
            self.store.rollback()
            raise

    def _previous_trading_day(self, date: str) -> str | None:
        rows = self.store.conn.execute(
            "SELECT date FROM trade_calendar WHERE is_open=1 AND date < ? "
            "ORDER BY date DESC LIMIT 1", (str(date),)
        ).fetchall()
        return rows[0]["date"] if rows else None

    def _persist(self, ctx: ExecutionContext, result: PaperRunResult) -> None:
        for order in ctx.orders:
            self.store.insert_order(order.as_record())
            self.store.update_order(
                order.order_id, status=order.status, reason=order.reason,
                attempts=order.attempts, valid_from=order.valid_from, qty=order.qty,
            )
        for trade in ctx.trades:
            self.store.insert_trade(trade)
        for record in ctx.equity_records:
            self.store.replace_equity(self.strategy_name, record)
        self.store.replace_positions(
            self.strategy_name,
            [
                {"symbol": s, "qty": p.qty, "avg_cost": p.avg_cost,
                 "last_price": p.last_price}
                for s, p in ctx.positions.items()
            ],
        )
        self.store.save_state(self.strategy_name, ctx.snapshot())

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        equity = self.store.get_equity(self.strategy_name)
        return {
            "strategy": self.strategy_name,
            "orders": self.store.count_orders(self.strategy_name),
            "trades": self.store.count_trades(self.strategy_name),
            "pending": len(self.store.pending_orders(self.strategy_name)),
            "equity_days": len(equity),
            "last_equity": (equity.iloc[-1].to_dict() if not equity.empty else None),
            "positions": self.store.get_positions(self.strategy_name),
        }
