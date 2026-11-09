# -*- coding: utf-8 -*-
"""监控台「策略展示 / 后续 5 日情景推演」数据层的离线契约测试。

全部使用构造的内存净值序列，不联网、不读真实行情、不改动任何运行期文件。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dashboard_build", ROOT / "dashboard" / "build.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _equity(n: int = 200, seed: int = 7) -> list[list]:
    """构造一条仅用于测试的净值序列（非行情数据）。"""
    rng = np.random.default_rng(seed)
    nav, bench, rows = 1.0, 1.0, []
    for _ in range(n):
        nav *= 1 + rng.normal(0.0004, 0.008)
        bench *= 1 + rng.normal(0.0002, 0.010)
        rows.append([nav, bench, 0.02, 1_000_000.0, 0.5, 3000.0, 0])
    return rows


def test_forecast_shape_and_quantile_order():
    fc = build.build_forecast(_equity())
    assert fc["days"] == 5 and fc["paths"] == 800 and fc["window"] >= 120
    data = fc["data"]
    assert len(data) == 200
    # 样本不足 30 个交易日时不推演，不编造数据
    assert all(x is None for x in data[:30])
    last = data[-1]
    assert len(last[0]) == fc["days"]
    for q in last[0]:
        assert q[0] <= q[1] <= q[2], "P10 ≤ P50 ≤ P90 必须成立"
        assert q[3] > 0, "基准 P50 相对倍数必须为正"
    for p in last[1:]:
        assert 0 <= p <= 2
    assert "不是行情预测" in fc["disclaimer"]


def test_forecast_never_uses_future_returns():
    """改写 i=120 之后的净值，i<120 的所有推演检查点必须逐字节不变。"""
    eq = _equity(200)
    before = build.build_forecast(eq)["data"]
    mutated = [list(r) for r in eq]
    for i in range(120, 200):
        mutated[i][0] *= 1.5
    after = build.build_forecast(mutated)["data"]
    for i in range(120):
        assert before[i] == after[i], f"第 {i} 日推演被未来收益污染"
    assert before[-1] != after[-1], "最新检查点应随其自身历史窗口变化"


def test_forecast_is_reproducible_and_reacts_to_history():
    eq = _equity(150)
    assert build.build_forecast(eq)["data"] == build.build_forecast(eq)["data"]
    changed = [list(r) for r in eq]
    changed[100][0] *= 0.5
    changed[101][0] *= 2.0
    assert build.build_forecast(eq)["data"][-1] != build.build_forecast(changed)["data"][-1]


def test_strategy_payload_is_complete():
    bt = {
        "signal_state": [[3, 0.3, 0, 2, 1]] * 100,
        "signal_rows": [[0, 1.2, 1.1, 1, 0.2, 0.08, 0.05, 1, 10.0]],
        "signal_date": "2026-11-09",
    }
    st = build.build_strategy(bt)
    assert st["id"] == "dual_ma" and "双均线" in st["name"]
    assert len(st["params"]) >= 8 and len(st["rules"]) >= 8
    assert all(len(p) == 3 for p in st["params"])
    assert all(len(r) == 2 for r in st["rules"])
    assert st["signal_state"] == bt["signal_state"]
    assert st["signal_rows"] == bt["signal_rows"]
    assert st["rebalance_every"] == 5 and st["deadband"] == 0.02


def test_delivered_snapshot_and_page_carry_strategy_and_forecast():
    snap_path = ROOT / "dashboard" / "snapshot.json"
    page = ROOT / "dashboard" / "index.html"
    if not snap_path.exists() or not page.exists():
        pytest.skip("尚未运行 dashboard/build.py 生成交付物")
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap["strategy"]["id"] == "dual_ma"
    assert snap["forecast"]["days"] == 5
    assert len(snap["forecast"]["data"]) == len(snap["dates"])
    assert len(snap["strategy"]["signal_state"]) == len(snap["dates"])
    assert any("情景推演" in n for n in snap["notes"])
    html = page.read_text(encoding="utf-8")
    assert "const SNAP = " in html and "/*__SNAPSHOT__*/" not in html
    assert 'id="fcBody"' in html and 'id="sigBody"' in html


def test_forecast_curve_is_shipped_and_uses_a_local_axis():
    """5 日曲线必须随交付页一起发布，且使用独立局部纵轴。

    回归背景：全历史净值轴跨度约 0.9~1.8，而 5 日情景只有 ±2%，
    直接画在同一轴上只有 3~5px，视觉上等于「没有曲线」。
    """
    tpl_path = ROOT / "dashboard" / "template.html"
    page = ROOT / "dashboard" / "index.html"
    snap_path = ROOT / "dashboard" / "snapshot.json"
    if not (tpl_path.exists() and page.exists() and snap_path.exists()):
        pytest.skip("尚未运行 dashboard/build.py 生成交付物")
    tpl = tpl_path.read_text(encoding="utf-8")
    html = page.read_text(encoding="utf-8")
    for marker in ('id="fcChart"', 'function renderFcChart', 'renderFcChart(f,i,wd)',
                   'class="fc-p50"', 'fc-band2', 'fc-inset', '局部轴'):
        assert marker in tpl, f"模板缺少 5 日曲线标记：{marker}"
        assert marker in html, f"交付页缺少 5 日曲线标记：{marker}"
    # 主图未来区 + 卡片专用图都必须有自己的缩放函数，不复用全历史 Y()
    assert "var YF=function" in tpl and "function PY(v)" in tpl

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    rows = snap["forecast"]["data"][-1][0]
    lo = min(min(q[0], q[3]) for q in rows)
    hi = max(max(q[2], q[3]) for q in rows)
    span = max(abs(lo - 1), abs(hi - 1), 0.006) * 1.25
    assert (hi - lo) / (2 * span) > 0.25, "局部轴上情景带应占据足够的纵向高度"

    eq = snap["equity"]
    ymin = min(min(r[0], r[1]) for r in eq)
    ymax = max(max(r[0], r[1]) for r in eq)
    base = eq[-1][0]
    # 全历史轴上：整个 P10–P90 带不足绘图高度的 10%，P50 主线更不足 1%
    assert (hi - lo) * base / (ymax - ymin) < 0.10
    p50 = [q[1] for q in rows] + [1.0]
    assert (max(p50) - min(p50)) * base / (ymax - ymin) < 0.01
