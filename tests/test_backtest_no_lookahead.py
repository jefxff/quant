"""R-006 / T-001：禁止未来函数，信号 T 日生成、最早 T+1 开盘成交。"""

from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.engine import BacktestEngine
from quant.config import load_config
from quant.data.store import Store
from quant.strategy.base import check_no_lookahead
from quant.strategy.dual_ma import DualMAStrategy

FIXTURES = Path(__file__).resolve().parent / "fixtures"
START, END = "2022-01-04", "2022-12-30"


class SpyStrategy(DualMAStrategy):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls: list[tuple[str, str | None]] = []

    def generate_signals(self, history, date, symbols, context=None):
        max_date = str(history["date"].max()) if history is not None and not history.empty else None
        self.calls.append((str(date), max_date))
        return super().generate_signals(history, date, symbols, context)


def _build_store(path: Path) -> Store:
    db = Store(path)
    db.upsert_universe(pd.read_csv(FIXTURES / "universe.csv", dtype=str))
    db.upsert_calendar(pd.read_csv(FIXTURES / "trade_calendar.csv", dtype={"date": str}))
    db.upsert_daily(pd.read_csv(FIXTURES / "daily_prices.csv", dtype={"symbol": str, "date": str}))
    return db


def test_interface_rejects_future_rows():
    frame = pd.DataFrame({"symbol": ["600000.SH", "600000.SH"],
                          "date": ["2022-01-04", "2022-01-06"]})
    with pytest.raises(AssertionError, match="未来函数"):
        check_no_lookahead(frame, "2022-01-05")
    check_no_lookahead(frame.head(1), "2022-01-05")


def test_strategy_never_sees_future_bars(cfg, store, main_symbols):
    spy = SpyStrategy(cfg)
    engine = BacktestEngine(cfg, store, strategy=spy, strategy_name="dual_ma", run_id="spy")
    result = engine.run(START, END, main_symbols)
    assert len(spy.calls) == len(result.trading_days)
    for call_date, max_history_date in spy.calls:
        assert max_history_date is None or max_history_date <= call_date
    # 历史窗口有界（R-013）
    assert all(len(dq) <= spy.required_history() + 5 for dq in engine_run_history(result))


def engine_run_history(result):
    return result.context.history.values()


def test_every_fill_is_at_least_t_plus_one(cfg, store, main_symbols):
    result = BacktestEngine(cfg, store, run_id="tplus1").run(START, END, main_symbols)
    orders = result.orders.set_index("order_id")["trade_date"].to_dict()
    assert len(result.trades) > 0
    exact_t1 = 0
    for trade in result.trades.itertuples(index=False):
        signal_date = orders[trade.order_id]
        next_day = store.next_trading_day(signal_date)
        assert next_day is not None
        assert trade.fill_date >= next_day, "不得在信号当日或 T+1 之前成交"
        if trade.fill_date == next_day:
            exact_t1 += 1
    assert exact_t1 > 0


def test_future_price_shock_does_not_change_past_orders(tmp_path, cfg, main_symbols):
    """把某日之后的所有收盘价放大 3 倍，该日之前的订单必须逐字节一致。"""
    split_date = "2022-06-30"
    db1 = _build_store(tmp_path / "a.db")
    db2 = _build_store(tmp_path / "b.db")
    try:
        db2.conn.execute(
            "UPDATE daily_price SET close = close * 3 WHERE date > ?", (split_date,)
        )
        r1 = BacktestEngine(cfg, db1, run_id="a").run(START, END, main_symbols)
        r2 = BacktestEngine(cfg, db2, run_id="b").run(START, END, main_symbols)
        cols = ["order_id", "trade_date", "symbol", "side", "qty", "status"]
        o1 = r1.orders[r1.orders["trade_date"] < split_date][cols].reset_index(drop=True)
        o2 = r2.orders[r2.orders["trade_date"] < split_date][cols].reset_index(drop=True)
        pd.testing.assert_frame_equal(o1, o2)
        assert not o1.empty
        e1 = r1.equity[r1.equity["date"] <= split_date][["date", "equity"]].reset_index(drop=True)
        e2 = r2.equity[r2.equity["date"] <= split_date][["date", "equity"]].reset_index(drop=True)
        pd.testing.assert_frame_equal(e1, e2)
    finally:
        db1.close()
        db2.close()


def test_fixed_params_single_full_run_with_segmented_report(cfg, store, main_symbols):
    """参数只跑全程一次，样本内外仅做分段报告（无两段式泄漏）。"""
    result = BacktestEngine(cfg, store, run_id="seg").run(START, END, main_symbols)
    assert result.oos_start is not None
    days = result.trading_days
    assert result.oos_start in days
    oos_days = [d for d in days if d >= result.oos_start]
    assert 0 < len(oos_days) < len(days)
    assert abs(len(oos_days) / len(days) - float(cfg.get("backtest.out_of_sample_ratio"))) < 0.02
    assert len(result.equity) == len(days)
    assert not result.equity["equity"].isna().any()
