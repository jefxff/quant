"""回测层：事件驱动引擎 + 成本/规则/共享执行内核。"""

from .costs import commission, max_affordable_shares, slippage_price, stamp_tax
from .engine import BacktestEngine, BacktestResult
from .execution import ExecutionContext
from .rules import decide_fill, limit_prices, round_lot

__all__ = [
    "BacktestEngine", "BacktestResult", "ExecutionContext",
    "commission", "stamp_tax", "slippage_price", "max_affordable_shares",
    "decide_fill", "limit_prices", "round_lot",
]
