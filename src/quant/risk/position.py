"""独立风控层：仓位上限 + 回撤刹车状态机。

写死策略：回撤 >= 10% -> 总仓位上限由 80% **降仓**至 40%（而非暂停开仓）。
解除条件（二选一即生效）：回撤修复至 5% 以下 **或** 进入刹车状态满 N 个
交易日（默认 10，可配置）。状态机需覆盖「触发 -> 降仓 -> 解除」全流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["RiskStateMachine", "apply_risk", "drawdown"]


def drawdown(peak_equity: float, equity: float) -> float:
    if peak_equity <= 0:
        return 0.0
    return max(0.0, (float(peak_equity) - float(equity)) / float(peak_equity))


@dataclass
class RiskStateMachine:
    cfg: Any
    mode: str = "NORMAL"          # NORMAL | BRAKED
    days_in_brake: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trigger(self) -> float:
        return float(self.cfg.get("risk.drawdown_trigger", 0.10))

    @property
    def release(self) -> float:
        return float(self.cfg.get("risk.drawdown_release", 0.05))

    @property
    def release_days(self) -> int:
        return int(self.cfg.get("risk.release_days", 10))

    def total_cap(self) -> float:
        full = float(self.cfg.get("risk.max_total_weight", 0.80))
        braked = float(self.cfg.get("risk.braked_max_total_weight", 0.40))
        return braked if self.mode == "BRAKED" else full

    def update(self, current_drawdown: float, date: str) -> str:
        """Advance one trading day. Returns the resulting mode."""
        dd = float(current_drawdown)
        before = self.mode
        if self.mode == "NORMAL":
            if dd >= self.trigger:
                self.mode = "BRAKED"
                self.days_in_brake = 0
        else:
            self.days_in_brake += 1
            if dd <= self.release or self.days_in_brake >= self.release_days:
                self.mode = "NORMAL"
                self.days_in_brake = 0
        self.history.append({
            "date": date, "drawdown": dd, "from": before, "to": self.mode,
            "days_in_brake": self.days_in_brake, "total_cap": self.total_cap(),
        })
        return self.mode

    def snapshot(self) -> dict[str, Any]:
        return {
            "risk_mode": self.mode,
            "risk_days": self.days_in_brake,
            "total_cap": self.total_cap(),
            "trigger": self.trigger,
            "release": self.release,
            "release_days": self.release_days,
        }

    @classmethod
    def restore(cls, cfg: Any, state: Mapping[str, Any] | None) -> "RiskStateMachine":
        if not state:
            return cls(cfg)
        return cls(
            cfg,
            mode=str(state.get("risk_mode", "NORMAL")),
            days_in_brake=int(state.get("risk_days", 0)),
        )


def apply_risk(
    target_weights: Mapping[str, float],
    cfg: Any,
    risk: RiskStateMachine,
    *,
    equity: float = 0.0,
) -> dict[str, float]:
    """单票上限 -> 总仓位上限（按比例缩放，不新增标的）。"""
    single_cap = float(cfg.get("risk.max_single_weight", 0.10))
    total_cap = risk.total_cap()
    capped = {str(s): max(0.0, min(float(w), single_cap)) for s, w in target_weights.items()}
    total = sum(capped.values())
    if total > total_cap and total > 0:
        scale = total_cap / total
        capped = {s: w * scale for s, w in capped.items()}
    return capped
