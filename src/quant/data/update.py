"""数据更新入口 + 缺失交易日审计。

手动 smoke 命令 (``data update``) 允许访问网络；回测/模拟盘入口一律
``allow_network=False``（R-004）。批量写入按 ``data.page_size`` 分页（R-013）。
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from ..config import Config
from ..errors import ConfigValidationError
from . import calendar as cal
from .sources import (
    AKShareSource,
    BaostockSource,
    DataRouter,
    FixtureSource,
    PriceSource,
    normalize_symbol,
    retry_call,
)
from .store import Store

log = logging.getLogger("quant.data.update")

__all__ = [
    "build_sources", "classify_board", "fetch_universe", "update_store",
    "data_status", "fetch_daily", "UpdateResult",
]


def build_sources(
    cfg: Config,
    source: str | None = None,
    fixture_dir: Path | str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[PriceSource]:
    """Build the ordered source list. Primary first, then configured fallbacks."""
    primary = (source or cfg.get("data.source", "baostock")).lower()
    common = dict(
        max_retries=cfg.get("data.max_retries", 3),
        retry_base_interval=cfg.get("data.retry_base_interval", 1.0),
        timeout=cfg.get("data.request_timeout", 20),
        sleep=sleep,
    )

    def make(name: str) -> PriceSource:
        if name == "baostock":
            return BaostockSource(**common)
        if name == "akshare":
            return AKShareSource(**common)
        if name == "fixture":
            base = fixture_dir or (cfg.root / "tests" / "fixtures")
            return FixtureSource(base, **common)
        raise ConfigValidationError(f"未知数据源: {name}")

    ordered = [primary]
    for fallback in cfg.get("data.fallback_sources", []) or []:
        if fallback not in ordered and fallback != "fixture":
            ordered.append(fallback)
    if primary == "fixture":
        ordered = ["fixture"]  # 离线模式绝不隐式触发网络
    return [make(name) for name in ordered]


def classify_board(symbol: str, name: str = "") -> str:
    """R-014 board enum. ST overrides the exchange-board classification."""
    text = (name or "").upper().replace(" ", "")
    if "ST" in text:
        return "ST"
    code = normalize_symbol(symbol).split(".")[0]
    if code.startswith("300") or code.startswith("301"):
        return "CYB"
    if code.startswith("688") or code.startswith("689"):
        return "KCB"
    if code.startswith(("8", "4", "9")) and code[0] in "84":
        return "BSE"
    if code.startswith(("60", "000", "001", "002", "003")):
        return "MAIN"
    return "OTHER"


def _hs300_from_akshare(max_retries: int, base_interval: float, sleep: Any) -> pd.DataFrame:
    """Free CSI 300 constituent list, with two endpoint fallbacks."""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 akshare") from exc

    def attempt() -> pd.DataFrame:
        try:
            frame = ak.index_stock_cons_csindex(symbol="000300")
        except Exception:  # noqa: BLE001 - endpoint drift / rate limit
            frame = ak.index_stock_cons(symbol="000300")
        if frame is None or frame.empty:
            raise RuntimeError("沪深300成分为空")
        cols = {str(c): c for c in frame.columns}
        code_col = next((cols[c] for c in cols if "代码" in c or c.lower() in ("code", "symbol")), None)
        name_col = next((cols[c] for c in cols if "名称" in c or "简称" in c or c.lower() == "name"), None)
        if code_col is None:
            raise RuntimeError(f"无法识别成分券代码列: {list(frame.columns)}")
        codes = frame[code_col].astype(str).str.strip().str.zfill(6)
        names = frame[name_col].astype(str) if name_col else pd.Series([""] * len(frame))
        return pd.DataFrame({"symbol": codes, "name": names})

    return retry_call(
        attempt, max_retries=max_retries, base_interval=base_interval,
        source="akshare:hs300", sleep=sleep or _time.sleep,
    )


def fetch_universe(
    cfg: Config,
    pool: str,
    sources: Sequence[PriceSource],
    fixture_dir: Path | str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Return universe frame with the R-014 ``board`` label."""
    warnings: list[str] = []
    fixture = fixture_dir or (cfg.root / "tests" / "fixtures")
    if any(s.name == "fixture" for s in sources):
        path = Path(fixture) / "universe.csv"
        frame = pd.read_csv(path, dtype=str)
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
        if "board" not in frame.columns:
            frame["board"] = frame.apply(
                lambda r: classify_board(r["symbol"], r.get("name", "")), axis=1
            )
    else:
        if pool != "hs300":
            raise ConfigValidationError(f"首版仅支持 hs300 股票池，收到 {pool!r}")
        try:
            frame = _hs300_from_akshare(
                cfg.get("data.max_retries", 3),
                cfg.get("data.retry_base_interval", 1.0),
                sleep,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"沪深300成分获取失败: {exc}；尝试离线 fixture 兜底")
            log.warning("沪深300成分获取失败: %s", exc)
            path = Path(fixture) / "universe.csv"
            if not path.exists():
                raise
            frame = pd.read_csv(path, dtype=str)
            frame["symbol"] = frame["symbol"].map(normalize_symbol)
        frame["board"] = frame.apply(
            lambda r: classify_board(r["symbol"], str(r.get("name", ""))), axis=1
        )
        frame["industry"] = None
        frame["in_date"] = None
        frame["out_date"] = None
        frame["source"] = "akshare:index_cons"
    if "industry" not in frame.columns:
        frame["industry"] = None
    for col in ("in_date", "out_date", "source", "name"):
        if col not in frame.columns:
            frame[col] = None
    return frame, warnings


@dataclass
class UpdateResult:
    symbols: list[str]
    source: str
    audit: dict[str, Any] = field(default_factory=dict)
    calendar: dict[str, Any] = field(default_factory=dict)
    universe_rows: int = 0
    industry_rows: int = 0
    rows_written: int = 0
    warnings: list[str] = field(default_factory=list)


def update_store(
    cfg: Config,
    store: Store,
    *,
    source: str | None = None,
    pool: str | None = None,
    limit: int | None = None,
    start: str | None = None,
    end: str | None = None,
    fixture_dir: Path | str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> UpdateResult:
    source_name = (source or cfg.get("data.source", "baostock")).lower()
    pool_name = pool or cfg.get("data.pool", "hs300")
    begin, finish = str(start or cfg.get("data.start")), str(end or cfg.get("data.end"))
    n_limit = int(cfg.get("data.limit", 30) if limit is None else limit)
    page_size = int(cfg.get("data.page_size", 500))

    sources = build_sources(cfg, source_name, fixture_dir, sleep)
    result = UpdateResult(symbols=[], source=source_name)

    # 1) universe (board-labelled)
    universe, warnings = fetch_universe(cfg, pool_name, sources, fixture_dir, sleep)
    result.warnings.extend(warnings)
    store.upsert_universe(universe, pool=pool_name)
    result.universe_rows = len(universe)

    # 2) industry classification: best-effort, failure -> empty + warning
    wanted = store.get_universe(pool_name, limit=None, main_only=False)["symbol"].tolist()
    router = DataRouter(store, sources, cfg, allow_network=True)
    industry = router.fetch_industry(wanted)
    result.warnings.extend(router.warnings)
    if not industry.empty:
        merged = universe.set_index("symbol").copy()
        ind = industry.drop_duplicates("symbol").set_index("symbol")
        for col in ("name", "industry"):
            if col in ind.columns:
                merged[col] = ind[col].reindex(merged.index).combine_first(merged.get(col, pd.Series(index=merged.index, dtype=object)))
        for col in ("board", "in_date", "out_date", "source"):
            if col not in merged.columns:
                merged[col] = None
        store.upsert_universe(
            merged.reset_index()[["symbol", "name", "industry", "board",
                                  "in_date", "out_date", "source"]],
            pool=pool_name,
        )
        result.industry_rows = len(industry)

    # 3) trading calendar (R-011)
    if source_name == "fixture":
        result.calendar = cal.sync_from_daily_price(store)  # may be empty on first run
    else:
        result.calendar = cal.update_calendar(store, begin, finish, sources)

    # 4) daily bars: first N by universe order, never random (R-003 + limit semantics)
    symbols = store.get_universe(pool_name, limit=n_limit, main_only=True)["symbol"].tolist() if n_limit else \
        store.get_universe(pool_name, main_only=True)["symbol"].tolist()
    if not symbols:
        raise ConfigValidationError(f"股票池 {pool_name} 无 MAIN 标的，无法更新")
    result.symbols = symbols

    router = DataRouter(store, sources, cfg, allow_network=True)
    frame = router.fetch_daily(
        symbols, begin, finish, adjust=cfg.get("data.adjust", "qfq"),
        allow_network=True, persist=True, use_cache=False, page_size=page_size,
    )
    result.rows_written = len(frame)
    result.warnings.extend(router.warnings)

    # 5) offline calendar may need a second sync now that bars exist
    if source_name == "fixture" and not store.trading_days(begin, finish):
        result.calendar = cal.sync_from_daily_price(store)

    # 6) audit: expected_days / actual_days / missing_days
    result.audit = store.missing_days_audit(symbols, begin, finish)
    result.audit["pool"] = pool_name
    result.audit["limit"] = n_limit
    result.audit["start"] = begin
    result.audit["end"] = finish
    result.audit["boards"] = store.board_counts(pool_name)
    result.audit["survivorship_bias_warning"] = (
        "当前免费数据源可能缺失退市股票；使用『沪深300 当前成分』回溯历史存在幸存者偏差"
    )
    return result


def fetch_daily(
    cfg: Config,
    store: Store,
    symbols: Sequence[str],
    start: str,
    end: str,
    adjust: str = "qfq",
    *,
    allow_network: bool = False,
    use_cache: bool = True,
    sources: Sequence[PriceSource] | None = None,
) -> pd.DataFrame:
    """统一数据接口。回测/模拟盘默认 ``allow_network=False``（R-004）。"""
    srcs = list(sources) if sources is not None else build_sources(cfg)
    router = DataRouter(store, srcs, cfg, allow_network=allow_network)
    return router.fetch_daily(
        symbols, start, end, adjust=adjust,
        allow_network=allow_network, use_cache=use_cache,
        page_size=int(cfg.get("data.page_size", 500)),
    )


def data_status(store: Store, pool: str = "hs300") -> dict[str, Any]:
    """Summary for ``python -m quant data status --pool hs300``."""
    universe = store.get_universe(pool, main_only=False)
    boards = store.board_counts(pool)
    by_source = [dict(r) for r in store.conn.execute(
        "SELECT source, COUNT(*) AS rows, MIN(date) AS first_date, MAX(date) AS last_date "
        "FROM daily_price GROUP BY source ORDER BY source"
    ).fetchall()]
    symbols = store.get_universe(pool, main_only=True)["symbol"].tolist()
    audit = store.missing_days_audit(symbols, "1900-01-01", "2999-12-31") if symbols else {}
    return {
        "pool": pool,
        "db_path": str(store.path),
        "schema_version": store.schema_version,
        "universe_rows": len(universe),
        "main_symbols": len(symbols),
        "boards": boards,
        "daily_rows": store.count_daily(),
        "by_source": by_source,
        "calendar_rows": store.calendar_count(),
        "open_days": len(store.trading_days()),
        "latest_trade_date": store.latest_trade_date(),
        "paper_orders": store.count_orders(),
        "paper_trades": store.count_trades(),
        "audit": audit,
    }
