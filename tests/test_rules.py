"""T-005 / T-007 / T-009 / T-010 交易规则离线测试。"""

import pytest

from quant.backtest.rules import (
    decide_fill, decide_suspension, is_suspended, limit_prices, round_lot, target_shares,
)


def test_round_lot(cfg):
    assert round_lot(99, cfg) == 0
    assert round_lot(155, cfg) == 100
    assert round_lot(200, cfg) == 200
    assert round_lot(1234, cfg) == 1200


def test_target_shares_uses_close(cfg):
    # 100 万 * 10% / 10 元 = 10000 股 -> 整手 10000
    assert target_shares(1_000_000.0, 0.10, 10.0, cfg) == 10_000
    assert target_shares(1_000_000.0, 0.0, 10.0, cfg) == 0


def test_limit_prices_round_half_up(cfg):
    assert limit_prices(10.0, cfg) == (9.0, 11.0)
    assert limit_prices(10.05, cfg) == (9.05, 11.06)  # 9.045 -> 9.05, 11.055 -> 11.06


def test_limit_up_buy_and_limit_down_sell_blocked(cfg):
    buy = decide_fill(11.0, "BUY", 10.0, cfg)
    assert buy.blocked == "limit_up"
    sell = decide_fill(9.0, "SELL", 10.0, cfg)
    assert sell.blocked == "limit_down"
    # 反方向不受阻
    assert decide_fill(11.0, "SELL", 10.0, cfg).blocked is None
    assert decide_fill(9.0, "BUY", 10.0, cfg).blocked is None


def test_fill_price_is_clipped_inside_limits(cfg):
    decision = decide_fill(10.0, "BUY", 10.0, cfg)
    assert decision.blocked is None
    assert decision.lower <= decision.price <= decision.upper
    assert abs(decision.price - 10.001) < 1e-9
    near_up = decide_fill(10.9999, "BUY", 10.0, cfg)
    assert near_up.price <= near_up.upper == 11.0
    near_down = decide_fill(9.0001, "SELL", 10.0, cfg)
    assert near_down.price >= near_down.lower == 9.0


def test_suspension_detection_and_expiry(cfg):
    assert is_suspended(None)
    assert is_suspended({"volume": 0})
    assert not is_suspended({"volume": 100})
    assert decide_suspension(None, 0, cfg) == "deferred"
    assert decide_suspension({"volume": 0}, 2, cfg) == "deferred"
    assert decide_suspension({"volume": 0}, 3, cfg) == "expired"
    assert decide_suspension({"volume": 10}, 99, cfg) == "tradeable"


def test_non_main_board_never_gets_ten_percent(store, cfg):
    """R-014/T-009: 非 MAIN 标的被过滤，且配置拒绝 ±20%。"""
    from quant.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        cfg.with_overrides({"rules.price_limit_pct": 0.20})
    main = store.get_universe("hs300", main_only=True)["symbol"].tolist()
    assert "300750.SZ" not in main
    assert "688981.SH" not in main
    assert "830001.BJ" not in main
    assert "000004.SZ" not in main
    assert main == sorted(main)  # limit 语义：按代码顺序，非随机
