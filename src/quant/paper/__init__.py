"""模拟盘：本地 SQLite 订单/成交/资金曲线，不接实盘。"""

from .engine import PaperEngine, PaperRunResult

__all__ = ["PaperEngine", "PaperRunResult"]
