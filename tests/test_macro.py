# -*- coding: utf-8 -*-
"""「政策 · 外围市场 · 传导」层的离线契约测试。

全部使用构造的内存序列，不联网、不读写运行期缓存、不改动任何交付物。
核心约束：真实数据、异常值不填补、严格无未来函数、样本不足不编造。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


macro = _load("dashboard_macro", "dashboard/macro.py")


def test_policy_events_are_dated_and_sourced():
    evs = macro.POLICY_EVENTS + macro.GLOBAL_EVENTS
    assert len(evs) >= 7
    for e in evs:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["date"]), e
        assert e["title"] and e["detail"] and e["source"] and e["tag"]
        assert e["url"].startswith("https://")
        assert e["tone"] in {"support", "tighten", "risk", "neutral"}
    assert all(e["date"] >= "2026-11-01" for e in macro.POLICY_EVENTS), "只收录可核实日期的近期政策"


def test_clean_rows_drops_zero_and_duplicates_without_filling():
    df = pd.DataFrame({
        "date": ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-06", "2026-11-09"],
        "close": [100.0, 0.0, 98.5, 98.7, None],
    })
    rows, quality = macro._clean_rows(df, "2026-11-01")
    assert rows == [["2026-11-04", 100.0], ["2026-11-06", 98.7]]
    assert quality and "剔除" in quality[0]
    assert all(c > 0 for _, c in rows), "0 / 缺失必须剔除，绝不填补或插值"


def test_align_uses_only_strictly_past_closes():
    rows = [["2024-01-01", 100.0], ["2024-01-02", 110.0], ["2024-01-03", 99.0]]
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    le, le_dates, shock = macro._align(rows, dates)
    assert le == [110.0, 99.0, 99.0]
    assert le_dates == ["2024-01-02", "2024-01-03", "2024-01-03"]
    assert shock[0] is None                       # 严格早于首日的数据不足两个
    assert shock[1] == pytest.approx(0.10)        # 110/100 - 1，只用到 1/1、1/2
    assert shock[2] == pytest.approx(99 / 110 - 1)
    # 1/4 没有行情时，走势图保留 1/3 的最后值，不向前填出 1/4 的点
    assert le_dates[2] != "2024-01-04"


def _synthetic(n: int = 420, seed: int = 11):
    rng = np.random.default_rng(seed)
    nav = np.cumprod(1.0 + rng.normal(0.0004, 0.009, n))
    composite = [None] + [float(x) for x in rng.normal(0, 0.013, n - 1)]
    for i in range(20, n, 11):                    # 制造足够多的「外围承压」起点
        composite[i] = -0.031
    return nav, composite


def test_conditional_forecast_is_reproducible_and_quantiles_ordered():
    nav, comp = _synthetic()
    fc = macro.build_conditional_forecast(nav, comp)
    assert fc["data"] == macro.build_conditional_forecast(nav, comp)["data"]
    valid = [x for x in fc["data"] if x]
    assert len(valid) > 50
    assert fc["window"] == 250 and fc["paths"] == 800
    for item in valid:
        assert len(item[0]) == 5 and item[2] >= fc["min_samples"]
        for q in item[0]:
            assert q[0] <= q[1] <= q[2], "P10 ≤ P50 ≤ P90"
        assert 0 <= item[1] <= 1


def test_conditional_forecast_never_uses_future_returns():
    """改写第 220 日之后的净值与外围冲击，之前所有检查点必须逐字节不变。"""
    nav, comp = _synthetic()
    before = macro.build_conditional_forecast(nav, comp)["data"]
    nav2 = nav.copy()
    nav2[220:] *= 1.5
    comp2 = list(comp)
    for i in range(220, len(comp2)):
        comp2[i] = 0.04
    after = macro.build_conditional_forecast(nav2, comp2)["data"]
    for i in range(220):
        assert before[i] == after[i], f"第 {i} 日条件情景被未来数据污染"
    assert before[-1] != after[-1]


def test_conditional_forecast_refuses_to_invent_when_samples_are_missing():
    nav, _ = _synthetic(200)
    fc = macro.build_conditional_forecast(nav, [None] * 200)
    assert all(x is None for x in fc["data"]), "样本不足必须留空，不能合成行情"


def _fake_series(dates):
    out = {}
    for seed, (k, base) in enumerate([("spx", 5000), ("ndx", 15000),
                                      ("nky", 38000), ("ks11", 2600)]):
        rng = np.random.default_rng(seed + 1)
        levels = base * np.cumprod(1 + rng.normal(0.0003, 0.011, len(dates)))
        out[k] = {
            "key": k, "name": k, "region": "test", "source": "test",
            "rows": [[d, round(float(v), 2)] for d, v in zip(dates, levels)],
            "quality": [], "error": "",
        }
    return out


def test_build_macro_shape_and_boundary():
    dates = pd.bdate_range("2024-01-02", periods=320).strftime("%Y-%m-%d").tolist()
    rng = np.random.default_rng(5)
    bench = 3000 * np.cumprod(1 + rng.normal(0.0003, 0.009, len(dates)))
    nav = np.cumprod(1 + rng.normal(0.0004, 0.008, len(dates)))
    index_close = dict(zip(dates, [float(x) for x in bench]))
    equity = [[float(a), float(b)] for a, b in zip(nav, bench)]
    m = macro.build_macro(_fake_series(dates), dates, index_close, equity)
    assert m["available_count"] == 4 and len(m["latest"]) == 4
    assert set(m["curves"]) == {"hs300", "spx", "ndx", "nky", "ks11"}
    assert all(len(v) == len(dates) for v in m["curves"].values())
    assert len(m["roll_corr"]) == len(dates) and len(m["conditional"]["data"]) == len(dates)
    assert len(m["composite"]) == len(dates)
    assert set(m["stats"]) == {"spx", "ndx", "nky", "ks11"}
    assert all("samples" in v for v in m["stats"].values())
    assert len(m["events"]) == len(macro.POLICY_EVENTS) + len(macro.GLOBAL_EVENTS)
    assert all("bench" in e and "nav" in e for e in m["events"])
    assert "未进入回测信号" in m["boundary"]


def test_delivered_snapshot_and_page_carry_macro_layer():
    snap_path = ROOT / "dashboard" / "snapshot.json"
    page = ROOT / "dashboard" / "index.html"
    tpl = ROOT / "dashboard" / "template.html"
    if not (snap_path.exists() and page.exists()):
        pytest.skip("尚未运行 dashboard/build.py 生成交付物")
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert "macro" in snap
    m = snap["macro"]
    assert m["available_count"] >= 1
    assert len(m["conditional"]["data"]) == len(snap["dates"])
    assert any("政策" in n for n in snap["notes"])
    html = page.read_text(encoding="utf-8")
    source = tpl.read_text(encoding="utf-8")
    for marker in ('id="macroBar"', 'id="macroChart"', 'id="mcStats"',
                   'id="mcEvents"', 'id="mcWindows"', 'id="fcCond"',
                   'function renderMacroChart', 'function renderFcCond'):
        assert marker in source, f"模板缺少宏观层标记：{marker}"
        assert marker in html, f"交付页缺少宏观层标记：{marker}"
    assert len(re.findall(r'(?:src|href)="https?://', html)) == 0, "页面须保持零外链"
