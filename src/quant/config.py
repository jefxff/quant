"""Configuration loading with the fixed priority chain:

    CLI  >  environment (QUANT_*)  >  config.yaml  >  built-in defaults

After merging, a schema validation pass runs. Bad config -> ConfigValidationError
(exit code 4).  No silent coercion of garbage: unknown keys and bad ranges fail.
"""

from __future__ import annotations

import copy
import datetime as _dt
import os
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigValidationError

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

__all__ = ["Config", "DEFAULT_CONFIG", "load_config", "find_project_root"]

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {"path": "data/quant.db"},
    "data": {
        "source": "baostock",
        "fallback_sources": ["akshare"],
        "pool": "hs300",
        "adjust": "qfq",
        "start": "2018-01-01",
        "end": "2025-12-31",
        "limit": 30,
        "max_retries": 3,
        "retry_base_interval": 1.0,
        "page_size": 500,
        "request_timeout": 20,
    },
    "universe": {"exclude_non_main": True},
    "costs": {
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_tax_rate": 0.0005,
        "slippage_rate": 0.0001,
    },
    "rules": {
        "lot_size": 100,
        "price_limit_pct": 0.10,
        "max_suspension_days": 3,
        "suspension_expiry_days": 3,
    },
    "backtest": {
        "strategy": "dual_ma",
        "initial_cash": 1_000_000.0,
        "out_of_sample_ratio": 0.20,
        "short_window": 20,
        "long_window": 60,
        "vol_lookback": 20,
        "vol_target": 0.15,
        "risk_free_rate": 0.0,
    },
    "risk": {
        "max_single_weight": 0.10,
        "max_total_weight": 0.80,
        "drawdown_trigger": 0.10,
        "braked_max_total_weight": 0.40,
        "drawdown_release": 0.05,
        "release_days": 10,
    },
    "paper": {"strategy": "dual_ma", "initial_cash": 1_000_000.0},
    "report": {"out_dir": "reports", "make_png": True},
}

# Environment aliases. Generic form is QUANT_<SECTION>__<KEY>, e.g.
# QUANT_DATA__LIMIT=10 ; these short aliases are accepted as a convenience.
_ENV_ALIASES: dict[str, str] = {
    "QUANT_DB_PATH": "database.path",
    "QUANT_DATA_SOURCE": "data.source",
    "QUANT_POOL": "data.pool",
    "QUANT_LIMIT": "data.limit",
    "QUANT_START": "data.start",
    "QUANT_END": "data.end",
    "QUANT_PAGE_SIZE": "data.page_size",
    "QUANT_MAX_RETRIES": "data.max_retries",
    "QUANT_STRATEGY": "backtest.strategy",
}


def find_project_root(start: Path | str | None = None) -> Path:
    """Walk upwards until a pyproject.toml / config.yaml is found."""
    here = Path(start or Path.cwd()).resolve()
    if here.is_file():
        here = here.parent
    for candidate in [here, *here.parents]:
        if (
            (candidate / "pyproject.toml").exists()
            or (candidate / "config.yaml").exists()
            or (candidate / "src" / "quant" / "__init__.py").exists()
        ):
            return candidate
    return here


class Config:
    """Thin mapping wrapper with dotted-path access."""

    def __init__(self, values: Mapping[str, Any], root: Path | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(dict(values))
        self.root = Path(root) if root else find_project_root()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise KeyError(dotted)
        return value

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def path(self, dotted: str) -> Path:
        """Resolve a config path relative to the project root."""
        value = Path(str(self.get(dotted)))
        return value if value.is_absolute() else (self.root / value)

    def with_overrides(self, overrides: Mapping[str, Any]) -> "Config":
        merged = _deep_merge(self.as_dict(), _unflatten(overrides))
        cfg = Config(merged, self.root)
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------ #
    def validate(self) -> "Config":
        """Schema validation. Raises ConfigValidationError on any problem."""
        errors: list[str] = []
        known = set(_flatten(DEFAULT_CONFIG))
        actual = set(_flatten(self._data))
        for key in sorted(actual - known):
            errors.append(f"未知配置项: {key}")

        def num(path: str, lo: float | None = None, hi: float | None = None) -> float:
            try:
                v = float(self.get(path))
            except (TypeError, ValueError):
                errors.append(f"{path} 必须是数字，实际={self.get(path)!r}")
                return float("nan")
            if lo is not None and v < lo:
                errors.append(f"{path} 必须 >= {lo}，实际={v}")
            if hi is not None and v > hi:
                errors.append(f"{path} 必须 <= {hi}，实际={v}")
            return v

        def integer(path: str, lo: int = 0) -> int:
            v = num(path, lo)
            if v != int(v):
                errors.append(f"{path} 必须是整数，实际={v}")
            return int(v)

        # data
        if self.get("data.source") not in ("baostock", "akshare", "fixture"):
            errors.append("data.source 必须是 baostock / akshare / fixture")
        if self.get("data.adjust") != "qfq":
            errors.append("data.adjust 首版仅支持 qfq（存储仍为不复权 OHLCV + adj_factor）")
        fallbacks = self.get("data.fallback_sources") or []
        if not isinstance(fallbacks, (list, tuple)):
            errors.append("data.fallback_sources 必须是列表")
        elif any(x not in ("baostock", "akshare") for x in fallbacks):
            errors.append("data.fallback_sources 只能包含 baostock / akshare")
        integer("data.limit", 0)
        integer("data.max_retries", 0)
        num("data.retry_base_interval", 0.0)
        integer("data.page_size", 1)
        integer("data.request_timeout", 1)
        try:
            start = _parse_date(self.get("data.start"))
            end = _parse_date(self.get("data.end"))
            if start > end:
                errors.append("data.start 必须 <= data.end")
        except ValueError as exc:
            errors.append(f"日期格式错误（应为 YYYY-MM-DD）: {exc}")

        # costs / rules
        num("costs.commission_rate", 0.0)
        num("costs.commission_min", 0.0)
        num("costs.stamp_tax_rate", 0.0)
        num("costs.slippage_rate", 0.0)
        pct = num("rules.price_limit_pct", 0.0, 0.5)
        integer("rules.lot_size", 1)
        integer("rules.max_suspension_days", 0)
        integer("rules.suspension_expiry_days", 0)
        if abs(pct - 0.10) > 1e-12:
            errors.append("首版仅支持沪深主板 ±10%，rules.price_limit_pct 必须为 0.10")

        # backtest
        num("backtest.initial_cash", 1.0)
        num("backtest.out_of_sample_ratio", 0.0, 0.5)
        sw = integer("backtest.short_window", 2)
        lw = integer("backtest.long_window", 3)
        integer("backtest.vol_lookback", 2)
        num("backtest.vol_target", 0.0)
        num("backtest.risk_free_rate", 0.0)
        if sw >= lw:
            errors.append("backtest.short_window 必须 < backtest.long_window")
        if self.get("backtest.strategy") not in ("dual_ma",):
            errors.append("backtest.strategy 首版仅支持 dual_ma")

        # risk
        ms = num("risk.max_single_weight", 0.0, 1.0)
        mt = num("risk.max_total_weight", 0.0, 1.0)
        bt = num("risk.braked_max_total_weight", 0.0, 1.0)
        trig = num("risk.drawdown_trigger", 0.0, 1.0)
        rel = num("risk.drawdown_release", 0.0, 1.0)
        integer("risk.release_days", 1)
        if ms > mt:
            errors.append("risk.max_single_weight 不能大于 risk.max_total_weight")
        if bt > mt:
            errors.append("risk.braked_max_total_weight 不能大于 risk.max_total_weight")
        if rel >= trig:
            errors.append("risk.drawdown_release 必须 < risk.drawdown_trigger")

        num("paper.initial_cash", 1.0)
        if not isinstance(self.get("report.make_png"), bool):
            errors.append("report.make_png 必须是布尔值")
        if not isinstance(self.get("universe.exclude_non_main"), bool):
            errors.append("universe.exclude_non_main 必须是布尔值")

        if errors:
            raise ConfigValidationError("配置校验失败:\n  - " + "\n  - ".join(errors))
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return f"Config(root={self.root!s}, data={self._data!r})"


def _parse_date(value: Any) -> _dt.date:
    return _dt.date.fromisoformat(str(value))


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _unflatten(overrides: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dotted, value in overrides.items():
        if value is None:  # CLI omitted -> do not override
            continue
        parts = str(dotted).split(".")
        node = out
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):  # conflicting override
                node = {}
        node[parts[-1]] = value
    return out


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(value: str, template: Any) -> Any:
    """Coerce an environment/CLI string to the type of the default value."""
    if isinstance(template, bool):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(template, int) and not isinstance(template, bool):
        return int(value)
    if isinstance(template, float):
        return float(value)
    if isinstance(template, (list, tuple)):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _env_overrides(env: Mapping[str, str] | None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith("QUANT_"):
            continue
        dotted = _ENV_ALIASES.get(name)
        if dotted is None:
            if "__" not in name:
                continue
            _, _, tail = name.partition("QUANT_")
            dotted = ".".join(part.lower() for part in tail.split("__"))
        template = _flatten(DEFAULT_CONFIG).get(dotted, "")
        try:
            out[dotted] = _coerce(raw, template)
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"环境变量 {name}={raw!r} 无法解析: {exc}") from exc
    return out


def load_config(
    path: Path | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    root: Path | str | None = None,
) -> Config:
    """Merge defaults -> yaml -> env -> CLI overrides, then validate."""
    root_path = Path(root) if root else find_project_root()
    yaml_path = Path(path) if path else (root_path / "config.yaml")
    if not yaml_path.is_absolute():
        yaml_path = root_path / yaml_path

    merged = copy.deepcopy(DEFAULT_CONFIG)
    if yaml_path.exists():
        if yaml is None:
            raise ConfigValidationError("缺少 PyYAML，无法读取 config.yaml")
        with yaml_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, Mapping):
            raise ConfigValidationError(f"{yaml_path} 顶层必须是映射")
        merged = _deep_merge(merged, loaded)
    elif path is not None:
        raise ConfigValidationError(f"指定的配置文件不存在: {yaml_path}")

    merged = _deep_merge(merged, _unflatten(_env_overrides(env)))
    merged = _deep_merge(merged, _unflatten(overrides or {}))
    cfg = Config(merged, root_path)
    cfg.validate()
    return cfg
