"""佣金 / 印花税 / 滑点 —— 回测与模拟盘**唯一**的成本实现。

T-008 默认：佣金万 2.5、最低 5 元；印花税卖出单边 0.05%；滑点万 1。
T-006 现金不足：按含手续费的最大 100 股整数倍成交。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "commission", "stamp_tax", "estimate_fee", "slippage_price",
    "max_affordable_shares", "slippage_cost", "FillCosts",
]


def commission(notional: float, cfg: Any) -> float:
    rate = float(cfg.get("costs.commission_rate", 0.00025))
    minimum = float(cfg.get("costs.commission_min", 5.0))
    if notional <= 0:
        return 0.0
    return max(notional * rate, minimum)


def stamp_tax(notional: float, side: str, cfg: Any) -> float:
    """卖出单边征收。"""
    if str(side).upper() != "SELL" or notional <= 0:
        return 0.0
    return notional * float(cfg.get("costs.stamp_tax_rate", 0.0005))


def estimate_fee(notional: float, side: str, cfg: Any) -> float:
    return commission(notional, cfg) + stamp_tax(notional, side, cfg)


def slippage_price(price: float, side: str, cfg: Any) -> float:
    """买入向上、卖出向下（T-008）。一律基于不复权价（T-002）。"""
    rate = float(cfg.get("costs.slippage_rate", 0.0001))
    factor = 1.0 + rate if str(side).upper() == "BUY" else 1.0 - rate
    return float(price) * factor


def slippage_cost(fill_price: float, reference_open: float, qty: int) -> float:
    """滑点成本 = |成交价 - 开盘价| * 股数（相对未滑点开盘价的机会成本）。"""
    return abs(float(fill_price) - float(reference_open)) * int(qty)


def max_affordable_shares(cash: float, open_price: float, side: str, cfg: Any) -> int:
    """T-006: ``floor((cash - 预估费用) / (open*(1+slippage)) / lot) * lot``.

    先用现金上界预估手续费得到整数手数，再用**实际**成交额+实际费用
    逐手回退校验，保证成交后现金不为负。
    """
    lot = int(cfg.get("rules.lot_size", 100))
    cash, open_price = float(cash), float(open_price)
    if cash <= 0 or open_price <= 0 or lot <= 0:
        return 0
    slipped = slippage_price(open_price, side, cfg)
    pre_fee = estimate_fee(cash, side, cfg)  # 以 cash 作为成交额上界，保守预估
    usable = cash - pre_fee
    qty = int(math.floor(usable / slipped / lot)) * lot
    qty = max(0, qty)
    while qty > 0:
        notional = slipped * qty
        if notional + estimate_fee(notional, side, cfg) <= cash + 1e-9:
            break
        qty -= lot
    return max(0, qty)


@dataclass(frozen=True)
class FillCosts:
    commission: float
    stamp_tax: float
    slippage_cost: float

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.slippage_cost


def compute_fill_costs(
    fill_price: float, reference_open: float, qty: int, side: str, cfg: Any
) -> FillCosts:
    notional = float(fill_price) * int(qty)
    return FillCosts(
        commission=commission(notional, cfg),
        stamp_tax=stamp_tax(notional, side, cfg),
        slippage_cost=slippage_cost(fill_price, reference_open, qty),
    )
