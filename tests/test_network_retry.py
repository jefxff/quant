"""R-012 / R-003 / R-004：指数退避重试、数据源降级、离线禁止联网。

本文件除 ``@pytest.mark.network`` 的真实 smoke 外，全部使用注入的 ``sleep``
和假数据源，绝不访问网络；``sleep`` 被替换为纯记录函数，因此无真实等待。
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd
import pytest

from quant.data.sources import (
    AKShareSource,
    BaostockSource,
    DataRouter,
    PRICE_COLUMNS,
    PriceSource,
    _ak_map_columns,
    retry_call,
)
from quant.data.store import Store
from quant.data.update import build_sources
from quant.errors import DataIntegrityError, DataUnavailableError, RetryExhausted

START, END = "2022-01-04", "2022-12-30"
NO_SLEEP: Callable[[float], None] = lambda _seconds: None  # noqa: E731


def _frame(source: str = "secondary", dates=("2022-01-04", "2022-01-05")) -> pd.DataFrame:
    rows = [
        {
            "symbol": "600000.SH", "date": d, "open": 10.0, "high": 10.5,
            "low": 9.5, "close": 10.2, "volume": 1_000_000.0,
            "amount": 10_200_000.0, "adj_factor": 1.0, "source": source,
        }
        for d in dates
    ]
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


class CountingSource(PriceSource):
    def __init__(self, name: str, frame: pd.DataFrame | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.name = name
        self.frame = frame if frame is not None else _frame(name)
        self.calls = 0

    def fetch_daily(self, symbols, start, end, adjust="qfq"):
        self.calls += 1
        return self.frame


class FailingSource(PriceSource):
    def __init__(self, name: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.name = name
        self.calls = 0

    def fetch_daily(self, symbols, start, end, adjust="qfq"):
        def _work():
            self.calls += 1
            raise ConnectionError(f"{self.name} 限流/超时")
        return self._retry(_work)          # 用真实退避逻辑，sleep 已注入


class FlakySource(PriceSource):
    """前 n 次失败，之后成功，用于验证单源内部重试。"""

    def __init__(self, failures: int, **kwargs: Any):
        super().__init__(**kwargs)
        self.name = "flaky"
        self.failures = failures
        self.calls = 0

    def fetch_daily(self, symbols, start, end, adjust="qfq"):
        def _work():
            self.calls += 1
            if self.calls <= self.failures:
                raise ConnectionError("临时失败")
            return _frame("flaky")
        return self._retry(_work)


# --------------------------------------------------------------------------- #
# R-012 指数退避
# --------------------------------------------------------------------------- #
def test_backoff_intervals_are_base_times_two_pow():
    intervals: list[float] = []
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("transient")
        return "ok"

    value = retry_call(fn, max_retries=3, base_interval=0.25,
                       source="unit", sleep=intervals.append)
    assert value == "ok"
    assert attempts["n"] == 3
    assert intervals == [0.25, 0.5]           # base * 2^0, base * 2^1


def test_retry_exhaustion_sleeps_and_raises_retry_exhausted():
    intervals: list[float] = []
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        raise TimeoutError("always down")

    with pytest.raises(RetryExhausted) as exc:
        retry_call(fn, max_retries=3, base_interval=0.5,
                   source="unit", sleep=intervals.append)
    assert attempts["n"] == 4                      # max_retries + 1 次尝试
    assert intervals == [0.5, 1.0, 2.0]           # base * 2^0..2^2
    assert exc.value.source == "unit"
    assert exc.value.attempts == 4
    assert isinstance(exc.value.last_error, TimeoutError)


def test_on_retry_callback_and_zero_retries():
    seen: list[tuple[int, float]] = []
    result = retry_call(
        lambda: 42, max_retries=2, base_interval=1.0, source="u",
        sleep=NO_SLEEP, on_retry=lambda n, interval, exc: seen.append((n, interval)),
    )
    assert result == 42 and seen == []            # 一次成功，零重试
    with pytest.raises(RetryExhausted) as exc:
        retry_call(lambda: None, max_retries=0, base_interval=9.0,
                   source="none", sleep=NO_SLEEP)
    assert exc.value.attempts == 1                # 空结果也算失败


def test_empty_dataframe_counts_as_failed_attempt():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        return pd.DataFrame()

    with pytest.raises(RetryExhausted):
        retry_call(fn, max_retries=2, base_interval=0.01,
                   source="empty", sleep=NO_SLEEP)
    assert attempts["n"] == 3


def test_retry_settings_come_from_config(cfg, fixtures_dir, tmp_path):
    tuned = cfg.with_overrides({"data.max_retries": 5, "data.retry_base_interval": 0.3,
                                "data.request_timeout": 7})
    sources = build_sources(tuned, "fixture", fixtures_dir, sleep=NO_SLEEP)
    assert len(sources) == 1 and sources[0].name == "fixture"   # 离线模式绝不隐式联网
    assert sources[0].max_retries == 5
    assert sources[0].retry_base_interval == 0.3
    assert sources[0].timeout == 7
    assert sources[0]._sleep is NO_SLEEP

    ordered = build_sources(cfg, "baostock", sleep=NO_SLEEP)
    assert [s.name for s in ordered] == ["baostock", "akshare"]
    assert isinstance(ordered[0], BaostockSource)
    assert isinstance(ordered[1], AKShareSource)


# --------------------------------------------------------------------------- #
# R-003 降级链 + R-004 离线
# --------------------------------------------------------------------------- #
def test_cache_hit_skips_every_network_source(cfg, store, main_symbols):
    symbol = main_symbols[0]
    sources = [CountingSource("baostock", sleep=NO_SLEEP),
               CountingSource("akshare", sleep=NO_SLEEP)]
    router = DataRouter(store, sources, cfg, allow_network=True, sleep=NO_SLEEP)
    frame = router.fetch_daily([symbol], START, END, page_size=137)
    assert not frame.empty
    assert all(s.calls == 0 for s in sources), "本地缓存已覆盖时不得访问任何网络源"
    assert "local_cache" in router.source_tried


def test_single_source_internal_retry_then_success(tmp_path, cfg):
    db = Store(tmp_path / "retry.db")
    try:
        flaky = FlakySource(failures=2, max_retries=3, retry_base_interval=0.01,
                            sleep=NO_SLEEP)
        router = DataRouter(db, [flaky], cfg, allow_network=True, sleep=NO_SLEEP)
        frame = router.fetch_daily(["600000.SH"], START, END, persist=True)
        assert flaky.calls == 3
        assert set(frame["source"]) == {"flaky"}
        assert db.count_daily() == 2            # 降级成功后确实落地缓存
    finally:
        db.close()


def test_primary_exhausts_retries_then_degrades_to_secondary(tmp_path, cfg):
    db = Store(tmp_path / "degrade.db")
    try:
        primary = FailingSource("baostock", max_retries=2,
                                retry_base_interval=0.01, sleep=NO_SLEEP)
        secondary = CountingSource("akshare", sleep=NO_SLEEP)
        router = DataRouter(db, [primary, secondary], cfg,
                            allow_network=True, sleep=NO_SLEEP)
        frame = router.fetch_daily(["600000.SH"], START, END, persist=False)
        assert primary.calls == 3               # 单源重试耗尽后降级，不崩溃
        assert secondary.calls == 1
        assert set(frame["source"]) == {"akshare"}
        assert any("baostock" in w for w in router.warnings)
        assert "akshare" in [s.name for s in router.sources]
    finally:
        db.close()


def test_all_sources_fail_raises_data_unavailable_with_full_context(tmp_path, cfg):
    db = Store(tmp_path / "unavailable.db")
    try:
        a = FailingSource("baostock", max_retries=1,
                          retry_base_interval=0.01, sleep=NO_SLEEP)
        b = FailingSource("akshare", max_retries=1,
                          retry_base_interval=0.01, sleep=NO_SLEEP)
        router = DataRouter(db, [a, b], cfg, allow_network=True, sleep=NO_SLEEP)
        with pytest.raises(DataUnavailableError) as exc:
            router.fetch_daily(["600000.SH"], START, END)
        err = exc.value
        assert err.exit_code == 2
        assert err.source_tried == ["local_cache", "baostock", "akshare"]
        assert err.symbol == "600000.SH"
        assert err.date_range == f"{START}..{END}"
        for token in ("local_cache", "baostock", "akshare", "600000.SH", START, END):
            assert token in str(err)
    finally:
        db.close()


def test_allow_network_false_never_touches_adapters(tmp_path, cfg):
    """R-004：回测/模拟盘入口下，缓存缺失应直接报错而不是发起网络请求。"""
    db = Store(tmp_path / "offline.db")
    try:
        src = CountingSource("baostock", sleep=NO_SLEEP)
        router = DataRouter(db, [src], cfg, allow_network=False, sleep=NO_SLEEP)
        with pytest.raises(DataUnavailableError) as exc:
            router.fetch_daily(["600000.SH"], START, END, allow_network=False)
        assert src.calls == 0
        assert exc.value.source_tried == ["local_cache"]
        assert "R-004" in exc.value.detail
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# AKShare 字段漂移 / 行业分类降级
# --------------------------------------------------------------------------- #
def test_akshare_column_aliases_and_field_drift():
    cn = pd.DataFrame({"日期": ["2022-01-04"], "开盘": [1.0], "最高": [1.1],
                       "最低": [0.9], "收盘": [1.05], "成交量": [100],
                       "成交额": [105.0]})
    en = pd.DataFrame({"date": ["2022-01-04"], "open": [1.0], "high": [1.1],
                       "low": [0.9], "close": [1.05], "volume": [100],
                       "amount": [105.0]})
    for frame in (cn, en):
        mapped = _ak_map_columns(frame)
        assert list(mapped.columns) == ["date", "open", "high", "low", "close",
                                        "volume", "amount"]
    with pytest.raises(DataIntegrityError, match="字段漂移"):
        _ak_map_columns(cn.drop(columns=["成交额"]))


def test_industry_failure_degrades_to_empty_frame_with_warning(monkeypatch):
    src = AKShareSource(max_retries=1, retry_base_interval=0.01, sleep=NO_SLEEP)

    def _boom():
        raise ConnectionError("东财限流")

    monkeypatch.setattr(src, "_client", _boom)
    frame = src.fetch_industry(["600000.SH"])
    assert frame.empty
    assert list(frame.columns) == ["symbol", "name", "industry", "source"]
    assert any("降级为空" in w for w in src.warnings)


def test_router_industry_all_fail_returns_empty_and_warns(cfg):
    class Bad(PriceSource):
        def fetch_industry(self, symbols):
            raise RuntimeError("no industry")

    a, b = Bad(), Bad()
    a.name, b.name = "baostock", "akshare"
    router = DataRouter(None, [a, b], cfg)
    assert router.fetch_industry(["600000.SH"]).empty
    assert any("行业分类全部失败" in w for w in router.warnings)


@pytest.mark.network
def test_live_baostock_smoke():
    """手动 smoke：``pytest -m network`` 才会访问免费数据源（默认跳过）。"""
    src = BaostockSource(max_retries=1, retry_base_interval=0.5)
    frame = src.fetch_daily(["600000.SH"], "2022-01-04", "2022-01-14")
    assert not frame.empty
    assert "600000.SH" in set(frame["symbol"])
    assert not frame[["open", "high", "low", "close", "adj_factor"]].isna().any().any()
