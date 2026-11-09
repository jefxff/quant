"""策略层：Strategy 接口 + 首版示例策略。"""

from .base import Strategy, StrategyContext
from .dual_ma import DualMAStrategy

__all__ = ["Strategy", "StrategyContext", "DualMAStrategy"]
