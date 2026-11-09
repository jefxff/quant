"""模拟盘：T+1 结算、幂等、dry-run、状态机、waiting_for_next_bar。"""

import sqlite3

import pandas as pd
import pytest

from quant.errors import DataUnavailableError
from quant.paper.engine import PaperEngine


def test_latest_is_max_daily_price_date(cfg, store, trading_days):
    engine = PaperEngine(cfg, store)
    assert engine.resolve_date("latest") == trading_days[-1]
    assert engine.resolve_date("latest") == store.latest_trade_date()
    assert engine.resolve_date("2022-05-24") == "2022-05-24"


def test_pending_then_filled_on_next_bar(cfg, store, trading_days):
    engine = PaperEngine(cfg, store)
    d0, d1 = trading_days[100], trading_days[101]
    r1 = engine.run(d0)
    assert r1.orders_created > 0
    assert r1.trades_created == 0          # T 日只下单，不成交
    assert r1.waiting_for_next_bar
    assert any("waiting_for_next_bar" in m for m in r1.messages)
    pending = store.pending_orders("dual_ma")
    assert pending and all(o["status"] == "pending" for o in pending)
    assert all(o["valid_from"] == d1 for o in pending)

    r2 = engine.run(d1)
    assert r2.trades_created > 0
    orders = store.get_orders("dual_ma")
    assert "filled" in set(orders["status"])
    trades = store.get_trades("dual_ma")
    assert not trades.empty
    assert (trades["fill_date"] == d1).all()
    # 成交价含滑点，且费用字段齐全
    assert (trades["commission"] > 0).all()
    assert (trades["slippage_cost"] > 0).all()


def test_idempotent_same_day_repeat(cfg, store, trading_days):
    engine = PaperEngine(cfg, store)
    d0, d1 = trading_days[100], trading_days[101]
    engine.run(d0)
    first = engine.run(d1)
    n_orders, n_trades = store.count_orders("dual_ma"), store.count_trades("dual_ma")
    equity = store.get_equity("dual_ma")["equity"].tolist()

    second = engine.run(d1)
    assert second.already_processed
    assert second.orders_created == 0 and second.trades_created == 0
    assert first.trades_created > 0
    assert store.count_orders("dual_ma") == n_orders
    assert store.count_trades("dual_ma") == n_trades
    assert store.get_equity("dual_ma")["equity"].tolist() == equity


def test_dry_run_writes_nothing(cfg, store, trading_days):
    engine = PaperEngine(cfg, store)
    before = (store.count_orders("dual_ma"), store.count_trades("dual_ma"),
              len(store.get_equity("dual_ma")), len(store.get_positions("dual_ma")))
    result = engine.run(trading_days[100], dry_run=True)
    assert result.dry_run
    assert result.orders_created > 0             # 完整流程确实跑过
    assert any("dry-run" in m for m in result.messages)
    after = (store.count_orders("dual_ma"), store.count_trades("dual_ma"),
             len(store.get_equity("dual_ma")), len(store.get_positions("dual_ma")))
    assert after == before == (0, 0, 0, 0)


def test_unique_constraints_block_duplicates(cfg, store):
    order = {
        "order_id": "o1", "trade_date": "2022-05-24", "strategy": "dual_ma",
        "symbol": "600000.SH", "side": "BUY", "qty": 100,
        "valid_from": "2022-05-25", "created_at": "2022-05-24T00:00:00+00:00",
    }
    assert store.insert_order(order) is True
    order2 = dict(order, order_id="o2")
    assert store.insert_order(order2) is False       # UNIQUE(trade_date,strategy,symbol,side)
    trade = {
        "trade_id": "t1", "order_id": "o1", "fill_date": "2022-05-25",
        "symbol": "600000.SH", "side": "BUY", "qty": 100, "price": 10.0,
        "commission": 5.0, "stamp_tax": 0.0, "slippage_cost": 0.01,
    }
    assert store.insert_trade(trade) is True
    assert store.insert_trade(dict(trade, trade_id="t2")) is False  # UNIQUE(order_id,fill_date)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO paper_orders(order_id,trade_date,strategy,symbol,side,qty,status,reason,created_at) "
            "VALUES('o3','2022-05-24','dual_ma','600000.SH','BUY',100,'weird','','x')"
        )


def test_no_data_raises_data_unavailable(cfg, tmp_path):
    from quant.data.store import Store
    empty = Store(tmp_path / "empty.db")
    try:
        with pytest.raises(DataUnavailableError) as exc:
            PaperEngine(cfg, empty).run("latest")
        assert "daily_price" in str(exc.value) or "local_cache" in str(exc.value)
    finally:
        empty.close()


def test_status_machine_and_waiting_message(cfg, store, trading_days):
    engine = PaperEngine(cfg, store)
    result = engine.run(trading_days[-1])
    assert result.waiting_for_next_bar
    assert result.pending_orders
    assert all(o["status"] == "pending" for o in result.pending_orders)
    # 日历中尚无 T+1：valid_from 必须为空，绝不能回退到信号当日（R-006）
    assert all(o["valid_from"] is None for o in result.pending_orders)
    allowed = {"pending", "filled", "rejected", "cancelled", "expired"}
    assert set(store.get_orders("dual_ma")["status"]).issubset(allowed)
    assert engine.status()["pending"] == len(result.pending_orders)


def test_pending_order_fills_when_next_bar_arrives(cfg, store, trading_days, main_symbols):
    """T 日无 T+1 -> pending 保留；T+1 行情落库后按 T+1 开盘成交，不在 T 日成交。"""
    engine = PaperEngine(cfg, store)
    engine.run(trading_days[-1])
    assert store.count_trades("dual_ma") == 0
    pending = store.pending_orders("dual_ma")
    assert pending and all(o["valid_from"] is None for o in pending)

    next_day = "2023-01-02"
    store.upsert_calendar(pd.DataFrame([{"date": next_day, "is_open": 1}]), source="fixture")
    bars = store.read_daily(main_symbols, trading_days[-1], trading_days[-1]).copy()
    bars["date"] = next_day
    bars["source"] = "fixture"
    store.upsert_daily(bars)

    result = engine.run("latest")
    assert result.date == next_day
    assert result.trades_created > 0
    trades = store.get_trades("dual_ma")
    assert not trades.empty
    assert (trades["fill_date"] == next_day).all()
    # 成交价必须来自 T+1 开盘（含滑点），而非信号日收盘
    opens = {r["symbol"]: float(r["open"]) for r in bars.to_dict("records")}
    for trade in trades.itertuples(index=False):
        assert trade.symbol in opens
        assert abs(trade.price - opens[trade.symbol]) < 0.5
