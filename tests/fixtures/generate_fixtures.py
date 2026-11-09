"""生成**离线测试专用**的确定性 fixture（不联网、不含 NaN、不填补）。

这些 CSV 是明确标注的合成测试数据，只在 ``--source fixture`` / pytest 中使用，
写入 DB 时 ``source='fixture'``，绝不与真实行情混淆（R-005）。
运行：``python tests/fixtures/generate_fixtures.py``
"""

from __future__ import annotations

import csv
import math
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
START = date(2022, 1, 4)
N_WEEKDAYS = 259          # 2022-01-04 .. 2022-12-30（与 conftest.END 对齐）

UNIVERSE = [
    # symbol,      name,        industry, board, in_date, out_date, source
    ("000001.SZ", "平安银行", "银行", "MAIN", "2022-01-04", "", "fixture"),
    ("000002.SZ", "万科A", "房地产", "MAIN", "2022-01-04", "", "fixture"),
    ("000004.SZ", "ST测试", "综合", "ST", "2022-01-04", "", "fixture"),
    ("000651.SZ", "格力电器", "家用电器", "MAIN", "2022-01-04", "", "fixture"),
    ("300750.SZ", "宁德时代", "电池", "CYB", "2022-01-04", "", "fixture"),
    ("600000.SH", "浦发银行", "银行", "MAIN", "2022-01-04", "", "fixture"),
    ("600030.SH", "中信证券", "证券", "MAIN", "2022-01-04", "", "fixture"),
    ("600036.SH", "招商银行", "银行", "MAIN", "2022-01-04", "", "fixture"),
    ("600519.SH", "贵州茅台", "白酒", "MAIN", "2022-01-04", "", "fixture"),
    ("601318.SH", "中国平安", "保险", "MAIN", "2022-01-04", "", "fixture"),
    ("688981.SH", "中芯国际", "半导体", "KCB", "2022-01-04", "", "fixture"),
    ("830001.BJ", "北交测试", "机械", "BSE", "2022-01-04", "", "fixture"),
]

ADJ_FACTORS = {
    "000001.SZ": 1.0, "000002.SZ": 1.0, "000004.SZ": 1.0, "000651.SZ": 1.0,
    "300750.SZ": 1.0, "600000.SH": 1.5, "600030.SH": 1.0, "600036.SH": 1.0,
    "600519.SH": 1.2, "601318.SH": 1.0, "688981.SH": 1.0, "830001.BJ": 1.0,
}


def weekdays(n: int) -> list[date]:
    out: list[date] = []
    cur = START
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def main() -> None:
    dates = weekdays(N_WEEKDAYS)
    # 交易日历：工作日 is_open=1，另外显式写入若干周末 is_open=0
    with (HERE / "trade_calendar.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "is_open"])
        for d in dates:
            w.writerow([d.isoformat(), 1])
        cur = START
        closed = 0
        while closed < 20:
            if cur.weekday() >= 5:
                w.writerow([cur.isoformat(), 0])
                closed += 1
            cur += timedelta(days=1)

    with (HERE / "universe.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "name", "industry", "board", "in_date", "out_date", "source"])
        for row in UNIVERSE:
            w.writerow(row)

    with (HERE / "daily_prices.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "date", "open", "high", "low", "close",
                    "volume", "amount", "adj_factor", "source"])
        for idx, (symbol, *_rest) in enumerate(UNIVERSE):
            phase = idx * 0.7
            base = 8.0 + idx * 2.5
            factor = ADJ_FACTORS[symbol]
            prev_close = base
            bars: list[list[object]] = []
            for t, d in enumerate(dates):
                trend = 1.0 + 0.00075 * t
                cyc = 1.0 + 0.055 * math.sin(t / 13.0 + phase) + 0.018 * math.sin(t / 3.3 + phase)
                close = round(base * trend * cyc, 2)
                open_px = round(prev_close * (1.0 + 0.0035 * math.sin(t / 5.0 + phase)), 2)
                high = round(max(open_px, close) * 1.012, 2)
                low = round(min(open_px, close) * 0.988, 2)
                volume = 1_000_000 + (t * 7919 + idx * 104729) % 500_000
                amount = round(volume * close, 2)
                iso = d.isoformat()
                # T-010 停牌样本：600000.SH 第 100 个交易日 volume=0
                if symbol == "600000.SH" and t == 100:
                    open_px = high = low = close = prev_close
                    volume = 0
                    amount = 0.0
                # T-009 涨停样本：000001.SZ 第 120 个交易日开盘涨停
                if symbol == "000001.SZ" and t == 120:
                    open_px = round(prev_close * 1.10, 2)
                    close = open_px
                    high = open_px
                    low = round(prev_close * 1.02, 2)
                bars.append([symbol, iso, open_px, high, low, close,
                             volume, amount, factor, "fixture"])
                prev_close = close
            w.writerows(bars)
    print(f"fixtures written: {len(UNIVERSE)} symbols x {len(dates)} trading days -> {HERE}")


if __name__ == "__main__":
    main()
