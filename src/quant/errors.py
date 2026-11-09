"""QUANT-001 exception hierarchy.

Exit-code contract (implemented in cli.main):
    0 success / 2 data missing / 3 network failure / 4 validation failure
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "QuantError",
    "DataUnavailableError",
    "NetworkFailureError",
    "ConfigValidationError",
    "SchemaError",
    "DataIntegrityError",
    "RetryExhausted",
]


class QuantError(Exception):
    """Base class for all quant errors."""

    exit_code = 1


class DataUnavailableError(QuantError):
    """Raised when no data source can serve the requested bar(s).

    R-003 / D-001: the message MUST contain the attempted sources, the symbol
    and the requested date range. Never silently return an empty frame / NaN.
    """

    exit_code = 2

    def __init__(
        self,
        source_tried: Iterable[str],
        symbol: str | Sequence[str],
        date_range: Sequence[str] | str,
        detail: str = "",
    ) -> None:
        self.source_tried: list[str] = [str(s) for s in source_tried]
        self.symbol = symbol if isinstance(symbol, str) else ",".join(map(str, symbol))
        if isinstance(date_range, str):
            self.date_range = date_range
        else:
            parts = [str(x) for x in date_range]
            self.date_range = f"{parts[0]}..{parts[-1]}" if len(parts) > 1 else (parts[0] if parts else "")
        self.detail = detail
        super().__init__(self._message())

    def _message(self) -> str:
        extra = f" | detail={self.detail}" if self.detail else ""
        return (
            "DataUnavailableError: 无可用行情数据 "
            f"symbol={self.symbol} date_range={self.date_range} "
            f"source_tried={self.source_tried}{extra}"
        )


class NetworkFailureError(QuantError):
    """All configured network sources failed after retry + degradation."""

    exit_code = 3


class ConfigValidationError(QuantError):
    """config.yaml / env / CLI value failed schema validation."""

    exit_code = 4


class SchemaError(QuantError):
    """SQLite schema / user_version mismatch."""

    exit_code = 4


class DataIntegrityError(QuantError):
    """NaN, negative volume, duplicated bars, missing adj_factor, etc.

    R-005: we raise instead of forward-filling or fabricating data.
    """

    exit_code = 2


class RetryExhausted(QuantError):
    """Internal marker: one source exhausted its exponential-backoff retries."""

    def __init__(self, source: str, attempts: int, last_error: Exception | None = None) -> None:
        self.source = source
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"source={source} 重试 {attempts} 次后仍失败: "
            f"{type(last_error).__name__ if last_error else 'empty result'}"
        )
