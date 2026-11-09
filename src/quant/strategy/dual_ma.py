"""示例策略：双均线(20/60) + 波动率目标仓位。

  * 均线、波动率全部基于**前复权收盘价**（T-002）；
  * 只使用截至 T 日的数据；T 日无行情的标的从返回值中省略，由引擎保留
    上一期目标权重（避免停牌被误判为平仓信号）；
  * 输出的是**目标权重**，订单生成/执行完全在引擎侧（策略与执行解耦）。
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import pandas as pd

from .base import Strategy, StrategyContext

__all__ = ["DualMAStrategy"]


class DualMAStrategy(Strategy):
    name = "dual_ma"

    def required_history(self) -> int:
        return max(
            int(self.cfg.get("backtest.long_window", 60)),
            int(self.cfg.get("backtest.vol_lookback", 20)),
        ) + 10

    def _signals(
        self,
        history: pd.DataFrame,
        date: str,
        symbols: list[str],
        context: StrategyContext,
    ) -> dict[str, float]:
        short = int(self.cfg.get("backtest.short_window", 20))
        long = int(self.cfg.get("backtest.long_window", 60))
        vol_lb = int(self.cfg.get("backtest.vol_lookback", 20))
        vol_target = float(self.cfg.get("backtest.vol_target", 0.15))
        weights: dict[str, float] = {}
        if history is None or history.empty:
            return weights

        for symbol in symbols:
            series = history[history["symbol"] == symbol].sort_values("date")
            if len(series) < max(long, vol_lb) + 1:
                continue
            if str(series["date"].iloc[-1]) != str(date):
                # 当日无行情（停牌/缺失）：不产生新信号，由引擎沿用上期权重
                continue
            closes = series["qfq_close"].astype(float).to_numpy()
            if not np.isfinite(closes).all() or (closes <= 0).any():
                continue
            ma_short = float(pd.Series(closes).rolling(short).mean().iloc[-1])
            ma_long = float(pd.Series(closes).rolling(long).mean().iloc[-1])
            if not (math.isfinite(ma_short) and math.isfinite(ma_long)) or ma_short <= ma_long:
                weights[symbol] = 0.0
                continue
            returns = pd.Series(closes).pct_change().dropna().iloc[-vol_lb:]
            vol = float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) >= 2 else 0.0
            scale = 1.0 if not math.isfinite(vol) or vol <= 1e-9 else min(1.0, vol_target / vol)
            weights[symbol] = max(0.0, min(1.0, scale))
        return weights
