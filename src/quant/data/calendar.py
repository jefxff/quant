"""R-011: 交易日历必须落库，T+1 / 停牌顺延 / 样本外切分全部依赖该表。

禁止用 ``pd.bdate_range`` 或任何"工作日猜测"替代。真实运行从 Baostock
``query_trade_dates`` 获取（AKShare 备用）；离线测试可用
``sync_from_daily_price`` 从已落库的真实交易日生成，并在 source 列明确标记
为 ``fixture``，绝不会被误认为网络日历。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import pandas as pd

from ..errors import NetworkFailureError
from .sources import PriceSource

log = logging.getLogger("quant.data.calendar")

__all__ = ["update_calendar", "sync_from_daily_price", "calendar_report"]


def update_calendar(
    store: Any,
    start: str,
    end: str,
    sources: Sequence[PriceSource],
) -> dict[str, Any]:
    """Fetch the calendar from Baostock (then AKShare), never from bdate_range."""
    existing = store.trading_days(start, end)
    if existing:
        return {"rows": len(existing), "source": "cache", "skipped": True}
    errors: list[str] = []
    for source in sources:
        try:
            frame = source.fetch_trade_calendar(start, end)
            store.upsert_calendar(frame)
            n_open = int((frame["is_open"] == 1).sum())
            log.info("交易日历落库 source=%s rows=%d is_open=%d", source.name, len(frame), n_open)
            return {"rows": len(frame), "open_days": n_open, "source": source.name, "skipped": False}
        except Exception as exc:  # noqa: BLE001 - degrade to next source
            errors.append(f"{source.name}: {exc}")
            log.warning("交易日历源 %s 失败: %s", source.name, exc)
    raise NetworkFailureError("交易日历全部数据源失败: " + " | ".join(errors))


def sync_from_daily_price(store: Any, source: str = "fixture") -> dict[str, Any]:
    """Offline-only fallback: derive the calendar from real bars already in DB.

    Used by the offline smoke test / fixtures. It does NOT invent dates, and it
    labels the rows ``source='fixture'`` so the report can disclose the origin.
    """
    dates = store.all_dates()
    if not dates:
        return {"rows": 0, "source": source, "skipped": True}
    frame = pd.DataFrame({"date": dates, "is_open": 1})
    store.upsert_calendar(frame, source=source)
    return {"rows": len(frame), "open_days": len(frame), "source": source, "skipped": False}


def calendar_report(store: Any, start: str, end: str) -> dict[str, Any]:
    days = store.trading_days(start, end)
    rows = [dict(r) for r in store.conn.execute(
        "SELECT is_open, COUNT(*) AS c FROM trade_calendar WHERE date BETWEEN ? AND ? "
        "GROUP BY is_open", (str(start), str(end))
    ).fetchall()]
    breakdown = {int(r["is_open"]): int(r["c"]) for r in rows}
    return {
        "start": str(start),
        "end": str(end),
        "open_days": len(days),
        "closed_days": breakdown.get(0, 0),
        "first": days[0] if days else None,
        "last": days[-1] if days else None,
    }
