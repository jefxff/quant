"""事件驱动回测引擎（R-009: 不引入 backtrader/vnpy/qlib）。

按交易日历逐日推进，不使用全期向量化直接生成交易。行情按日分页读取
（R-013），全程只读本地 SQLite（R-004）。
固定参数跑**全程**后再做样本内/样本外分段报告，禁止两段式泄漏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from ..config import Config
from ..errors import DataUnavailableError
from ..strategy.base import Strategy
from ..strategy.dual_ma import DualMAStrategy
from .execution import ExecutionContext

log = logging.getLogger("quant.backtest")

__all__ = ["BacktestEngine", "BacktestResult"]


@dataclass
class BacktestResult:
    run_id: str
    strategy: str
    start: str
    end: str
    symbols: list[str]
    context: ExecutionContext
    cfg: Config
    audit: dict[str, Any]
    oos_start: str | None = None
    trading_days: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def trades(self) -> pd.DataFrame:
        return self.context.trades_frame()

    @property
    def equity(self) -> pd.DataFrame:
        return self.context.equity_frame()

    @property
    def orders(self) -> pd.DataFrame:
        return self.context.orders_frame()

    @property
    def positions(self) -> pd.DataFrame:
        return self.context.positions_frame()

    @property
    def costs(self) -> pd.DataFrame:
        return self.context.costs_frame()


def build_strategy(cfg: Config, name: str | None = None) -> Strategy:
    name = name or str(cfg.get("backtest.strategy", "dual_ma"))
    if name == "dual_ma":
        return DualMAStrategy(cfg)
    raise ValueError(f"未知策略: {name}")


class BacktestEngine:
    def __init__(
        self,
        cfg: Config,
        store: Any,
        strategy: Strategy | None = None,
        strategy_name: str | None = None,
        run_id: str = "backtest",
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.strategy_name = strategy_name or str(cfg.get("backtest.strategy", "dual_ma"))
        self.strategy = strategy or build_strategy(cfg, self.strategy_name)
        self.run_id = run_id

    def run(
        self,
        start: str,
        end: str,
        symbols: Sequence[str],
        initial_cash: float | None = None,
    ) -> BacktestResult:
        start, end = str(start), str(end)
        cash = float(initial_cash if initial_cash is not None else self.cfg.get("backtest.initial_cash"))
        dates = self.store.trading_days(start, end)
        if not dates:
            raise DataUnavailableError(
                ["local_cache:trade_calendar"], symbols, (start, end),
                detail="trade_calendar 为空，请先执行 data update 落库交易日历",
            )
        audit = self.store.missing_days_audit(symbols, start, end)
        audit["boards"] = self.store.board_counts(str(self.cfg.get("data.pool", "hs300")))
        audit["survivorship_bias_warning"] = (
            "当前免费数据源可能缺失退市股票；使用『沪深300 当前成分』回溯历史存在幸存者偏差"
        )
        for symbol in symbols:
            if audit["per_symbol"].get(symbol, {}).get("actual_days", 0) == 0:
                raise DataUnavailableError(
                    ["local_cache"], symbol, (start, end),
                    detail="R-004 回测禁止联网；本地缓存无任何该标的行情，"
                           "请手动执行 data update，禁止填补/伪造数据",
                )

        ratio = float(self.cfg.get("backtest.out_of_sample_ratio", 0.20))
        split_idx = int(len(dates) * (1.0 - ratio))
        oos_start = dates[split_idx] if 0 < split_idx < len(dates) else None

        ctx = ExecutionContext(
            self.cfg, self.strategy, self.strategy_name, cash,
            calendar_next=self.store.next_trading_day, store=self.store,
        )
        page_size = int(self.cfg.get("data.page_size", 500))
        for date in dates:
            bars = ctx.load_bars(date, symbols, page_size, self.store)
            # T-003 fixed order
            ctx.settle(date, bars)                     # 1. T+1 开盘撮合 pending
            equity_rec = ctx.mark_to_market(date, bars)  # 2. 收盘 mark-to-market
            ctx.append_history(date, bars)             # 3a. 仅截至 T 日的历史
            weights = ctx.target_weights(date, symbols, equity_rec["equity"])  # 3b+4
            ctx.place_orders(date, bars, weights, equity_rec["equity"])        # 5
        log.info(
            "回测完成 %s %s..%s symbols=%d days=%d trades=%d",
            self.strategy_name, start, end, len(symbols), len(dates), len(ctx.trades),
        )
        return BacktestResult(
            run_id=self.run_id, strategy=self.strategy_name, start=start, end=end,
            symbols=list(symbols), context=ctx, cfg=self.cfg, audit=audit,
            oos_start=oos_start, trading_days=dates, warnings=list(ctx.warnings),
        )
