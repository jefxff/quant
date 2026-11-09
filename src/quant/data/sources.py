"""Free data-source adapters + the R-003 fallback chain.

=============================================================================
T-001 内部代码格式契约 (conversion contract)
=============================================================================
内部统一格式 (internal)   : ``600000.SH`` / ``000001.SZ`` / ``830001.BJ``
Baostock                  : ``sh.600000`` / ``sz.000001``   (小写交易所前缀)
AKShare / 新浪 / 腾讯公开行情: ``600000`` / ``000001``     (6 位纯数字)
其他可识别输入            : ``sh600000``, ``SH600000``, ``600000.XSHG``

规则：
  * 6 位代码 60/68/9 -> SH, 00/30/2 -> SZ, 4/8 -> BJ（北交所）。
  * 交易所缩写大小写不敏感；未知后缀一律抛 ValueError，禁止猜测后静默继续。
  * ``normalize_symbol`` 是所有入口的唯一转换函数；DB / 回测 / 报告只用内部格式。

数据源分工 (R-002)：
  * Baostock  -> 历史日线主源（匿名、无需 token）
  * AKShare   -> 行业分类 + 备用行情（含字段漂移兼容）
  * 公开行情（新浪/腾讯）由 AKShare 适配器内部封装，不直接暴露给上层。

重试 (R-012)：``retry_call`` 实现 ``base * 2**retry_count`` 指数退避；
单个源耗尽重试后抛 ``RetryExhausted``，由 ``DataRouter`` 降级到下一源，
不直接崩溃。``sleep`` 可注入，离线测试无需真实等待。
"""

from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from ..errors import DataIntegrityError, DataUnavailableError, RetryExhausted

log = logging.getLogger("quant.data.sources")

__all__ = [
    "normalize_symbol",
    "to_baostock",
    "to_plain",
    "retry_call",
    "PriceSource",
    "BaostockSource",
    "AKShareSource",
    "FixtureSource",
    "DataRouter",
    "PRICE_COLUMNS",
]

PRICE_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "adj_factor", "source",
]

_SYMBOL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)
_BAOSTOCK_RE = re.compile(r"^(sh|sz|bj)\.(\d{6})$", re.IGNORECASE)
_TIGHT_RE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
_XSHG_RE = re.compile(r"^(\d{6})\.(XSHG|XSHE|BSE)$", re.IGNORECASE)
_PLAIN_RE = re.compile(r"^(\d{6})$")
_BOARD_BY_SUFFIX = {"XSHG": "SH", "XSHE": "SZ", "BSE": "BJ"}


def _exchange_of(code: str) -> str:
    if code[0] in "69":
        return "SH"
    if code[0] in "023":
        return "SZ"
    if code[0] in "48":
        return "BJ"
    raise ValueError(f"无法从代码 {code!r} 推断交易所（请显式给出 .SH/.SZ/.BJ）")


def normalize_symbol(value: Any) -> str:
    """Convert any supported representation to ``600000.SH`` form."""
    text = str(value).strip()
    m = _SYMBOL_RE.match(text)
    if m:
        return f"{m.group(1)}.{m.group(2).upper()}"
    m = _BAOSTOCK_RE.match(text)
    if m:
        return f"{m.group(2)}.{m.group(1).upper()}"
    m = _TIGHT_RE.match(text)
    if m:
        return f"{m.group(2)}.{m.group(1).upper()}"
    m = _XSHG_RE.match(text)
    if m:
        return f"{m.group(1)}.{_BOARD_BY_SUFFIX[m.group(2).upper()]}"
    m = _PLAIN_RE.match(text)
    if m:
        return f"{m.group(1)}.{_exchange_of(m.group(1))}"
    raise ValueError(f"无法识别的股票代码格式: {value!r}（应为 600000.SH / sh.600000 / 600000）")


def to_baostock(symbol: str) -> str:
    """``600000.SH`` -> ``sh.600000``."""
    code, _, exchange = normalize_symbol(symbol).partition(".")
    return f"{exchange.lower()}.{code}"


def to_plain(symbol: str) -> str:
    """``600000.SH`` -> ``600000`` (AKShare / 新浪 / 腾讯公开行情格式)."""
    return normalize_symbol(symbol).split(".")[0]


# --------------------------------------------------------------------------- #
# R-012 exponential backoff
# --------------------------------------------------------------------------- #
class _EmptyResult(Exception):
    """A source returned an empty/None frame -> treat as a failed attempt."""


def retry_call(
    fn: Callable[[], Any],
    *,
    max_retries: int = 3,
    base_interval: float = 1.0,
    source: str = "unknown",
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Any:
    """Call ``fn`` with exponential backoff.

    Intervals are exactly ``base_interval * 2**retry_count`` for
    ``retry_count = 0 .. max_retries-1`` (i.e. ``max_retries`` sleeps and
    ``max_retries + 1`` attempts). Exhaustion raises ``RetryExhausted`` so the
    caller can degrade to the next source.
    """
    last_error: Exception | None = None
    for retry_count in range(0, max(0, int(max_retries)) + 1):
        try:
            result = fn()
            if result is None:
                raise _EmptyResult("source returned None")
            if isinstance(result, pd.DataFrame) and result.empty:
                raise _EmptyResult("source returned an empty DataFrame")
            return result
        except Exception as exc:  # noqa: BLE001 - any failure is retryable
            last_error = exc
            if retry_count >= int(max_retries):
                break
            interval = float(base_interval) * (2 ** retry_count)
            log.warning(
                "[%s] 第 %d 次调用失败 (%s)，%.3fs 后重试 (base*2^%d)",
                source, retry_count + 1, exc, interval, retry_count,
            )
            if on_retry is not None:
                on_retry(retry_count, interval, exc)
            sleep(interval)
    raise RetryExhausted(source, max(0, int(max_retries)) + 1, last_error)


def _finalize(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    """Validate + normalise a raw adapter frame. Never fills NaN (R-005)."""
    if frame is None or len(frame) == 0:
        raise _EmptyResult("no rows")
    required = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "adj_factor"]
    for col in required:
        if col not in frame.columns:
            raise DataIntegrityError(f"{source} 返回缺少字段 {col}: {list(frame.columns)}")
    out = frame.copy()
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out["date"] = out["date"].astype(str).str.slice(0, 10)
    numeric = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]
    for col in numeric:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    bad = out[numeric].isna().any(axis=1) | (out[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if bool(bad.any()):
        n_bad = int(bad.sum())
        out = out.loc[~bad].copy()
        log.warning("[%s] 丢弃 %d 行不可用行情（无 NaN 填补，R-005）", source, n_bad)
        if out.empty:
            raise _EmptyResult("all rows were unusable")
    if (out["volume"] < 0).any() or (out["amount"] < 0).any():
        raise DataIntegrityError(f"{source} 返回负成交量/成交额")
    if (out["adj_factor"] <= 0).any():
        raise DataIntegrityError(f"{source} 返回非正 adj_factor")
    out["source"] = source
    out = out[PRICE_COLUMNS].drop_duplicates(subset=["symbol", "date"], keep="last")
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def _attach_adj_factor(raw: pd.DataFrame, qfq: pd.DataFrame, source: str) -> pd.DataFrame:
    """adj_factor := qfq_close / raw_close (T-002). Derived, never fabricated.

    Both frames come from the same free source with different adjust flags.
    Only the *ratio* is used for signal computation, so the absolute anchoring
    of the source's qfq series is irrelevant.
    """
    keys = ["symbol", "date"]
    left = raw[keys + ["open", "high", "low", "close", "volume", "amount"]].copy()
    right = qfq[keys + ["close"]].rename(columns={"close": "qfq_close"})
    merged = left.merge(right, on=keys, how="inner", validate="one_to_one")
    if merged.empty:
        raise _EmptyResult("qfq/bfq 无法对齐")
    if (merged["qfq_close"] <= 0).any() or (merged["close"] <= 0).any():
        raise DataIntegrityError(f"{source} 复权/不复权收盘价存在非正值")
    merged["adj_factor"] = merged["qfq_close"] / merged["close"]
    return merged


class PriceSource:
    """Adapter interface. ``sleep`` / ``fetcher`` are injectable for offline tests."""

    name = "base"

    def __init__(
        self,
        max_retries: int = 3,
        retry_base_interval: float = 1.0,
        timeout: int = 20,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.max_retries = int(max_retries)
        self.retry_base_interval = float(retry_base_interval)
        self.timeout = int(timeout)
        self._sleep = sleep if sleep is not None else time.sleep
        self.warnings: list[str] = []

    def _retry(self, fn: Callable[[], Any]) -> Any:
        return retry_call(
            fn,
            max_retries=self.max_retries,
            base_interval=self.retry_base_interval,
            source=self.name,
            sleep=self._sleep,
        )

    def fetch_daily(self, symbols: Sequence[str], start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        raise NotImplementedError

    def fetch_industry(self, symbols: Sequence[str]) -> pd.DataFrame:  # pragma: no cover - default
        self.warnings.append(f"{self.name} 不提供行业分类")
        return pd.DataFrame(columns=["symbol", "name", "industry", "source"])

    def fetch_trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        raise NotImplementedError


class BaostockSource(PriceSource):
    """R-002 历史日线主源。匿名 login，无需 token，无付费接口。"""

    name = "baostock"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bs: Any = None

    def _client(self) -> Any:
        if self._bs is None:
            try:
                import baostock as bs  # lazy: offline tests never need it
            except ImportError as exc:  # pragma: no cover
                raise ImportError("缺少 baostock，请执行 pip install -e '.[dev]'") from exc
            self._bs = bs
        return self._bs

    def _login(self) -> None:
        bs = self._client()
        lg = bs.login()
        if getattr(lg, "error_code", "0") != "0":
            raise RuntimeError(f"baostock 登录失败: {getattr(lg, 'error_msg', lg)}")

    @staticmethod
    def _rows(result: Any) -> list[list[str]]:
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(f"baostock query 失败: {getattr(result, 'error_msg', result)}")
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        return rows

    def _query(self, symbol: str, start: str, end: str, adjustflag: str) -> pd.DataFrame:
        bs = self._client()
        fields = "date,code,open,high,low,close,volume,amount"
        res = bs.query_history_k_data_plus(
            to_baostock(symbol), fields,
            start_date=start, end_date=end, frequency="d", adjustflag=adjustflag,
        )
        rows = self._rows(res)
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=fields.split(","))
        frame = frame.rename(columns={"code": "symbol"})
        frame["symbol"] = symbol
        for col in ("open", "high", "low", "close", "volume", "amount"):
            frame[col] = frame[col].replace("", np.nan)
        return frame

    def fetch_daily(self, symbols: Sequence[str], start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        if adjust != "qfq":
            raise ValueError("BaostockSource 仅支持 adjust='qfq'（存储仍为不复权价）")

        def _work() -> pd.DataFrame:
            self._login()
            try:
                pieces = []
                for symbol in symbols:
                    internal = normalize_symbol(symbol)
                    raw = self._query(internal, start, end, "3")   # 3 = 不复权
                    qfq = self._query(internal, start, end, "2")   # 2 = 前复权
                    if raw.empty or qfq.empty:
                        continue
                    pieces.append(_attach_adj_factor(raw, qfq, self.name))
                if not pieces:
                    raise _EmptyResult("baostock 无数据")
                return _finalize(pd.concat(pieces, ignore_index=True), self.name)
            finally:
                try:
                    self._client().logout()
                except Exception:  # pragma: no cover
                    pass

        return self._retry(_work)

    def fetch_trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        def _work() -> pd.DataFrame:
            self._login()
            try:
                bs = self._client()
                res = bs.query_trade_dates(start_date=start, end_date=end)
                rows = self._rows(res)
                frame = pd.DataFrame(rows, columns=["date", "is_open"])
                if frame.empty:
                    raise _EmptyResult("baostock 交易日历为空")
                frame["is_open"] = pd.to_numeric(frame["is_open"], errors="raise").astype(int)
                frame["source"] = self.name
                return frame[["date", "is_open", "source"]]
            finally:
                self._client().logout()

        return self._retry(_work)


# AKShare 字段漂移兼容表：键为内部字段，值为可能的中文/英文列名（按优先级）
_AK_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("日期", "date", "trade_date", "交易日期"),
    "open": ("开盘", "开盘价", "open"),
    "high": ("最高", "最高价", "high"),
    "low": ("最低", "最低价", "low"),
    "close": ("收盘", "收盘价", "close"),
    "volume": ("成交量", "volume", "vol"),
    "amount": ("成交额", "amount", "turnover"),
}


def _ak_map_columns(frame: pd.DataFrame) -> pd.DataFrame:
    rename: dict[str, str] = {}
    for internal, aliases in _AK_ALIASES.items():
        for alias in aliases:
            if alias in frame.columns:
                rename[alias] = internal
                break
    missing = [k for k in _AK_ALIASES if k not in rename.values()]
    if missing:
        raise DataIntegrityError(
            f"AKShare 字段漂移，无法识别 {missing}；实际字段={list(frame.columns)}"
        )
    return frame.rename(columns=rename)[list(_AK_ALIASES)].copy()


class AKShareSource(PriceSource):
    """R-002 备用行情 + 行业分类。内部封装新浪/腾讯/东财公开行情。"""

    name = "akshare"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ak: Any = None

    def _client(self) -> Any:
        if self._ak is None:
            try:
                import akshare as ak  # lazy: heavy import, offline tests skip it
            except ImportError as exc:  # pragma: no cover
                raise ImportError("缺少 akshare，请执行 pip install -e '.[dev]'") from exc
            self._ak = ak
        return self._ak

    def _hist(self, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
        ak = self._client()
        frame = ak.stock_zh_a_hist(
            symbol=to_plain(symbol),
            period="daily",
            start_date=str(start).replace("-", ""),
            end_date=str(end).replace("-", ""),
            adjust=adjust,
        )
        if frame is None or frame.empty:
            return pd.DataFrame()
        out = _ak_map_columns(frame)
        out["symbol"] = normalize_symbol(symbol)
        out["date"] = pd.to_datetime(out["date"], errors="raise").dt.strftime("%Y-%m-%d")
        return out

    def fetch_daily(self, symbols: Sequence[str], start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        if adjust != "qfq":
            raise ValueError("AKShareSource 仅支持 adjust='qfq'")

        def _work() -> pd.DataFrame:
            pieces = []
            for symbol in symbols:
                internal = normalize_symbol(symbol)
                raw = self._hist(internal, start, end, "")
                qfq = self._hist(internal, start, end, "qfq")
                if raw.empty or qfq.empty:
                    continue
                pieces.append(_attach_adj_factor(raw, qfq, self.name))
            if not pieces:
                raise _EmptyResult("akshare 无数据")
            return _finalize(pd.concat(pieces, ignore_index=True), self.name)

        return self._retry(_work)

    def fetch_industry(self, symbols: Sequence[str]) -> pd.DataFrame:
        """Best-effort: any failure degrades to an EMPTY frame + warning."""
        try:
            def _work() -> pd.DataFrame:
                ak = self._client()
                rows = []
                for symbol in symbols:
                    info = ak.stock_individual_info_em(symbol=to_plain(symbol))
                    if info is None or info.empty:
                        continue
                    mapping = dict(zip(info.iloc[:, 0].astype(str), info.iloc[:, 1].astype(str)))
                    rows.append({
                        "symbol": normalize_symbol(symbol),
                        "name": mapping.get("股票简称", ""),
                        "industry": mapping.get("行业", ""),
                        "source": self.name,
                    })
                if not rows:
                    raise _EmptyResult("akshare 行业为空")
                return pd.DataFrame(rows)

            return self._retry(_work)
        except Exception as exc:  # noqa: BLE001 - D-001 允许行业分类降级为空
            self.warnings.append(f"AKShare 行业分类获取失败，降级为空: {exc}")
            log.warning("fetch_industry 降级为空: %s", exc)
            return pd.DataFrame(columns=["symbol", "name", "industry", "source"])

    def fetch_trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        def _work() -> pd.DataFrame:
            ak = self._client()
            frame = ak.tool_trade_date_hist_sina()
            if frame is None or frame.empty:
                raise _EmptyResult("akshare 交易日历为空")
            col = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
            dates = pd.to_datetime(frame[col], errors="raise").dt.strftime("%Y-%m-%d")
            mask = (dates >= str(start)) & (dates <= str(end))
            out = pd.DataFrame({"date": dates[mask].values, "is_open": 1, "source": self.name})
            if out.empty:
                raise _EmptyResult("区间内无交易日")
            return out

        return self._retry(_work)


class FixtureSource(PriceSource):
    """Offline-only adapter backed by checked-in test fixtures.

    It is NEVER used implicitly: only ``--source fixture`` or offline tests
    construct it. Rows keep ``source='fixture'`` in the DB so real data can
    never be confused with synthetic test data (R-005).
    """

    name = "fixture"

    def __init__(self, fixture_dir: Path | str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fixture_dir = Path(fixture_dir)

    def _prices(self) -> pd.DataFrame:
        path = self.fixture_dir / "daily_prices.csv"
        if not path.exists():
            raise FileNotFoundError(f"离线 fixture 不存在: {path}")
        return pd.read_csv(path, dtype={"symbol": str, "date": str})

    def fetch_daily(self, symbols: Sequence[str], start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        wanted = {normalize_symbol(s) for s in symbols}
        frame = self._prices()
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
        mask = (
            frame["symbol"].isin(wanted)
            & (frame["date"] >= str(start))
            & (frame["date"] <= str(end))
        )
        return self._retry(lambda: _finalize(frame.loc[mask].copy(), self.name))

    def fetch_industry(self, symbols: Sequence[str]) -> pd.DataFrame:
        path = self.fixture_dir / "universe.csv"
        if not path.exists():
            return pd.DataFrame(columns=["symbol", "name", "industry", "source"])
        frame = pd.read_csv(path, dtype=str)
        frame["symbol"] = frame["symbol"].map(normalize_symbol)
        frame["source"] = self.name
        return frame[["symbol", "name", "industry", "source"]]

    def fetch_trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        path = self.fixture_dir / "trade_calendar.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path, dtype={"date": str})
        mask = (frame["date"] >= str(start)) & (frame["date"] <= str(end))
        frame = frame.loc[mask, ["date", "is_open"]].copy()
        frame["source"] = self.name
        return frame.reset_index(drop=True)


class DataRouter:
    """R-003: 本地缓存 -> Baostock -> AKShare -> DataUnavailableError.

    ``allow_network=False`` is used unconditionally by the backtest and paper
    engines (R-004): they can only ever see local SQLite rows.
    """

    def __init__(
        self,
        store: Any,
        sources: Sequence[PriceSource],
        cfg: Any = None,
        *,
        allow_network: bool = True,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.sources = list(sources)
        self.cfg = cfg
        self.allow_network = allow_network
        self._sleep = sleep
        self.warnings: list[str] = []
        self.source_tried: list[str] = []

    def _expected_dates(self, start: str, end: str) -> list[str]:
        if self.store is None:
            return []
        return self.store.trading_days(start, end)

    def fetch_daily(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        adjust: str = "qfq",
        *,
        allow_network: bool | None = None,
        persist: bool = True,
        use_cache: bool = True,
        page_size: int = 500,
    ) -> pd.DataFrame:
        use_network = self.allow_network if allow_network is None else allow_network
        expected = self._expected_dates(start, end)
        frames: list[pd.DataFrame] = []

        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            tried: list[str] = []
            cached = (
                self.store.read_daily([symbol], start, end, page_size)
                if (use_cache and self.store is not None) else pd.DataFrame()
            )
            if use_cache:
                tried.append("local_cache")
            covered = self._cache_covers(cached, symbol, expected)
            if covered:
                frames.append(cached)
                continue

            if not use_network:
                raise DataUnavailableError(
                    tried, symbol, (start, end),
                    detail=f"本地缓存仅 {len(cached)} 条，期望 {len(expected)} 个交易日；"
                           "R-004 禁止回测/模拟盘调用网络，且 R-005 禁止填补缺失行情",
                )

            result: pd.DataFrame | None = None
            for source in self.sources:
                tried.append(source.name)
                try:
                    result = source.fetch_daily([symbol], start, end, adjust=adjust)
                    break
                except Exception as exc:  # noqa: BLE001 - degrade, do not crash (R-012)
                    log.warning("数据源 %s 失败，降级: %s", source.name, exc)
                    self.warnings.append(f"{source.name} 失败: {exc}")
            if result is None or result.empty:
                raise DataUnavailableError(
                    tried, symbol, (start, end),
                    detail="所有已配置数据源重试/降级后仍无可用行情",
                )
            if persist and self.store is not None:
                self.store.upsert_daily(result)
            frames.append(result)

        self.source_tried = list(dict.fromkeys(self.source_tried + ["local_cache"] + [s.name for s in self.sources]))
        if not frames:
            raise DataUnavailableError(["local_cache"], symbols, (start, end))
        return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)

    @staticmethod
    def _cache_covers(cached: pd.DataFrame, symbol: str, expected: list[str]) -> bool:
        if cached is None or cached.empty:
            return False
        got = set(cached["date"].astype(str))
        if expected:
            return set(expected).issubset(got)
        return len(cached[cached["symbol"] == symbol]) > 0

    def fetch_industry(self, symbols: Sequence[str]) -> pd.DataFrame:
        for source in self.sources:
            try:
                frame = source.fetch_industry(symbols)
                if frame is not None and not frame.empty:
                    return frame
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"{source.name} 行业分类失败: {exc}")
        self.warnings.append("行业分类全部失败，降级为空（不参与首版风控）")
        log.warning("行业分类降级为空")
        return pd.DataFrame(columns=["symbol", "name", "industry", "source"])
