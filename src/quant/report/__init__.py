"""报告层：绩效指标 + Markdown/CSV/PNG 渲染 + 自动生成的假设与限制。"""

from .metrics import compute_metrics, metrics_frame, segment_metrics
from .render import build_assumptions, make_run_id, render_backtest, render_paper, show_report

__all__ = [
    "compute_metrics", "segment_metrics", "metrics_frame",
    "build_assumptions", "make_run_id", "render_backtest", "render_paper", "show_report",
]
