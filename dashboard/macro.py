# -*- coding: utf-8 -*-
"""宏观政策与外围市场传导层 —— 真实数据，不改写回测绩效。

免费数据源
  - 美股：AKShare ``index_us_stock_sina``（新浪，标普500 / 纳斯达克）
  - 日本 / 韩国：AKShare ``index_global_hist_sina``（新浪环球市场，日经225 / 首尔综合）
  - A 股基准：build.py 已缓存的新浪沪深300真实指数
  - 政策事件：人工整理自公开权威报道，逐条带来源与 URL（非行情数据）

口径与边界
  - 对齐 A 股真实交易日历；任何「外围冲击」只使用**严格早于**该 A 股交易日的
    已收盘外围数据，避免引入当日未来信息。
  - 异常值（例如 KOSPI 最新收盘价为 0 的占位记录）直接剔除并如实记录，
    不填补、不插值、不伪造。
  - 政策 / 外围数据**不进入回测信号**，不改动任何历史绩效；只用于观察、
    传导统计，以及 5 日统计情景推演的「外围承压」条件分组抽样。
"""
from __future__ import annotations

import bisect
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 外围指数定义

SERIES = [
    {"key": "spx", "name": "标普500", "region": "美国", "api": "us_sina",
     "symbol": ".INX", "source": "新浪财经 美股指数日线"},
    {"key": "ndx", "name": "纳斯达克", "region": "美国", "api": "us_sina",
     "symbol": ".IXIC", "source": "新浪财经 美股指数日线"},
    {"key": "nky", "name": "日经225", "region": "日本", "api": "global_sina",
     "symbol": "日经225指数", "source": "新浪财经 环球市场日线"},
    {"key": "ks11", "name": "韩国KOSPI", "region": "韩国", "api": "global_sina",
     "symbol": "首尔综合指数", "source": "新浪财经 环球市场日线"},
]

# ---------------------------------------------------------------- 真实政策 / 事件
# 只收录日期可核实、有公开来源的条目；描述为报道要点，页面标注「非行情数据」。
POLICY_EVENTS = [
    {
        "date": "2026-11-02", "tag": "流动性", "tone": "support",
        "title": "三部门部署用好用足两项结构性货币政策工具",
        "detail": "央行会同金融监管总局、证监会召开座谈会；互换便利余额 1050 亿元，"
                  "回购增持贷款签约约 4400 亿元、累计发放超 2800 亿元。",
        "source": "经济日报",
        "url": "https://www.jingjiribao.cn/static/detail.jsp?id=694784",
    },
    {
        "date": "2026-11-02", "tag": "制度", "tone": "support",
        "title": "证监会答记者问：稳市场稳预期四项安排",
        "detail": "护盘式回购拟新增「破发」触发、取消上市满 6 个月与集合竞价申报限制；"
                  "引导 18 只主动 ETF 加快建仓；两融逆周期调节适度释放融资空间。",
        "source": "证券时报",
        "url": "https://finance.sina.com.cn/jjxw/2026-11-03/doc-inixkrkm0205008.shtml",
    },
    {
        "date": "2026-11-03", "tag": "资金", "tone": "support",
        "title": "第三次互换便利启动，31 家机构申请超 1000 亿元",
        "detail": "参与机构数量与单批规模均超过前两轮，为 A 股提供增量流动性；"
                  "同步提高回购增持专项再贷款覆盖率。",
        "source": "证券时报",
        "url": "https://finance.sina.com.cn/jjxw/2026-11-03/doc-inixkrkm0205008.shtml",
    },
    {
        "date": "2026-11-04", "tag": "利率", "tone": "support",
        "title": "股票回购增持再贷款利率降至 1%，转为长期制度",
        "detail": "由 1.25% 下调 0.25 个百分点，政策由阶段性安排升级为长期制度性安排，"
                  "降低上市公司回购增持资金成本。",
        "source": "中国证券报",
        "url": "https://jnzstatic.cs.com.cn/zzb/htmlInfo/137387.html",
    },
]

GLOBAL_EVENTS = [
    {
        "date": "2026-11-05", "tag": "美股", "tone": "risk",
        "title": "AI 硬件链集中抛售，科技股重挫",
        "detail": "纳指与纳指100创 2025 年 4 月以来最大单日跌幅，费城半导体指数大跌；"
                  "市场担忧 AI 基础设施审查收紧。行情数字以本页真实日线为准。",
        "source": "央广网 / 参考消息",
        "url": "https://www.163.com/dy/article/LB7O4OPQ0514BQ68.html",
    },
    {
        "date": "2026-11-06", "tag": "日韩", "tone": "risk",
        "title": "抛售传导亚太，KOSPI 触发熔断",
        "detail": "韩国 KOSPI 盘中跌超 8% 触发熔断，日经225 盘中跌破 6 万点；"
                  "半导体权重股领跌，韩国财政部称不排除美股外溢效应。",
        "source": "财联社 / 日本经济新闻",
        "url": "https://www.163.com/dy/article/LB1T9N7V05198CJN.html",
    },
    {
        "date": "2026-11-09", "tag": "分化", "tone": "neutral",
        "title": "亚太分化：日股反弹、韩股续跌",
        "detail": "日经225 与 TOPIX 反弹，KOSPI 小幅收跌；美股上一交易日科技股回升，"
                  "市场继续评估 AI 投资可持续性。",
        "source": "CNBC / Mettis Global",
        "url": "https://mettisglobal.news/Asian-markets-retreat-on-tech-selloff-concerns-Japan-rallies-64865",
    },
]

SHOCK_THRESHOLD = -0.02      # 「外围承压」：已知外围冲击 ≤ -2%
COND_PATHS = 800
COND_WINDOW = 250
COND_MIN_SAMPLES = 8
COND_SEED = 20261109
COND_DAYS = 5

# ---------------------------------------------------------------- 抓取与缓存


def _clean_rows(df, start: str) -> tuple[list[list], list[str]]:
    """把任意来源的日线整理成 [[date, close], ...]，剔除 0 / NaN / 重复。"""
    d = df.copy()
    d = d[["date", "close"]]
    d["date"] = d["date"].astype(str).str.slice(0, 10)
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    bad_dates = [str(x) for x in d.loc[d["close"].isna() | (d["close"] <= 0), "date"].tolist()]
    d = d[d["close"].notna() & (d["close"] > 0)]
    d = d.drop_duplicates(subset="date", keep="last").sort_values("date")
    # 多留 40 天用于计算 start 之前的「前一收盘」，页面上再截到 start
    cutoff = (np.datetime64(start) - np.timedelta64(40, "D")).astype(str)
    d = d[d["date"] >= cutoff]
    rows = [[str(r[0]), round(float(r[1]), 4)] for r in d[["date", "close"]].values.tolist()]
    quality = []
    if bad_dates:
        shown = sorted({x for x in bad_dates if x >= start})[-4:]
        quality.append(f"剔除 {len(bad_dates)} 条 close≤0/缺失记录" +
                       (f"（含 {', '.join(shown)}，源未更新）" if shown else ""))
    return rows, quality


def _call(spec: dict):
    import akshare as ak
    if spec["api"] == "us_sina":
        return ak.index_us_stock_sina(symbol=spec["symbol"])
    return ak.index_global_hist_sina(symbol=spec["symbol"])


def fetch_global_series(cache_dir: Path, offline: bool, start: str,
                        on_event=None) -> dict:
    """逐个抓取 / 读缓存，返回原始日线 + 数据来源与质量记录。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    for spec in SERIES:
        path = cache_dir / f"{spec['key']}.json"
        rows, source, quality, error = None, "", [], ""
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
            source = "cache:" + spec["api"]
        elif offline:
            error = "离线模式缺少缓存，该市场不可用"
        else:
            for attempt in range(3):
                try:
                    rows, quality = _clean_rows(_call(spec), start)
                    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                    source = "akshare:" + spec["api"]
                    break
                except Exception as exc:  # 免费源不稳定，指数退避后如实记录失败
                    if attempt == 2:
                        error = f"{type(exc).__name__}: {str(exc)[:80]}"
                    else:
                        time.sleep(1.0 * (2 ** attempt))
            if on_event and not error:
                on_event(f"外围市场采集：{spec['name']} 取得 {len(rows or [])} 条真实日线（{spec['source']}）")
            if on_event and error:
                on_event(f"外围市场采集失败：{spec['name']} {error}，页面标注为不可用（不编造数据）")
        out[spec["key"]] = {
            "key": spec["key"], "name": spec["name"], "region": spec["region"],
            "source": source or "unavailable", "source_note": spec["source"],
            "rows": rows or [], "quality": quality, "error": error,
        }
    return out


# ---------------------------------------------------------------- 对齐 / 统计


def _align(rows: list[list], dates: list[str]):
    """返回 (le_vals, le_dates, shock)。

    le_vals[i]  : 截至 A 股第 i 日「已发布」的最后收盘（≤ dates[i]，用于走势图）
    shock[i]    : 严格早于 dates[i] 的最近两个已收盘价之间的收益（隔夜/领先冲击）
    """
    ds = [r[0] for r in rows]
    cs = [float(r[1]) for r in rows]
    le_vals, le_dates, shock = [], [], []
    for d in dates:
        j = bisect.bisect_right(ds, d) - 1
        le_vals.append(cs[j] if j >= 0 else None)
        le_dates.append(ds[j] if j >= 0 else None)
        k = bisect.bisect_left(ds, d) - 1      # 严格早于 d
        if k >= 1 and cs[k - 1] > 0:
            shock.append(cs[k] / cs[k - 1] - 1.0)
        else:
            shock.append(None)
    return le_vals, le_dates, shock


def _pearson(x, y) -> float | None:
    if len(x) < 20 or float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _beta(x, y) -> float | None:
    if len(x) < 20 or float(np.var(x)) < 1e-12:
        return None
    return float(np.cov(x, y)[0, 1] / np.var(x))


def _stat_block(shocks, a_ret, upto: int, window: int = 60) -> dict:
    """截至 upto（不含未来）的传导统计。"""
    pairs = [(shocks[i], a_ret[i]) for i in range(1, upto + 1)
             if shocks[i] is not None and a_ret[i] is not None]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    recent = pairs[-window:]
    down = [y for x, y in pairs if x <= SHOCK_THRESHOLD]
    flat = [y for x, y in pairs if abs(x) < 0.01]
    return {
        "corr_full": _pearson(xs, ys),
        "corr_60": _pearson([p[0] for p in recent], [p[1] for p in recent]),
        "beta": _beta(xs, ys),
        "down_mean": float(np.mean(down)) if down else None,
        "down_win": float(np.mean([1.0 if v > 0 else 0.0 for v in down])) if down else None,
        "down_n": len(down),
        "flat_mean": float(np.mean(flat)) if flat else None,
        "flat_n": len(flat),
        "samples": len(pairs),
    }


def _window_returns(dates: list[str], values: list[float], event_date: str,
                    horizons=(1, 3, 5)) -> dict:
    """事件日之后 h 个 A 股交易日的真实累计涨跌；数据未到返回 None。"""
    j = bisect.bisect_left(dates, event_date)
    out = {}
    for h in horizons:
        k = j + h
        out[f"t{h}"] = (values[k] / values[j] - 1.0
                        if 0 <= j < len(values) and k < len(values) and values[j] > 0
                        else None)
    return out


def build_conditional_forecast(nav, composite, days: int = COND_DAYS) -> dict:
    """「外围承压」条件 5 日情景：只在历史外围冲击 ≤ -2% 的起点抽样。

    与主推演一样是经验分布外推，严格只用检查点之前的数据；样本不足返回 None，
    绝不合成行情。
    """
    sr = np.diff(nav) / np.where(nav[:-1] > 0, nav[:-1], 1.0)
    n = len(nav)
    data: list = []
    for i in range(n):
        lo = max(0, i - COND_WINDOW)
        pool = [k for k in range(lo, i - days + 1)
                if k >= 0 and k + 1 < len(composite)
                and composite[k + 1] is not None
                and composite[k + 1] <= SHOCK_THRESHOLD]
        if len(pool) < COND_MIN_SAMPLES:
            data.append(None)
            continue
        rng = np.random.default_rng(COND_SEED + i)
        starts = rng.choice(pool, size=COND_PATHS, replace=True)
        idx = starts[:, None] + np.arange(days)[None, :]
        cs = np.cumprod(1.0 + sr[idx], axis=1)
        q = np.quantile(cs, [0.10, 0.50, 0.90], axis=0)
        data.append([
            [[round(float(q[0, k]), 5), round(float(q[1, k]), 5),
              round(float(q[2, k]), 5)] for k in range(days)],
            round(float((cs[:, -1] < 1.0).mean()), 3),
            len(pool),
        ])
    return {
        "threshold": SHOCK_THRESHOLD, "paths": COND_PATHS,
        "window": COND_WINDOW, "seed": COND_SEED, "min_samples": COND_MIN_SAMPLES,
        "data": data,
        "method": "仅从「已知外围冲击 ≤ -2%」的历史交易日起点抽取 5 日连续收益块，"
                  "其余规则与无条件推演一致；样本不足 8 个起点时不推演。",
        "disclaimer": "条件情景是历史分组统计，不是对政策或外围走势的预测；"
                      "不进入回测、不生成订单、不构成投资建议。",
    }


def build_macro(series: dict, dates: list[str], index_close: dict,
                equity: list[list]) -> dict:
    """组装页面所需的宏观 / 外围 / 传导 / 条件情景数据。"""
    start, end = dates[0], dates[-1]
    bench = [float(index_close.get(d, 0.0)) for d in dates]
    nav = [float(r[0]) for r in equity]
    a_ret = [None] + [bench[i] / bench[i - 1] - 1.0
                      if bench[i] > 0 and bench[i - 1] > 0 else None
                      for i in range(1, len(bench))]

    aligned, shocks, latest, stats, curves = {}, {}, [], {}, {}
    for key, item in series.items():
        rows = item["rows"]
        le, le_dates, sh = _align(rows, dates)
        aligned[key], shocks[key] = le, sh
        stats[key] = _stat_block(sh, a_ret, len(dates) - 1)
        # 最新有效收盘与日涨跌
        if rows:
            d0, c0 = rows[-1]
            c1 = rows[-2][1] if len(rows) > 1 else None
            pct = (c0 / c1 - 1.0) if c1 else None
            stale = sum(1 for d in dates if d > d0)
        else:
            d0, c0, pct, stale = None, None, None, len(dates)
        latest.append({
            "key": key, "name": item["name"], "region": item["region"],
            "date": d0, "close": c0, "pct": pct, "stale_days": stale,
            "source": item["source"], "error": item["error"],
            "quality": item["quality"],
        })
        # 归一化走势：以区间首个有效值为 1，缺失点保持 None（不填补）
        base = next((v for v in le if v is not None and v > 0), None)
        curves[key] = [round(v / base, 5) if (v is not None and base) else None for v in le]
        aligned[key + "_dates"] = le_dates

    b0 = next((v for v in bench if v > 0), None)
    curves["hs300"] = [round(v / b0, 5) if (v > 0 and b0) else None for v in bench]

    # 综合冲击：四个市场已知冲击的等权均值（至少 2 个有效）
    composite: list[float | None] = []
    for i in range(len(dates)):
        vals = [shocks[k][i] for k in series if shocks[k][i] is not None]
        composite.append(float(np.mean(vals)) if len(vals) >= 2 else None)

    # 滚动 60 日相关（综合冲击 vs A 股当日收益），体现相关性随时间变化
    roll: list[float | None] = []
    for i in range(len(dates)):
        win = [(composite[j], a_ret[j]) for j in range(max(1, i - 59), i + 1)
               if composite[j] is not None and a_ret[j] is not None]
        roll.append(_pearson([p[0] for p in win], [p[1] for p in win])
                    if len(win) >= 20 else None)

    events = []
    for ev in POLICY_EVENTS + GLOBAL_EVENTS:
        row = dict(ev)
        row["bench"] = _window_returns(dates, bench, ev["date"])
        row["nav"] = _window_returns(dates, nav, ev["date"])
        idx = bisect.bisect_left(dates, ev["date"])
        row["trade_date"] = dates[idx] if idx < len(dates) else None
        row["days_left"] = max(0, len(dates) - 1 - idx) if idx < len(dates) else 0
        events.append(row)
    events.sort(key=lambda e: (e["date"], e["tag"]))

    cond = build_conditional_forecast(np.asarray(nav, dtype=float), composite)
    available = [x for x in latest if x["date"] is not None]
    return {
        "start": start, "end": end,
        "series": [{"key": s["key"], "name": s["name"], "region": s["region"]}
                   for s in SERIES],
        "curves": curves,
        "latest": latest,
        "stats": stats,
        "roll_corr": [round(v, 4) if v is not None else None for v in roll],
        "composite": [round(v, 5) if v is not None else None for v in composite],
        "events": events,
        "policy_count": len(POLICY_EVENTS),
        "global_count": len(GLOBAL_EVENTS),
        "available_count": len(available),
        "conditional": cond,
        "sources": [{"name": x["name"], "source": x["source"],
                     "date": x["date"], "quality": x["quality"], "error": x["error"]}
                    for x in latest],
        "boundary": "政策与外围数据仅用于观察、传导统计与 5 日条件情景抽样，未进入回测信号；"
                    "原回测仍是纯 A 股量价规则（双均线 + 波动率目标仓位），"
                    "未读入宏观或跨市场变量，历史绩效未被改写。",
        "disclaimer": "政策条目为公开权威报道要点（附来源），非行情数据；外围指数为免费公开接口真实日线，"
                      "异常值如实剔除、不填补。相关性不代表因果，历史分组不代表未来，不构成投资建议。",
    }
