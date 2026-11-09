"""Strategy 接口 (R-006 禁止未来函数)。

约定：
  * 策略只接收 ``history``（截至 T 日、且仅含 T 日及之前的前复权行情）；
  * 接口层强制校验 ``history.date.max() <= date``，任何越界直接抛错；
  * 输出为目标权重 dict ``{symbol: weight}``，与订单执行完全解耦；
  * 策略不得访问网络、不得读取 T 日之后的任何数据。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger("quant.strategy")

__all__ = ["Strategy", "StrategyContext", "to_qfq", "check_no_lookahead"]


def check_no_lookahead(history: pd.DataFrame, date: str) -> None:
    """R-006 hard guard, executed by the interface for every strategy."""
    if history is None or history.empty:
        return
    if "date" not in history.columns:
        raise ValueError("history 必须包含 date 列")
    future = history[history["date"].astype(str) > str(date)]
    if not future.empty:
        raise AssertionError(
            f"检测到未来函数：策略在 {date} 看到了 {sorted(future['date'].unique())[:3]} 的数据"
        )


def to_qfq(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """T-002: 前复权价 = 不复权价 * adj_factor.

    adj_factor 缺失的行被剔除（不填补），对应标的当日不生成信号并记 warning。
    返回 (qfq_frame, skipped_symbols)。
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["symbol", "date", "qfq_open", "qfq_high",
                                     "qfq_low", "qfq_close", "volume", "amount"]), []
    data = frame.copy()
    data["adj_factor"] = pd.to_numeric(data.get("adj_factor"), errors="coerce")
    bad = data["adj_factor"].isna() | (data["adj_factor"] <= 0)
    skipped = sorted(data.loc[bad, "symbol"].astype(str).unique().tolist())
    if skipped:
        log.warning("adj_factor 缺失，以下标的当日不生成信号: %s", skipped)
    good = data.loc[~bad].copy()
    if good.empty:
        return pd.DataFrame(columns=["symbol", "date", "qfq_open", "qfq_high",
                                     "qfq_low", "qfq_close", "volume", "amount"]), skipped
    for col in ("open", "high", "low", "close"):
        good[f"qfq_{col}"] = good[col] * good["adj_factor"]
    return good[["symbol", "date", "qfq_open", "qfq_high", "qfq_low",
                 "qfq_close", "volume", "amount"]], skipped


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to see on date T."""

    date: str
    symbols: list[str]
    cash: float = 0.0
    equity: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Strategy(ABC):
    name: str = "base"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    def required_history(self) -> int:
        """Number of most-recent bars needed (bounded rolling window, R-013)."""
        return max(
            int(self.cfg.get("backtest.long_window", 60)),
            int(self.cfg.get("backtest.vol_lookback", 20)),
        ) + 5

    def generate_signals(
        self,
        history: pd.DataFrame,
        date: str,
        symbols: Sequence[str],
        context: StrategyContext | None = None,
    ) -> dict[str, float]:
        """Return target weights. Never place orders yourself."""
        check_no_lookahead(history, date)  # enforced for every subclass
        return self._signals(history, date, list(symbols), context or StrategyContext(date, list(symbols)))

    @abstractmethod
    def _signals(
        self,
        history: pd.DataFrame,
        date: str,
        symbols: list[str],
        context: StrategyContext,
    ) -> dict[str, float]:
        ...
