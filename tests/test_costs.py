"""T-006 / T-008 成本公式离线测试。"""

from quant.backtest.costs import (
    commission, compute_fill_costs, estimate_fee, max_affordable_shares,
    slippage_cost, slippage_price, stamp_tax,
)


def test_commission_minimum_fee(cfg):
    assert commission(1000.0, cfg) == 5.0           # 0.25 元 -> 最低 5 元
    assert commission(0.0, cfg) == 0.0
    assert abs(commission(1_000_000.0, cfg) - 250.0) < 1e-9


def test_stamp_tax_sell_only(cfg):
    assert stamp_tax(10_000.0, "BUY", cfg) == 0.0
    assert abs(stamp_tax(10_000.0, "SELL", cfg) - 5.0) < 1e-9
    assert abs(estimate_fee(10_000.0, "SELL", cfg) - 10.0) < 1e-9  # 5 佣金 + 5 印花


def test_slippage_direction(cfg):
    assert slippage_price(10.0, "BUY", cfg) > 10.0
    assert slippage_price(10.0, "SELL", cfg) < 10.0
    assert abs(slippage_price(10.0, "BUY", cfg) - 10.001) < 1e-9
    assert abs(slippage_cost(10.001, 10.0, 100) - 0.09999999999999964) < 1e-6


def test_max_affordable_shares_lot_and_fees(cfg):
    cash, price = 10_000.0, 10.0
    qty = max_affordable_shares(cash, price, "BUY", cfg)
    assert qty % 100 == 0
    assert qty == 900
    fill = slippage_price(price, "BUY", cfg)
    assert fill * qty + estimate_fee(fill * qty, "BUY", cfg) <= cash + 1e-9
    # 再买一手就会超出现金
    assert fill * (qty + 100) + estimate_fee(fill * (qty + 100), "BUY", cfg) > cash


def test_max_affordable_shares_zero_cash(cfg):
    assert max_affordable_shares(0.0, 10.0, "BUY", cfg) == 0
    assert max_affordable_shares(4.0, 10.0, "BUY", cfg) == 0


def test_compute_fill_costs_all_components(cfg):
    costs = compute_fill_costs(10.001, 10.0, 100, "BUY", cfg)
    assert costs.commission == 5.0
    assert costs.stamp_tax == 0.0
    assert costs.slippage_cost > 0
    assert abs(costs.total - (costs.commission + costs.slippage_cost)) < 1e-12
    sell = compute_fill_costs(9.999, 10.0, 100, "SELL", cfg)
    assert sell.commission == 5.0
    assert abs(sell.stamp_tax - 9.999 * 100 * 0.0005) < 1e-9
    assert sell.slippage_cost > 0
