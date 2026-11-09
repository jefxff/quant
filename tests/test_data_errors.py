"""R-003 / R-005 / R-011 / R-014 / 配置优先级与校验 离线测试。"""

import sqlite3

import pandas as pd
import pytest

from quant.config import load_config
from quant.data.sources import normalize_symbol, to_baostock, to_plain
from quant.data.store import Store
from quant.data.update import classify_board, fetch_daily
from quant.errors import ConfigValidationError, DataIntegrityError, DataUnavailableError
from quant.strategy.base import to_qfq

START, END = "2022-01-04", "2022-12-30"


def test_data_unavailable_error_contains_required_fields():
    err = DataUnavailableError(["local_cache", "baostock", "akshare"],
                               "600000.SH", (START, END), detail="no rows")
    text = str(err)
    assert "600000.SH" in text
    assert START in text and END in text
    assert "local_cache" in text and "baostock" in text and "akshare" in text
    assert err.exit_code == 2
    assert err.source_tried == ["local_cache", "baostock", "akshare"]
    assert err.date_range == f"{START}..{END}"


def test_offline_fetch_with_empty_cache_raises(cfg, tmp_path):
    empty = Store(tmp_path / "empty.db")
    try:
        with pytest.raises(DataUnavailableError) as exc:
            fetch_daily(cfg, empty, ["600000.SH"], START, END, allow_network=False)
        assert "local_cache" in exc.value.source_tried
        assert "600000.SH" in str(exc.value)
    finally:
        empty.close()


def test_no_silent_nan_fill(cfg, tmp_path):
    db = Store(tmp_path / "nan.db")
    bad = pd.DataFrame([{
        "symbol": "600000.SH", "date": START, "open": 10.0, "high": 11.0,
        "low": 9.0, "close": float("nan"), "volume": 100, "amount": 1000.0,
        "adj_factor": 1.0, "source": "fixture",
    }])
    with pytest.raises(DataIntegrityError):
        db.upsert_daily(bad)
    assert db.count_daily() == 0
    db.close()


def test_missing_adj_factor_produces_no_signal_row(cfg):
    frame = pd.DataFrame([
        {"symbol": "600000.SH", "date": START, "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 100, "amount": 1000.0, "adj_factor": 1.0},
        {"symbol": "000001.SZ", "date": START, "open": 5.0, "high": 5.5, "low": 4.5,
         "close": 5.2, "volume": 100, "amount": 520.0, "adj_factor": None},
    ])
    qfq, skipped = to_qfq(frame)
    assert skipped == ["000001.SZ"]
    assert list(qfq["symbol"]) == ["600000.SH"]
    assert not qfq.isna().any().any()
    assert abs(qfq["qfq_close"].iloc[0] - 10.5) < 1e-9
    # 前复权 = 不复权 * adj_factor
    frame2 = frame.copy()
    frame2["adj_factor"] = 1.5
    qfq2, _ = to_qfq(frame2)
    assert abs(qfq2["qfq_close"].iloc[0] - 15.75) < 1e-9


def test_board_check_constraint(cfg, store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO universe(symbol,name,industry,board,in_date,out_date,source,pool) "
            "VALUES('999999.SH','x','x','INVALID',NULL,NULL,'fixture','hs300')"
        )
    assert store.board_counts("hs300")["MAIN"] == 8
    assert classify_board("300750.SZ", "宁德时代") == "CYB"
    assert classify_board("688981.SH", "中芯国际") == "KCB"
    assert classify_board("830001.BJ", "北交测试") == "BSE"
    assert classify_board("600000.SH", "*ST浦发") == "ST"


def test_symbol_conversion_contract():
    assert normalize_symbol("600000.SH") == "600000.SH"
    assert normalize_symbol("sh.600000") == "600000.SH"
    assert normalize_symbol("SH600000") == "600000.SH"
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("000001.SZ") == "000001.SZ"
    assert normalize_symbol("sz.000001") == "000001.SZ"
    assert normalize_symbol("830001.BJ") == "830001.BJ"
    assert to_baostock("600000.SH") == "sh.600000"
    assert to_plain("000001.SZ") == "000001"
    with pytest.raises(ValueError):
        normalize_symbol("not-a-symbol")


def test_config_priority_cli_env_yaml_default(project_root, tmp_path):
    cfg = load_config(
        None,
        overrides={"data.limit": 7, "database.path": str(tmp_path / "x.db")},
        env={"QUANT_LIMIT": "9", "QUANT_DATA__PAGE_SIZE": "321"},
        root=project_root,
    )
    assert cfg.get("data.limit") == 7           # CLI 胜出
    assert cfg.get("data.page_size") == 321     # env 胜出 yaml
    assert cfg.get("costs.commission_rate") == 0.00025  # 默认值
    assert cfg.get("data.source") == "baostock"         # yaml


@pytest.mark.parametrize("patch", [
    {"backtest.out_of_sample_ratio": 0.9},
    {"backtest.short_window": 80},
    {"rules.price_limit_pct": 0.20},
    {"data.source": "wind"},
    {"risk.drawdown_release": 0.15},
    {"totally.unknown.key": 1},
])
def test_config_validation_rejects_bad_values(cfg, patch):
    with pytest.raises(ConfigValidationError) as exc:
        cfg.with_overrides(patch)
    assert exc.value.exit_code == 4


def test_trading_calendar_comes_from_table_not_bdate_range(store):
    days = store.trading_days()
    assert len(days) == 259
    assert "2022-01-08" not in days               # 周六
    assert "2022-01-09" not in days               # 周日
    assert all(pd.Timestamp(d).weekday() < 5 for d in days)
    assert store.next_trading_day(days[0]) == days[1]
    assert store.next_trading_day(days[-1]) is None
    assert store.calendar_count() > len(days)     # 含 is_open=0 的闭市日
