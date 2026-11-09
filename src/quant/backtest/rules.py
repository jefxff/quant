"""A 股交易规则：100 股整数倍、T+1、涨跌停、滑点夹紧、停牌顺延。

T-002 涨跌停价 / 成交价 / 滑点一律使用**不复权价**。
T-009 首版仅支持沪深主板 ±10% 近似；非主板标的由 universe.board 过滤，
       绝不会对创业板/科创板/北交所/ST 套用 ±10%。
T-010 停牌：无行情或 volume=0 -> 最多顺延 3 个交易日，超时 expired。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .costs import slippage_price

__all__ = [
    "round_lot", "target_shares", "limit_prices", "FillDecision",
    "decide_fill", "is_suspended", "decide_suspension", "t_plus_one",
]

_CENT = Decimal("0.01")
_EPS = 1e-8


def _round2(value: float) -> float:
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def round_lot(qty: int | float, cfg: Any) -> int:
    """下取整到 100 股整数倍（T-005）。"""
    lot = int(cfg.get("rules.lot_size", 100))
    return int(math.floor(float(qty) / lot)) * lot


def target_shares(equity: float, weight: float, close_price: float, cfg: Any) -> int:
    """T-004: 用 T 日**收盘价**估算目标股数（不复权）。"""
    if equity <= 0 or weight <= 0 or close_price <= 0:
        return 0
    return round_lot(equity * float(weight) / float(close_price), cfg)


def limit_prices(prev_close: float, cfg: Any) -> tuple[float, float]:
    """以昨日不复权收盘价计算涨跌停价，按 A 股规则四舍五入到分。"""
    pct = float(cfg.get("rules.price_limit_pct", 0.10))
    base = Decimal(str(prev_close))
    lower = _round2(float(base * (Decimal("1") - Decimal(str(pct)))))
    upper = _round2(float(base * (Decimal("1") + Decimal(str(pct)))))
    return lower, upper


@dataclass(frozen=True)
class FillDecision:
    price: float
    lower: float
    upper: float
    blocked: str | None = None   # 'limit_up' / 'limit_down'


def decide_fill(open_price: float, side: str, prev_close: float, cfg: Any) -> FillDecision:
    """T-007 + T-009.

    1. 开盘触及涨停 -> 买单不成交；触及跌停 -> 卖单不成交；
    2. 否则成交价 = clip(open*(1±slippage), 跌停价, 涨停价)，
       滑点不得把成交价推出涨跌停区间。
    """
    side = str(side).upper()
    lower, upper = limit_prices(prev_close, cfg)
    if side == "BUY" and float(open_price) >= upper - _EPS:
        return FillDecision(upper, lower, upper, blocked="limit_up")
    if side == "SELL" and float(open_price) <= lower + _EPS:
        return FillDecision(lower, lower, upper, blocked="limit_down")
    slipped = slippage_price(open_price, side, cfg)
    filled = min(max(slipped, lower), upper)
    # 按 T-007 原式返回 clip 结果；涨跌停价本身按分四舍五入，成交价不做二次取整，
    # 否则 0.01 元最小变动单位会把万 1 滑点整个抹掉。
    return FillDecision(float(filled), lower, upper, blocked=None)


def is_suspended(bar: Any | None) -> bool:
    """T-010: 无行情或 volume=0 视为停牌。"""
    if bar is None:
        return True
    volume = getattr(bar, "volume", None)
    if volume is None:
        volume = bar.get("volume") if isinstance(bar, dict) else None
    try:
        return float(volume) <= 0
    except (TypeError, ValueError):
        return True


def decide_suspension(bar: Any | None, attempts: int, cfg: Any) -> str:
    """Return ``'deferred'`` or ``'expired'`` (reason=suspended)."""
    max_days = int(cfg.get("rules.max_suspension_days", 3))
    if not is_suspended(bar):
        return "tradeable"
    return "expired" if int(attempts) + 1 > max_days else "deferred"


def t_plus_one(signal_date: str, calendar_next: Any) -> str | None:
    """R-006/T-001: 信号在 T 收盘后生成，最早 T+1 开盘成交。"""
    return calendar_next(signal_date) if calendar_next is not None else None
