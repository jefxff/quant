"""回撤刹车状态机：触发 -> 降仓 -> 解除 全流程。"""

from quant.risk.position import RiskStateMachine, apply_risk, drawdown


def test_drawdown_math():
    assert drawdown(100.0, 90.0) == 0.10
    assert drawdown(100.0, 110.0) == 0.0
    assert drawdown(0.0, 50.0) == 0.0


def test_trigger_brake_and_release_by_drawdown_repair(cfg):
    risk = RiskStateMachine(cfg)
    assert risk.mode == "NORMAL"
    assert risk.total_cap() == 0.80

    assert risk.update(0.02, "d1") == "NORMAL"
    assert risk.update(0.10, "d2") == "BRAKED"          # >= trigger 触发
    assert risk.total_cap() == 0.40                      # 降仓而非暂停开仓
    assert risk.update(0.08, "d3") == "BRAKED"          # 未修复、未满 N 日
    assert risk.update(0.07, "d4") == "BRAKED"
    assert risk.update(0.04, "d5") == "NORMAL"          # <= release 解除
    assert risk.total_cap() == 0.80
    assert risk.days_in_brake == 0
    assert [h["to"] for h in risk.history] == ["NORMAL", "BRAKED", "BRAKED", "BRAKED", "NORMAL"]


def test_release_by_time_even_without_full_repair(cfg):
    risk = RiskStateMachine(cfg)
    risk.update(0.15, "d0")
    release_days = int(cfg.get("risk.release_days"))
    for i in range(1, release_days):
        assert risk.update(0.08, f"d{i}") == "BRAKED"
    assert risk.update(0.08, f"d{release_days}") == "NORMAL"


def test_apply_risk_single_and_total_caps(cfg):
    risk = RiskStateMachine(cfg)
    raw = {f"{i:06d}.SZ": 0.5 for i in range(12)}
    capped = apply_risk(raw, cfg, risk)
    assert max(capped.values()) <= float(cfg.get("risk.max_single_weight")) + 1e-12
    assert abs(sum(capped.values()) - 0.80) < 1e-9

    risk.update(0.12, "d1")
    braked = apply_risk(raw, cfg, risk)
    assert abs(sum(braked.values()) - 0.40) < 1e-9
    assert all(braked[k] < capped[k] for k in capped)


def test_restore_snapshot(cfg):
    risk = RiskStateMachine(cfg)
    risk.update(0.12, "d1")
    snap = risk.snapshot()
    restored = RiskStateMachine.restore(cfg, snap)
    assert restored.mode == "BRAKED" and restored.total_cap() == 0.40
