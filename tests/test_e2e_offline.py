"""完全离线的端到端闭环：fixture -> SQLite -> 回测 -> 模拟盘 -> 报告。

* 不访问任何网络接口（用 socket / 适配器双重护栏证明）。
* 同时校验 D-003/R-011：「假设与限制」由程序按实际运行配置生成。
"""

from __future__ import annotations

import json
import math
import re
import socket
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant import cli
from quant.backtest.engine import BacktestEngine
from quant.data.sources import AKShareSource, BaostockSource
from quant.data.store import Store
from quant.data.update import update_store
from quant.report.metrics import compute_metrics, segment_metrics
from quant.report.render import (
    ASSUMPTIONS_MARKER,
    build_assumptions,
    make_run_id,
    render_backtest,
    show_report,
)

START, END = "2022-01-04", "2022-12-30"


@pytest.fixture
def offline_config(project_root, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({
        "database": {"path": str(tmp_path / "e2e.db")},
        "data": {"source": "fixture", "start": START, "end": END,
                 "limit": 5, "page_size": 137},
        "report": {"out_dir": str(tmp_path / "reports"), "make_png": False},
    }, ensure_ascii=False), encoding="utf-8")
    return path, tmp_path


def _no_network_guard(monkeypatch):
    """任何 socket 连接或网络适配器构造都会让测试立刻失败。"""
    def _forbid_connect(self, *_args, **_kwargs):
        raise AssertionError("离线端到端流程禁止网络连接 (R-004)")

    def _forbid_client(self):
        raise AssertionError("离线端到端流程禁止构造网络数据适配器 (R-004)")

    monkeypatch.setattr(socket.socket, "connect", _forbid_connect)
    monkeypatch.setattr(BaostockSource, "_client", _forbid_client)
    monkeypatch.setattr(AKShareSource, "_client", _forbid_client)


# --------------------------------------------------------------------------- #
# 1. 完整 CLI 闭环（含幂等 / dry-run）
# --------------------------------------------------------------------------- #
def test_full_offline_cli_pipeline(offline_config, monkeypatch):
    cfg_path, tmp = offline_config
    _no_network_guard(monkeypatch)
    base = ["--config", str(cfg_path)]
    db_path = tmp / "e2e.db"

    assert cli.main([*base, "data", "update", "--source", "fixture", "--pool", "hs300",
                     "--limit", "5", "--start", START, "--end", END]) == 0
    assert db_path.exists()

    with Store(db_path) as db:
        assert db.schema_version == 1
        assert db.count_daily() == 5 * 259
        assert len(db.trading_days(START, END)) == 259
        # --limit N = 按代码顺序取前 N，不是随机 N
        syms = db.get_universe("hs300", limit=5, main_only=True)["symbol"].tolist()
        assert syms == ["000001.SZ", "000002.SZ", "000651.SZ",
                        "600000.SH", "600030.SH"]
        assert db.board_counts("hs300")["MAIN"] == 8
        assert set(db.get_universe("hs300", main_only=False)["board"]) == {
            "MAIN", "ST", "CYB", "KCB", "BSE"}
        assert db.count_orders() == 0 and db.count_trades() == 0

    assert cli.main([*base, "data", "status", "--pool", "hs300"]) == 0
    assert cli.main([*base, "backtest", "--strategy", "dual_ma",
                     "--start", START, "--end", END, "--limit", "5"]) == 0

    # --dry-run 完整跑流程但不提交
    assert cli.main([*base, "paper", "run", "--date", "latest", "--dry-run"]) == 0
    with Store(db_path) as db:
        assert db.count_orders() == 0 and db.count_trades() == 0
        assert db.get_equity("dual_ma").empty and db.get_positions("dual_ma") == []

    assert cli.main([*base, "paper", "run", "--date", "latest"]) == 0
    with Store(db_path) as db:
        n_orders, n_trades = db.count_orders(), db.count_trades()
        assert n_orders > 0
        assert len(db.pending_orders("dual_ma")) > 0       # 等待 T+1，不报错
        assert len(db.get_equity("dual_ma")) == 1

    # 同日重复运行幂等
    assert cli.main([*base, "paper", "run", "--date", "latest"]) == 0
    with Store(db_path) as db:
        assert (db.count_orders(), db.count_trades()) == (n_orders, n_trades)
        assert len(db.get_equity("dual_ma")) == 1

    assert cli.main([*base, "paper", "status"]) == 0
    assert cli.main([*base, "report", "--run-id", "latest"]) == 0

    # 报告产物
    reports = tmp / "reports"
    pointer = (reports / "latest").read_text(encoding="utf-8").strip()
    assert pointer
    assert (reports / pointer).is_dir()
    run_dirs = [p for p in reports.iterdir() if p.is_dir()]
    assert any(json.loads((p / "run_meta.json").read_text(encoding="utf-8"))["kind"] == "backtest"
               for p in run_dirs)
    for p in run_dirs:
        for name in ("report.md", "run_meta.json", "assumptions.json",
                     "trades.csv", "equity.csv", "orders.csv", "positions.csv", "costs.csv"):
            assert (p / name).exists(), f"{p.name} 缺 {name}"
        meta = json.loads((p / "run_meta.json").read_text(encoding="utf-8"))
        assert set(("full", "in_sample", "out_of_sample")) <= set(meta["metrics"])
        assert meta["assumptions"] == json.loads((p / "assumptions.json").read_text(encoding="utf-8"))

    md = (reports / pointer / "report.md").read_text(encoding="utf-8")
    for token in (ASSUMPTIONS_MARKER, "假设与限制", "复权口径", "涨跌停", "停牌",
                  "样本外", "幸存者偏差", "未接入实盘", "未使用付费 API",
                  "miniQMT", "expected_days", "actual_days", "missing_days"):
        assert token in md, f"报告缺少: {token}"


# --------------------------------------------------------------------------- #
# 2. Python API 闭环 + 程序化假设
# --------------------------------------------------------------------------- #
def test_python_api_pipeline_and_report_content(cfg, store, main_symbols):
    result = BacktestEngine(cfg, store, run_id=make_run_id("dual_ma", START, END)).run(
        START, END, main_symbols
    )
    info = render_backtest(result, cfg, store)
    run_dir = Path(info["run_dir"])
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert ASSUMPTIONS_MARKER in md
    assert "未接入实盘、未使用付费 API、miniQMT 不在首版范围" in md
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["metrics"]["out_of_sample"]["sample_days"] > 0
    assert meta["metrics"]["in_sample"]["sample_days"] > 0
    assert meta["audit"]["expected_days"] > 0
    assert meta["audit"]["missing_days"] == 0
    assert store.get_run(info["run_id"])["kind"] == "backtest"

    # latest 指针 + report --run-id latest 重新渲染
    shown = show_report(cfg, "latest")
    assert shown["run_id"] == info["run_id"]
    assert shown["assumptions_count"] == len(meta["assumptions"])
    assert ASSUMPTIONS_MARKER in Path(shown["report_md"]).read_text(encoding="utf-8")


def test_assumptions_reflect_runtime_config_not_hardcoded_text(cfg):
    tuned = cfg.with_overrides({
        "backtest.out_of_sample_ratio": 0.30,
        "rules.max_suspension_days": 5,
        "data.page_size": 250,
        "data.max_retries": 6,
        "costs.slippage_rate": 0.0002,
    })
    audit = {
        "boards": {"MAIN": 8, "CYB": 1, "KCB": 1, "BSE": 1, "ST": 1},
        "expected_days": 1295, "actual_days": 1288, "missing_days": 7,
        "calendar_days": 259, "adj_factor_missing": 3, "sources": ["fixture"],
        "survivorship_bias_warning": "SURVIVORSHIP_TOKEN",
    }
    text = "\n".join(build_assumptions(tuned, audit, kind="backtest",
                                       extra={"oos_start": "2022-09-01",
                                              "in_sample_days": 181, "oos_days": 78}))
    for token in ("30%", "2022-09-01", "181", "78", "5 个交易日", "250 条/批",
                  "最大重试 6 次", "万 2.00", "±10%", "创业板(20%)",
                  "missing_days=7", "adj_factor 缺失记录数：3",
                  "SURVIVORSHIP_TOKEN", "CYB"):
        assert token in text, f"程序化假设未反映配置/审计: {token}"
    assert re.fullmatch(r"dual_ma_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_\d+",
                        make_run_id("dual_ma", START, END))


def test_update_store_fixture_audit_and_limit_semantics(cfg, store, fixtures_dir):
    """update_store 在已有库上重复运行应幂等，并输出完整缺失审计。"""
    before = store.count_daily()
    tuned = cfg.with_overrides({"data.start": START, "data.end": END,
                                "data.source": "fixture", "data.limit": 3})
    result = update_store(tuned, store, source="fixture", pool="hs300", limit=3,
                          start=START, end=END, fixture_dir=fixtures_dir)
    assert result.source == "fixture"
    assert result.symbols == ["000001.SZ", "000002.SZ", "000651.SZ"]
    assert result.rows_written == 3 * len(store.trading_days(START, END))
    assert result.audit["expected_days"] == 3 * len(store.trading_days(START, END))
    assert result.audit["actual_days"] == result.audit["expected_days"]
    assert result.audit["missing_days"] == 0
    assert result.audit["boards"]["MAIN"] == 8
    assert "幸存者偏差" in result.audit["survivorship_bias_warning"]
    assert store.count_daily() == before          # UPSERT，不重复插入


# --------------------------------------------------------------------------- #
# 3. 指标公式
# --------------------------------------------------------------------------- #
def test_metric_formulas_are_fixed(cfg):
    equity = pd.DataFrame({
        "date": pd.date_range("2022-01-04", periods=4).strftime("%Y-%m-%d"),
        "equity": [1000.0, 1010.0, 999.9, 1009.899],
    })
    trades = pd.DataFrame([
        {"trade_id": "t1", "fill_date": "2022-01-04", "symbol": "A", "side": "BUY",
         "qty": 100, "price": 10.0, "commission": 5.0, "stamp_tax": 0.0, "slippage_cost": 0.0},
        {"trade_id": "t2", "fill_date": "2022-01-05", "symbol": "A", "side": "SELL",
         "qty": 100, "price": 11.0, "commission": 5.0, "stamp_tax": 0.55, "slippage_cost": 0.0},
        {"trade_id": "t3", "fill_date": "2022-01-06", "symbol": "A", "side": "BUY",
         "qty": 100, "price": 10.0, "commission": 5.0, "stamp_tax": 0.0, "slippage_cost": 0.0},
        {"trade_id": "t4", "fill_date": "2022-01-07", "symbol": "A", "side": "SELL",
         "qty": 100, "price": 9.0, "commission": 5.0, "stamp_tax": 0.45, "slippage_cost": 0.0},
    ])
    m = compute_metrics(equity, trades, 1000.0, cfg)
    fees = 5.0 + 5.55 + 5.0 + 5.45
    assert m["trades"] == 4 and m["closed_trades"] == 2
    assert m["win_rate"] == pytest.approx(0.5)             # 按已平仓 FIFO 配对
    assert m["cost_ratio"] == pytest.approx(fees / 1000.0)  # 总费用 / 初始资金
    assert m["turnover"] == pytest.approx(4000.0 / float(equity["equity"].mean()))
    assert m["cum_return"] == pytest.approx(1009.899 / 1000.0 - 1.0)
    assert m["annual_return"] == pytest.approx((1009.899 / 1000.0) ** (252.0 / 4) - 1.0)
    assert m["max_drawdown"] == pytest.approx((1010.0 - 999.9) / 1010.0)
    rets = np.diff(equity["equity"].to_numpy()) / equity["equity"].to_numpy()[:-1]
    assert m["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * math.sqrt(252))

    seg = segment_metrics(equity, trades, 1000.0, cfg, "2022-01-07")
    assert seg["in_sample"]["sample_days"] == 3
    assert seg["out_of_sample"]["sample_days"] == 1
    assert seg["full"]["trades"] == 4
