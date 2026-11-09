# -*- coding: utf-8 -*-
"""真实行情快照生成器 —— 量化模拟盘监控台的数据层。

流程：
  1. 联网拉取沪深300「当前成分」与真实不复权日线（AKShare / 东方财富），
     备用源新浪；请求带指数退避重试，结果逐标的落盘缓存，可断点续传。
  2. 拉取沪深300真实指数（新浪）作为基准与**真实交易日历**。
  3. 用真实涨跌幅连乘构造前复权序列（策略信号用），不复权价用于成交、
     涨跌停与滑点（T-002）。
  4. 复用 quant.backtest.costs / rules 跑严格 A 股语义的事件驱动回测：
     T 日收盘生成信号 → T+1 开盘撮合、100 股整数倍、涨跌停夹紧、
     停牌顺延 3 日、佣金/印花税/滑点、单票/总仓位与回撤刹车状态机。
  5. 导出紧凑 snapshot.json，并把它注入 template.html 生成自包含 index.html。

运行：
  python dashboard/build.py              # 用缓存 + 补拉缺失，生成页面
  python dashboard/build.py --refresh    # 强制重新拉取真实行情
  python dashboard/build.py --offline    # 只用缓存，不联网（离线复现）
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "src"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import numpy as np
import pandas as pd

import macro as macro_mod  # 政策 / 外围市场传导层（真实数据，不改回测绩效）

from quant.backtest import costs, rules  # 复用项目唯一的成本/规则实现

SNAPSHOT = HERE / "snapshot.json"
TEMPLATE = HERE / "template.html"
OUTPUT = HERE / "index.html"

START = "2024-01-02"
POOL_SIZE = 28
REBALANCE_EVERY = 5      # 周频调仓，贴近真实中低频组合管理
DEADBAND = 0.02          # 目标权重与当前权重差 <2pct 不下单
INITIAL_CASH = 1_000_000.0
OOS_RATIO = 0.20
MAX_RETRY = 4
RETRY_BASE = 1.0
REQUEST_GAP = 0.35

# 后续 5 日「情景推演」参数。注意：这是基于真实历史收益分布的统计外推，
# 不是行情数据、不生成订单、不参与回测，页面与报告均显式标注。
FORECAST_DAYS = 5
FORECAST_PATHS = 800
FORECAST_WINDOW = 250
FORECAST_SEED = 20261109

# 真实沪深300权重股优先池；与「当前成分」取交集后按代码排序，不足再按代码补齐。
PREFERRED = [
    "600519", "601318", "600036", "000858", "000333", "601166", "600900",
    "000651", "600276", "002415", "600030", "601888", "600887", "601899",
    "600309", "601336", "601601", "600031", "000568", "600809", "000001",
    "600000", "601398", "601288", "600028", "601857", "600585", "601668",
    "000725", "600050", "601988", "600104", "601766", "600048", "000063",
]
INDUSTRY = {
    "600519": "食品饮料", "601318": "非银金融", "600036": "银行",
    "000858": "食品饮料", "000333": "家用电器", "601166": "银行",
    "600900": "公用事业", "000651": "家用电器", "600276": "医药生物",
    "600030": "非银金融", "601888": "社会服务", "600887": "食品饮料",
    "601899": "有色金属", "600309": "基础化工", "601336": "非银金融",
    "601601": "非银金融", "600031": "机械设备", "000568": "食品饮料",
    "600809": "食品饮料", "000001": "银行", "600000": "银行",
    "601398": "银行", "601288": "银行", "600028": "石油石化",
    "601857": "石油石化", "600585": "建筑材料", "601668": "建筑装饰",
    "000725": "电子", "600050": "通信", "601988": "银行",
    "600104": "汽车", "601766": "机械设备", "600048": "房地产",
    "000063": "通信", "002415": "电子",
}

DEFAULTS = {
    "costs.commission_rate": 0.00025,
    "costs.commission_min": 5.0,
    "costs.stamp_tax_rate": 0.0005,
    "costs.slippage_rate": 0.0001,
    "rules.lot_size": 100,
    "rules.price_limit_pct": 0.10,
    "rules.max_suspension_days": 3,
    "backtest.initial_cash": INITIAL_CASH,
}


class Cfg:
    """给 costs.py / rules.py 提供 cfg.get('a.b') 接口。"""

    def __init__(self, **overrides):
        self.d = {**DEFAULTS, **overrides}

    def get(self, key, default=None):
        return self.d.get(key, default)


CFG = Cfg()

# level: 0=info 1=warn 2=error 3=risk 4=data 5=trade
RAW_EVENTS: list[tuple[str, int, str]] = []


def event(date: str, level: int, text: str) -> None:
    RAW_EVENTS.append((date, level, text))


def log(msg: str) -> None:
    print(msg, flush=True)


def to_symbol(code: str) -> str:
    return code + (".SH" if code.startswith("6") else ".SZ")


# ---------------------------------------------------------------- 数据获取

def _std_frame(raw: pd.DataFrame, source: str) -> pd.DataFrame:
    """把不同源的字段统一成内部列。"""
    raw = raw.copy()
    if "日期" in raw.columns:
        df = pd.DataFrame({
            "date": pd.to_datetime(raw["日期"]).dt.strftime("%Y-%m-%d"),
            "open": pd.to_numeric(raw["开盘"], errors="coerce"),
            "high": pd.to_numeric(raw["最高"], errors="coerce"),
            "low": pd.to_numeric(raw["最低"], errors="coerce"),
            "close": pd.to_numeric(raw["收盘"], errors="coerce"),
            "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
            "amount": pd.to_numeric(raw["成交额"], errors="coerce"),
            "pct_chg": pd.to_numeric(raw["涨跌幅"], errors="coerce"),
        })
    else:
        s = raw.reset_index()
        dcol = "date" if "date" in s.columns else s.columns[0]
        df = pd.DataFrame({
            "date": pd.to_datetime(s[dcol]).dt.strftime("%Y-%m-%d"),
            "open": pd.to_numeric(s["open"], errors="coerce"),
            "high": pd.to_numeric(s["high"], errors="coerce"),
            "low": pd.to_numeric(s["low"], errors="coerce"),
            "close": pd.to_numeric(s["close"], errors="coerce"),
            "volume": pd.to_numeric(s["volume"], errors="coerce"),
            "amount": pd.to_numeric(s.get("amount", 0), errors="coerce"),
            "pct_chg": np.nan,
        })
        df["pct_chg"] = df["close"].pct_change() * 100.0
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["close"] > 0) & (df["open"] > 0)]
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    df["volume"] = df["volume"].fillna(0.0)
    df["amount"] = df["amount"].fillna(0.0)
    df["pct_chg"] = df["pct_chg"].fillna(0.0)
    df.attrs["source"] = source
    return df


def fetch_one(code: str, start: str, end: str, *, refresh: bool, offline: bool,
              today: str) -> tuple[pd.DataFrame | None, str]:
    """单标的真实日线；缓存 → 东财 → 新浪降级；返回 (df, 来源说明)。"""
    path = CACHE / f"{code}.json"
    if path.exists() and not refresh:
        try:
            df = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
            if len(df) >= 50:
                return df, "cache"
        except Exception:
            pass
    if offline:
        return None, "cache_miss"

    s, e = start.replace("-", ""), end.replace("-", "")
    last_err = ""
    for attempt in range(MAX_RETRY):
        try:
            import akshare as ak
            raw = ak.stock_zh_a_hist(symbol=code, period="daily",
                                     start_date=s, end_date=e, adjust="")
            df = _std_frame(raw, "eastmoney")
            if len(df) < 50:
                raise RuntimeError(f"有效交易日仅 {len(df)} 条")
            path.write_text(json.dumps(df.to_dict("records"), ensure_ascii=False),
                            encoding="utf-8")
            if attempt:
                event(today, 4, f"{to_symbol(code)} 东财接口第 {attempt} 次退避重试后恢复，取得 {len(df)} 条真实日线")
            time.sleep(REQUEST_GAP)
            return df, f"eastmoney:{len(df)}bars"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:90]}"
            if attempt < MAX_RETRY - 1:
                time.sleep(RETRY_BASE * (2 ** attempt))
    event(today, 2, f"{to_symbol(code)} 东财接口重试 {MAX_RETRY} 次失败（{last_err}），降级新浪行情源")

    try:  # 备用源：新浪（免费、匿名）
        import akshare as ak
        raw = ak.stock_zh_a_daily(symbol=("sh" if code.startswith("6") else "sz") + code,
                                  start_date=s, end_date=e, adjust="")
        df = _std_frame(raw, "sina")
        df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
        if len(df) < 50:
            raise RuntimeError("备用源样本过少")
        path.write_text(json.dumps(df.to_dict("records"), ensure_ascii=False),
                        encoding="utf-8")
        event(today, 4, f"{to_symbol(code)} 已降级至新浪源，取得 {len(df)} 条真实日线")
        time.sleep(REQUEST_GAP)
        return df, f"sina:{len(df)}bars"
    except Exception as exc:
        event(today, 2, f"{to_symbol(code)} 全部数据源不可用，已剔除（{type(exc).__name__}）")
        return None, "unavailable"


def fetch_universe(offline: bool, today: str) -> tuple[list[tuple[str, str]], str]:
    """沪深300当前成分 → 过滤非主板/ST → 权重股 + 近期纳入标的，按代码排序。

    纳入近期新成分是为了让数据里出现**真实**的未上市区间、上市初期缺失与
    高波动行情，而不是人为编造停牌或涨停。
    """
    cache = CACHE / "universe_v2.json"
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
        source = "cache:index_stock_cons"
    elif offline:
        raise RuntimeError("离线模式缺少 universe 缓存")
    else:
        import akshare as ak
        for attempt in range(MAX_RETRY):
            try:
                cons = ak.index_stock_cons(symbol="000300")
                rows = cons[["品种代码", "品种名称", "纳入日期"]].astype(str).values.tolist()
                cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                source = "akshare:index_stock_cons"
                break
            except Exception as exc:
                if attempt == MAX_RETRY - 1:
                    raise RuntimeError(f"成分股拉取失败: {exc}")
                time.sleep(RETRY_BASE * (2 ** attempt))
        else:
            source = "unknown"
    all_main: dict[str, tuple[str, str]] = {}
    for row in rows:
        code, name = str(row[0]).zfill(6), str(row[1])
        in_date = str(row[2]) if len(row) > 2 else ""
        if not code.startswith(("600", "601", "603", "605", "000", "001")):
            continue
        if "ST" in name.upper():
            continue
        all_main[code] = (name, in_date)
    preferred = [c for c in PREFERRED if c in all_main][:POOL_SIZE - 8]
    recent = sorted((c for c in all_main if c not in preferred),
                    key=lambda c: all_main[c][1], reverse=True)[:8]
    picked = sorted(set(preferred + recent))[:POOL_SIZE]
    event(today, 4, f"股票池加载：沪深300 当前成分 {len(rows)} 只，主板且非ST {len(all_main)} 只；"
                    f"取权重股 {len(preferred)} 只 + 近期纳入 {len(recent)} 只，按代码排序共 {len(picked)} 只")
    return [(c, all_main[c][0]) for c in picked], source


def fetch_index(start: str, offline: bool, today: str) -> tuple[pd.DataFrame, str]:
    """沪深300真实指数（新浪），同时作为真实交易日历。"""
    cache = CACHE / "index.json"
    if cache.exists() and offline:
        df = pd.DataFrame(json.loads(cache.read_text(encoding="utf-8")))
        return df, "cache:sina_index"
    if not offline:
        try:
            import akshare as ak
            raw = ak.stock_zh_index_daily(symbol="sh000300")
            d = raw.copy()
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            d = d[["date", "open", "high", "low", "close", "volume"]]
            d = d.sort_values("date").reset_index(drop=True)
            cache.write_text(json.dumps(d.to_dict("records"), ensure_ascii=False),
                             encoding="utf-8")
            event(today, 4, f"基准与交易日历：新浪沪深300真实指数 {len(d)} 条，最新 {d['date'].iloc[-1]}")
            return d, "sina:sh000300"
        except Exception as exc:
            event(today, 2, f"指数实时拉取失败（{type(exc).__name__}），使用本地缓存")
    if cache.exists():
        return pd.DataFrame(json.loads(cache.read_text(encoding="utf-8"))), "cache:sina_index"
    raise RuntimeError("无可用交易日历（指数缓存缺失）")


# ---------------------------------------------------------------- 回测内核

STATUS = {"pending": 0, "filled": 1, "rejected": 2, "expired": 3,
          "deferred": 4, "cancelled": 5}
REASON = {"": 0, "limit_up": 1, "limit_down": 2, "suspended": 3,
          "cash_insufficient": 4, "risk_cap": 5, "no_bar": 6,
          "adj_factor_missing": 7, "waiting_for_next_bar": 8}


def run_backtest(price_map: dict[int, pd.DataFrame], codes: list[str],
                 dates: list[str], index_close: dict[str, float]) -> dict:
    """严格按 T-003 事件顺序逐日推进，只读内存中的真实行情。"""
    n_sym = len(codes)
    bars: dict[tuple[int, str], dict] = {}
    for si, df in price_map.items():
        for r in df.to_dict("records"):
            bars[(si, r["date"])] = r

    date_idx = {d: i for i, d in enumerate(dates)}
    next_day = {dates[i]: dates[i + 1] if i + 1 < len(dates) else None
                for i in range(len(dates))}

    cash = INITIAL_CASH
    positions = {si: {"qty": 0, "avg": 0.0, "last": 0.0, "stale": 0} for si in range(n_sym)}
    hist: dict[int, deque] = {si: deque(maxlen=90) for si in range(n_sym)}
    peak = INITIAL_CASH
    braked, brake_since = False, None

    orders: list[dict] = []
    pending: list[int] = []
    oid_seq = 0

    equity_ts, positions_ts, signal_state_ts = [], [], []
    last_signals: list[list] = []
    daily_turnover, daily_fee, daily_slip, daily_pnl = [], [], [], []
    daily_trade_count, daily_signal_count = [], []
    total_fee = total_slip = total_turnover = 0.0
    bench0 = index_close[dates[0]]
    warmup_noted = False

    def trace(o: dict, d: str, status: str, reason: str = "") -> None:
        o["trace"].append([date_idx.get(d, 0), STATUS[status], REASON.get(reason, 0)])

    for di, today in enumerate(dates):
        day_turn = day_fee = day_slip = 0.0
        day_trades = 0

        # ---- 1. 撮合所有 <= 今日到期的 pending（T+1 开盘价） ----
        still: list[int] = []
        for oi in pending:
            o = orders[oi]
            if o["valid_from"] and o["valid_from"] > today:
                still.append(oi)
                continue
            si, side = o["si"], o["side"]
            bar = bars.get((si, today))
            if bar is None or float(bar["volume"]) <= 0:
                o["attempts"] += 1
                if o["attempts"] > int(CFG.get("rules.max_suspension_days")):
                    o["status"], o["reason"] = "expired", "suspended"
                    trace(o, today, "expired", "suspended")
                    event(today, 2, f"{codes[si]} 停牌顺延超 3 日，订单 {o['id']} expired（持仓按最后有效收盘价估值）")
                else:
                    o["status"], o["reason"] = "deferred", "suspended"
                    o["valid_from"] = next_day.get(today)
                    trace(o, today, "deferred", "suspended")
                    still.append(oi)
                    event(today, 1, f"{codes[si]} 无行情/成交量 0，订单 {o['id']} 顺延至 {o['valid_from'] or '下一交易日'}（{o['attempts']}/3）")
                continue

            dec = rules.decide_fill(float(bar["open"]), "BUY" if side == 1 else "SELL",
                                    float(bar["prev_close"]), CFG)
            if dec.blocked:
                o["status"] = "rejected"
                o["reason"] = dec.blocked
                trace(o, today, "rejected", dec.blocked)
                tag = "涨停买入不成交" if dec.blocked == "limit_up" else "跌停卖出不成交"
                event(today, 2, f"{codes[si]} 开盘触及{'涨停' if dec.blocked == 'limit_up' else '跌停'}，订单 {o['id']} rejected · {tag}")
                continue

            qty = int(o["qty"])
            if side == 1:
                affordable = costs.max_affordable_shares(cash, float(bar["open"]), "BUY", CFG)
                qty = rules.round_lot(min(qty, affordable), CFG)
                if qty < int(CFG.get("rules.lot_size")):
                    o["status"], o["reason"] = "rejected", "cash_insufficient"
                    trace(o, today, "rejected", "cash_insufficient")
                    event(today, 2, f"{codes[si]} 现金不足（可用 ¥{cash:,.0f}），订单 {o['id']} rejected · 按含费最大手数无法成交")
                    continue

            price = float(dec.price)
            notional = price * qty
            fc = costs.compute_fill_costs(price, float(bar["open"]), qty,
                                          "BUY" if side == 1 else "SELL", CFG)
            fee = fc.commission + fc.stamp_tax
            pos = positions[si]
            if side == 1:
                cash -= notional + fee
                pos["avg"] = (pos["avg"] * pos["qty"] + notional + fee) / (pos["qty"] + qty)
                pos["qty"] += qty
            else:
                cash += notional - fee
                realized = (price - pos["avg"]) * qty - fee
                daily_pnl.append((di, realized))
                pos["qty"] -= qty
                if pos["qty"] <= 0:
                    pos["qty"], pos["avg"] = 0, 0.0
            o.update(status="filled", qty=qty, price=round(price, 3),
                     fill_idx=di, fee=round(fee, 2))
            trace(o, today, "filled")
            day_turn += notional
            day_fee += fee
            day_slip += fc.slippage_cost
            day_trades += 1
            event(today, 5, f"{'买入' if side == 1 else '卖出'} {codes[si]} {qty:,} 股 @ ¥{price:.2f} · 订单 {o['id']} filled · 费用 ¥{fee:,.2f}")
        pending = still

        # ---- 2. 收盘 mark-to-market ----
        for si in range(n_sym):
            bar = bars.get((si, today))
            if bar is not None and float(bar["volume"]) > 0:
                positions[si]["last"] = float(bar["close"])
                positions[si]["stale"] = 0
                hist[si].append(float(bar["qfq"]))
            else:
                positions[si]["stale"] = 1
        market_value = sum(p["qty"] * p["last"] for p in positions.values())
        equity = cash + market_value
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        nav = equity / INITIAL_CASH
        bench_nav = index_close.get(today, bench0) / bench0

        stale_count = sum(1 for p in positions.values() if p["qty"] > 0 and p["stale"])
        if stale_count:
            event(today, 1, f"权益估值：{stale_count} 只持仓停牌，按最后有效收盘价估值（stale_price=1）")
        missing_today = sum(1 for si in range(n_sym) if (si, today) not in bars)
        if missing_today and di % 20 == 0:
            event(today, 1, f"行情覆盖：当日 {missing_today}/{n_sym} 只标的无行情（真实未上市或停牌），不生成信号、不填补")
        limit_hits = []
        for si in range(n_sym):
            b = bars.get((si, today))
            if b is not None and abs(float(b["pct_chg"])) >= 9.8:
                limit_hits.append(codes[si])
        if len(limit_hits) >= 2:
            event(today, 1, f"行情异常观察：当日 {len(limit_hits)} 只标的涨跌幅接近 ±10% 限制（{'、'.join(limit_hits[:3])} 等），相关订单按涨跌停规则处理")

        # ---- 3. 生成 T 日信号（仅用截至 T 日的前复权历史） ----
        weights: dict[int, float] = {}
        for si in range(n_sym):
            h = hist[si]
            if len(h) < 61:
                continue
            arr = np.asarray(h, dtype=float)
            ma_s, ma_l, px = arr[-20:].mean(), arr[-60:].mean(), arr[-1]
            if ma_s > ma_l and px > ma_s:
                rets = np.diff(np.log(arr[-21:]))
                vol = float(rets.std()) if len(rets) > 1 else 0.02
                scale = max(0.35, min(1.0, 0.020 / max(vol, 0.005)))
                weights[si] = 0.10 * scale
        bull_count = len(weights)
        if len(hist[0]) >= 61 and not warmup_noted:
            warmup_noted = True
            event(today, 0, "双均线（20/60）预热完成，开始生成目标权重；参数在全区间固定，样本外不做二次调参")

        # ---- 4. 风控调整（独立状态机） ----
        cap, risk_changed = 0.80, False
        if not braked and dd >= 0.10:
            braked, brake_since, risk_changed = True, di, True
            cap = 0.40
            event(today, 3, f"回撤刹车触发：当前回撤 {dd:.2%} ≥ 10%，总仓位上限降为 40%，暂停新开仓扩张")
        elif braked:
            if dd <= 0.05:
                braked, brake_since, risk_changed = False, None, True
                cap = 0.80
                event(today, 3, f"回撤刹车解除：回撤修复至 {dd:.2%} ≤ 5%，仓位上限恢复 80%")
            elif brake_since is not None and di - brake_since >= 20:
                braked, brake_since, risk_changed = False, None, True
                cap = 0.80
                event(today, 3, "回撤刹车解除：已满 20 个交易日冷却期，仓位上限恢复 80%")
            else:
                cap = 0.40
        single_cap = 0.10
        weights = {si: min(w, single_cap) for si, w in weights.items()}
        total_w = sum(weights.values())
        if total_w > cap:
            k = cap / total_w
            weights = {si: w * k for si, w in weights.items()}
            if di % 20 == 0:
                event(today, 3, f"组合权重裁剪：{total_w:.1%} → {cap:.0%}（单票 ≤10%{'，回撤刹车中' if braked else ''}）")

        # ---- 5. 目标权重 → pending 订单（股数按 T 日收盘价估算） ----
        # 周频调仓 + 死区：避免每日微调造成的非真实高频换手；风控状态变化立即放行。
        # 相位从最新交易日倒数，使最后一笔调仓落在最新收盘，符合每日模拟盘的现场状态。
        allow_rebalance = ((len(dates) - 1 - di) % REBALANCE_EVERY == 0) or risk_changed
        busy = {orders[i]["si"] for i in pending}
        sig_count = 0
        for si in range(n_sym):
            if si in busy or not allow_rebalance:
                continue
            bar_t = bars.get((si, today))
            if bar_t is None or positions[si]["stale"]:
                continue
            close_t = float(bar_t["close"])
            target_w = weights.get(si, 0.0)
            current_w = positions[si]["qty"] * close_t / equity if equity > 0 else 0.0
            if abs(target_w - current_w) < DEADBAND:
                continue
            target = rules.target_shares(equity, target_w, close_t, CFG)
            delta = target - positions[si]["qty"]
            if abs(delta) < int(CFG.get("rules.lot_size")):
                continue
            side = 1 if delta > 0 else -1
            qty = int(delta) if side == 1 else min(int(-delta), positions[si]["qty"])
            qty = rules.round_lot(qty, CFG)
            if qty < 100:
                continue
            oid_seq += 1
            oid = f"O{oid_seq:05d}"
            o = {"id": oid, "si": si, "side": side, "qty": qty,
                 "placed_idx": di, "valid_from": next_day.get(today),
                 "status": "pending", "reason": "", "price": 0.0,
                 "ref": round(close_t, 3),  # T 日收盘价，用于 T+1 计划估算（T-004）
                 "fill_idx": -1, "fee": 0.0, "attempts": 0,
                 "trace": [[di, STATUS["pending"], 0]]}
            if o["valid_from"] is None:
                o["reason"] = "waiting_for_next_bar"
                o["trace"] = [[di, STATUS["pending"], REASON["waiting_for_next_bar"]]]
            orders.append(o)
            pending.append(len(orders) - 1)
            sig_count += 1
        if sig_count:
            event(today, 0, f"{'风控触发即时' if risk_changed else '周频'}调仓：{sig_count} 只标的权重偏离超 2pct 阈值，"
                            f"生成 pending 订单，T+1 开盘撮合（不二次调仓）")

        # ---- 落当日快照 ----
        pos_rec = []
        for si, p in positions.items():
            if p["qty"] <= 0:
                continue
            bar_t = bars.get((si, today))
            day_pct = float(bar_t["pct_chg"]) / 100.0 if bar_t else 0.0
            pnl = (p["last"] - p["avg"]) / p["avg"] if p["avg"] > 0 else 0.0
            pos_rec.append([
                si, p["qty"], round(p["avg"], 3), round(p["last"], 3),
                round(pnl, 4), round(day_pct, 4), p["stale"],
                round(p["qty"] * p["last"] / equity, 4) if equity > 0 else 0.0,
            ])
        pos_rec.sort(key=lambda r: -r[7])
        positions_ts.append(pos_rec)
        equity_ts.append([round(nav, 6), round(bench_nav, 6), round(dd, 6),
                          round(cash, 2), round(market_value / equity, 4) if equity else 0.0,
                          round(index_close.get(today, bench0), 2), int(braked)])
        # 策略可解释性：每个交易日的多头信号数、目标总仓位、风控状态、待成交数、是否调仓日
        signal_state_ts.append([bull_count, round(float(sum(weights.values())), 4),
                                int(braked), len(pending), int(allow_rebalance)])
        if di == len(dates) - 1:
            # 最新收盘的逐标的信号明细，全部由截至当日的真实前复权序列计算
            for si in range(n_sym):
                h = hist[si]
                if len(h) < 61:
                    last_signals.append([si, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0.0])
                    continue
                arr = np.asarray(h, dtype=float)
                ma_s, ma_l, px = arr[-20:].mean(), arr[-60:].mean(), arr[-1]
                rets = np.diff(np.log(arr[-21:]))
                vol = float(rets.std()) if len(rets) > 1 else 0.0
                sig = 1 if (ma_s > ma_l and px > ma_s) else 0
                target_w = weights.get(si, 0.0) if sig else 0.0
                bar_t = bars.get((si, today))
                close_t = float(bar_t["close"]) if bar_t is not None else 0.0
                current_w = positions[si]["qty"] * close_t / equity if equity > 0 else 0.0
                gap = target_w - current_w
                action = 1 if gap > DEADBAND else (-1 if gap < -DEADBAND else 0)
                last_signals.append([
                    si, round(float(ma_s), 3), round(float(ma_l), 3), sig,
                    round(vol * math.sqrt(252), 4), round(target_w, 4),
                    round(current_w, 4), action, round(close_t, 2),
                ])
            last_signals.sort(key=lambda r: (-r[3], -r[5]))
        daily_turnover.append(day_turn)
        daily_fee.append(day_fee)
        daily_slip.append(day_slip)
        daily_trade_count.append(day_trades)
        daily_signal_count.append(sig_count)
        total_fee += day_fee
        total_slip += day_slip
        total_turnover += day_turn

        if di % 20 == 0 or di == len(dates) - 1:
            event(today, 0, f"收盘结算 · 权益 ¥{equity:,.0f} · 净值 {nav:.4f} · 持仓 {len(pos_rec)} 只 · 仓位 {market_value / equity:.1%} · 成交 {day_trades} 笔")

    # ---- 指标（公式固定：夏普 sqrt(252)、无风险 0、胜率按已平仓交易） ----
    def metrics(a: int, b: int) -> dict:
        seg = equity_ts[a:b]
        if not seg:
            return {}
        navs = np.array([r[0] for r in seg], dtype=float)
        rets = np.diff(navs) / navs[:-1]
        total_ret = navs[-1] / navs[0] - 1
        years = max(len(seg) / 252.0, 1 / 252)
        annual = (navs[-1] / navs[0]) ** (1 / years) - 1
        vol = float(rets.std()) if len(rets) > 1 else 0.0
        sharpe = float(rets.mean() / vol * math.sqrt(252)) if vol > 1e-12 else 0.0
        max_dd = max(r[2] for r in seg)
        turnover = sum(daily_turnover[a:b])
        fee = sum(daily_fee[a:b])
        avg_equity = float(np.mean([r[0] for r in seg])) * INITIAL_CASH
        closed = [p for d, p in daily_pnl if a <= d < b]
        return {
            "total_return": round(float(total_ret), 4),
            "annual_return": round(float(annual), 4),
            "max_drawdown": round(float(max_dd), 4),
            "sharpe": round(sharpe, 3),
            "win_rate": round(sum(1 for p in closed if p > 0) / len(closed), 4) if closed else 0.0,
            "closed_trades": len(closed),
            "turnover_ratio": round(turnover / avg_equity, 4) if avg_equity else 0.0,
            "cost_ratio": round(fee / INITIAL_CASH, 4),
            "days": len(seg),
            "final_nav": round(float(navs[-1]), 4),
        }

    split = int(len(dates) * (1 - OOS_RATIO))
    status_counts = {}
    for o in orders:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1
    reason_counts = {}
    for o in orders:
        if o["reason"]:
            reason_counts[o["reason"]] = reason_counts.get(o["reason"], 0) + 1

    return {
        "dates": dates, "equity": equity_ts, "positions": positions_ts,
        "orders": [{
            "id": o["id"], "si": o["si"], "side": o["side"], "qty": o["qty"],
            "p": o["placed_idx"], "status": STATUS[o["status"]], "reason": REASON.get(o["reason"], 0),
            "price": o["price"], "ref": o["ref"], "f": o["fill_idx"], "fee": o["fee"],
            "trace": o["trace"],
        } for o in orders],
        "signal_state": signal_state_ts,
        "signal_rows": last_signals,
        "signal_date": dates[-1],
        "metrics": {"full": metrics(0, len(dates)),
                    "in_sample": metrics(0, split),
                    "out_of_sample": metrics(split, len(dates))},
        "split_idx": split,
        "summary": {
            "total_fee": round(total_fee, 2), "total_slippage": round(total_slip, 2),
            "total_turnover": round(total_turnover, 2),
            "status_counts": status_counts, "reason_counts": reason_counts,
            "closed_trades": len(daily_pnl),
            "daily_trade_count": daily_trade_count,
            "daily_signal_count": daily_signal_count,
        },
    }


# ------------------------------------------- 策略说明 / 未来 5 日情景推演

def build_strategy(bt: dict) -> dict:
    """把回测实际使用的规则、参数与逐日状态导出，供页面解释策略。"""
    return {
        "id": "dual_ma",
        "name": "双均线趋势 + 波动率目标仓位",
        "lead": "趋势过滤决定「买不买」，波动率倒数决定「买多少」，独立风控层决定「最多买多少」。"
                "信号只用截至 T 日收盘的前复权序列，T+1 开盘撮合；参数在全区间固定，样本外不做二次调参。",
        "params": [
            ["短期 / 长期均线", "20 / 60 日", "前复权收盘价简单移动平均"],
            ["入场条件", "MA20 > MA60 且 收盘 > MA20", "两者同时满足才给目标权重"],
            ["波动率窗口", "20 日", "对数收益标准差，年化展示"],
            ["目标日波动", "2.0%", "仓位缩放 0.02 / 实际波动，截断于 35%~100%"],
            ["单票基准权重", "10%", "乘以波动率缩放后为最终目标权重"],
            ["调仓频率", f"每 {REBALANCE_EVERY} 个交易日", "周频；风控状态变化当天立即放行"],
            ["调仓死区", f"{DEADBAND:.0%}", "目标与当前权重偏离小于该值不下单"],
            ["样本外比例", f"最后 {OOS_RATIO:.0%}", "固定参数跑全程后分段报告，无参数泄漏"],
            ["初始资金", "¥1,000,000", "本地模拟盘，不接券商"],
        ],
        "rules": [
            ["成交时点", "T 日收盘生成信号 → T+1 开盘价撮合，禁止未来函数"],
            ["复权口径", "信号/均线用前复权；成交价、涨跌停、滑点一律用不复权价"],
            ["数量约束", "100 股整数倍；现金不足按含手续费的最大手数成交"],
            ["交易成本", "佣金万 2.5（最低 5 元）· 印花税卖出单边 0.05% · 滑点万 1"],
            ["涨跌停", "仅沪深主板 ±10% 近似；涨停买入 / 跌停卖出不成交，成交价夹紧在区间内"],
            ["停牌", "无行情或 volume=0 顺延最多 3 个交易日，超时 expired；持仓按最后有效收盘价估值"],
            ["仓位上限", "单票 ≤10% · 组合 ≤80%"],
            ["回撤刹车", "回撤 ≥10% 触发，总仓位上限降至 40% 并暂停新开仓扩张"],
            ["刹车解除", "回撤修复至 ≤5%，或满 20 个交易日冷却期"],
            ["数据纪律", "行情缺失不填补、不伪造；免费源缺失退市股票，当前成分回溯存在幸存者偏差"],
        ],
        "rebalance_every": REBALANCE_EVERY,
        "deadband": DEADBAND,
        "signal_state": bt["signal_state"],
        "signal_rows": bt["signal_rows"],
        "signal_date": bt["signal_date"],
        "oos_ratio": OOS_RATIO,
    }


def build_forecast(equity: list[list]) -> dict:
    """严格无未来函数的 5 日统计情景推演。

    对每个交易日 i，只用截至 i 的真实策略 / 基准日收益序列（滚动
    FORECAST_WINDOW 天），做 5 日连续收益块自助抽样（block bootstrap），
    保留短期波动聚集与策略—基准的相关性。固定随机种子，离线完全可复现。

    产出是**模拟路径分布**，不是行情数据：不写入 daily_price、不生成订单、
    不参与回测，也不影响任何已实现指标。页面与 README 均显式标注该边界。
    """
    nav = np.asarray([r[0] for r in equity], dtype=float)
    bench = np.asarray([r[1] for r in equity], dtype=float)
    sr = np.diff(nav) / np.where(nav[:-1] > 0, nav[:-1], 1.0)
    br = np.diff(bench) / np.where(bench[:-1] > 0, bench[:-1], 1.0)
    data: list = []
    for i in range(len(nav)):
        lo = max(0, i - FORECAST_WINDOW)
        s, b = sr[lo:i], br[lo:i]  # s[k] 为第 k+1 日收益，切片严格不含未来
        if len(s) < 30:
            data.append(None)
            continue
        rng = np.random.default_rng(FORECAST_SEED + i)
        max_start = len(s) - FORECAST_DAYS
        starts = rng.integers(0, max_start + 1, size=FORECAST_PATHS)
        idx = starts[:, None] + np.arange(FORECAST_DAYS)[None, :]
        cs = np.cumprod(1.0 + s[idx], axis=1)   # 策略净值相对倍数
        cb = np.cumprod(1.0 + b[idx], axis=1)   # 基准相对倍数（同一抽样块，保留相关性）
        q = np.quantile(cs, [0.10, 0.50, 0.90], axis=0)
        b50 = np.quantile(cb, 0.50, axis=0)
        data.append([
            [[round(float(q[0, k]), 5), round(float(q[1, k]), 5),
              round(float(q[2, k]), 5), round(float(b50[k]), 5)]
             for k in range(FORECAST_DAYS)],
            round(float((cs[:, -1] < 1.0).mean()), 3),        # 5 日后低于当前净值
            round(float((cs[:, -1] > cb[:, -1]).mean()), 3),   # 5 日跑赢基准
            round(float(s.std() * math.sqrt(252)), 4),         # 年化波动估计
            round(float((cs.min(axis=1) <= 0.97).mean()), 3),  # 期间短期回撤 ≥3% 情景
        ])
    return {
        "days": FORECAST_DAYS, "paths": FORECAST_PATHS,
        "window": FORECAST_WINDOW, "seed": FORECAST_SEED,
        "data": data,
        "method": f"以截至所选交易日的真实策略/基准日收益（滚动 {FORECAST_WINDOW} 日）为经验分布，"
                  f"抽取 {FORECAST_PATHS} 条 {FORECAST_DAYS} 日连续收益块路径，"
                  f"对策略与基准使用同一抽样块以保留相关性；固定随机种子 {FORECAST_SEED}。",
        "disclaimer": "统计情景推演，不是行情预测，也不是未来真实行情。不含任何未来数据、不写入行情库、"
                      "不生成订单、不参与回测与绩效计算；历史分布不代表未来，不构成投资建议。",
    }


# ---------------------------------------------------------------- 导出

def main() -> int:
    global POOL_SIZE
    ap = argparse.ArgumentParser(description="生成真实行情量化模拟盘监控台")
    ap.add_argument("--refresh", action="store_true", help="强制重新拉取真实行情")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--start", default=START)
    ap.add_argument("--limit", type=int, default=POOL_SIZE)
    args = ap.parse_args()
    POOL_SIZE = args.limit

    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    t0 = time.perf_counter()

    idx_df, index_source = fetch_index(args.start, args.offline, today)
    idx_all = idx_df[(idx_df["date"] >= args.start)].reset_index(drop=True)
    dates = idx_all["date"].astype(str).tolist()
    end = dates[-1]
    index_close = {str(r["date"]): float(r["close"]) for r in idx_all.to_dict("records")}
    log(f"[1/5] 真实交易日历 {len(dates)} 天：{dates[0]} → {end}（{index_source}）")

    universe, uni_source = fetch_universe(args.offline, today)
    log(f"[2/5] 股票池 {len(universe)} 只主板成分（{uni_source}）")

    price_map, symbols, sources, failed = {}, [], {}, []
    for i, (code, name) in enumerate(universe, 1):
        df, src = fetch_one(code, args.start, end, refresh=args.refresh,
                            offline=args.offline, today=today)
        if df is None:
            failed.append(code)
            continue
        df = df[(df["date"] >= args.start) & (df["date"] <= end)].reset_index(drop=True)
        if len(df) < 50:
            failed.append(code)
            continue
        # 前复权序列：真实涨跌幅连乘；不复权价用于成交/涨跌停（T-002）
        qfq, v = [], float(df["close"].iloc[0])
        for pct in df["pct_chg"].tolist():
            qfq.append(v)
            v *= (1.0 + float(pct) / 100.0)
        df["qfq"] = qfq
        df["prev_close"] = df["close"].shift(1).fillna(df["open"])
        df["adj_factor"] = df["qfq"] / df["close"]
        si = len(symbols)
        price_map[si] = df
        symbols.append([to_symbol(code), name, INDUSTRY.get(code, "沪深300成分")])
        sources[code] = src
        if i % 6 == 0 or i == len(universe):
            log(f"      行情 {i}/{len(universe)} 已就绪，最新 {len(symbols)} 只可用")
    if len(symbols) < 8:
        raise RuntimeError(f"可用标的仅 {len(symbols)} 只，不足以运行（失败：{failed}）")
    codes = [s[0] for s in symbols]
    log(f"[3/5] 真实日线加载完成：{len(symbols)} 只 / 失败 {len(failed)} 只 / "
        f"{sum(len(d) for d in price_map.values()):,} 条 K 线")

    # 数据质量审计（真实缺失，不填补）
    expected = len(dates) * len(symbols)
    actual = sum(len(d) for d in price_map.values())
    adj_missing = int(sum((d["adj_factor"] <= 0).sum() for d in price_map.values()))
    zero_vol = int(sum((d["volume"] <= 0).sum() for d in price_map.values()))
    event(dates[0], 4, f"数据审计：expected {expected:,} / actual {actual:,} / missing {expected - actual:,} 条（真实停牌或未上市，不填补）")
    event(dates[0], 4, f"数据源状态：{len(symbols)} 只标的行情采集完成，失败 {len(failed)} 只；"
                       f"东财主源 + 新浪备用源，单源最多 {MAX_RETRY} 次指数退避重试（base={RETRY_BASE}s）")
    if zero_vol:
        event(dates[0], 1, f"行情质检：发现 {zero_vol} 条 volume=0 记录，按停牌处理")
    if failed:
        event(dates[0], 2, f"数据源异常：{len(failed)} 只标的所有免费源均失败，已剔除：{', '.join(failed)}")
    event(dates[0], 1, "数据局限：免费源可能缺失退市股票；以『沪深300 当前成分』回溯历史存在幸存者偏差")

    log("[4/5] 事件驱动回测（真实行情，严格 A 股规则）...")
    bt = run_backtest(price_map, codes, dates, index_close)
    strategy = build_strategy(bt)
    forecast = build_forecast(bt["equity"])
    log(f"      策略状态 {len(strategy['signal_state'])} 天 · "
        f"5 日情景推演 {sum(1 for x in forecast['data'] if x)} 个检查点 / "
        f"{forecast['paths']} 条路径")

    log("[4.6/5] 宏观政策与外围市场传导层（真实数据，不改回测绩效）...")
    gseries = macro_mod.fetch_global_series(
        CACHE / "global", args.offline, START,
        on_event=lambda text: event(dates[-1], 4, text),
    )
    macro = macro_mod.build_macro(gseries, dates, index_close, bt["equity"])
    log(f"      外围市场 {macro['available_count']}/{len(macro['latest'])} 可用 · "
        f"真实政策/事件 {len(macro['events'])} 条 · "
        f"外围承压情景 {sum(1 for x in macro['conditional']['data'] if x)} 个检查点")

    waiting = sum(1 for o in bt["orders"] if o["status"] == STATUS["pending"])
    if waiting:
        event(dates[-1], 0, f"最新收盘已生成 {waiting} 笔 pending 订单，等待 T+1 开盘（waiting_for_next_bar）；本地库尚无下一交易日行情")
    event(dates[-1], 0, f"全区间固定参数回测完成：{len(dates)} 交易日 / {len(bt['orders'])} 笔订单 / {bt['summary']['closed_trades']} 笔平仓")

    # 采集期真实事件（限流、退避重试、降级、数据源状态）持久化，
    # 保证 --offline 从缓存复现时异常场景不丢失，而不是只留下"漂亮"的成交记录。
    fetch_log = CACHE / "fetch_events.json"
    stored: list[list] = []
    if fetch_log.exists():
        try:
            stored = json.loads(fetch_log.read_text(encoding="utf-8"))
        except Exception:
            stored = []
    known = {e[2] for e in stored}
    dset = set(dates)
    for d, lv, txt in RAW_EVENTS:
        if d not in dset and txt not in known:
            stored.append([d, lv, txt])
            known.add(txt)
    fetch_log.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    raw_all = list(RAW_EVENTS)
    have = {e[2] for e in raw_all}
    for d, lv, txt in stored:
        if txt not in have:
            raw_all.append((d, lv, txt))
            have.add(txt)
    # 真实政策 / 外围市场事件进入运行事件流（异常筛选可见），不参与回测信号
    for e in macro["events"]:
        if e["trade_date"] in dset:
            txt = f"{e['tag']}｜{e['title']}（{e['source']}）"
            if txt not in have:
                raw_all.append((e["trade_date"], 1 if e["tone"] == "risk" else 0, txt))
                have.add(txt)
    ev_idx = {d: i for i, d in enumerate(dates)}
    # 采集期事件（带时间戳、不在交易日历内）归到最新交易日，符合"今天采集"的真实时间。
    events = sorted(
        [[ev_idx.get(d, len(dates) - 1), lv, txt] for d, lv, txt in raw_all],
        key=lambda r: r[0],
    )

    snap = {
        "meta": {
            "generated_at": today,
            "start": dates[0], "end": end, "days": len(dates),
            "symbols": len(symbols), "bars": actual,
            "initial_cash": INITIAL_CASH,
            "source": "AKShare / 东方财富真实日线 + 新浪沪深300真实指数",
            "universe_source": uni_source,
            "index_source": index_source,
            "mode": "real" if not args.offline else "cached_real",
            "oos_ratio": OOS_RATIO,
            "cfg": {k: v for k, v in DEFAULTS.items()},
            "audit": {"expected": expected, "actual": actual,
                      "missing": expected - actual, "adj_missing": adj_missing,
                      "zero_volume": zero_vol, "failed_symbols": failed},
            "sources": sources,
        },
        "symbols": symbols,
        "dates": dates,
        "equity": bt["equity"],
        "positions": bt["positions"],
        "orders": bt["orders"],
        "events": events,
        "metrics": bt["metrics"],
        "split_idx": bt["split_idx"],
        "summary": bt["summary"],
        "strategy": strategy,
        "forecast": forecast,
        "macro": macro,
        "notes": [
            "行情为免费公开接口采集的真实 A 股不复权日线，未做任何随机生成或缺失填补。",
            "前复权价由真实涨跌幅连乘构造，仅用于双均线信号；成交价、涨跌停与滑点一律用不复权价。",
            "首版仅支持沪深主板 ±10% 涨跌停近似；创业板/科创板/北交所/ST 标的已从股票池剔除。",
            "信号在 T 日收盘后生成，最早按 T+1 开盘价成交；停牌最多顺延 3 个交易日。",
            "固定参数跑全程后按最后 20% 切分样本外，不存在两段式参数泄漏。",
            "「后续 5 日」为基于真实历史收益分布的蒙特卡洛情景推演，不是行情数据、不参与回测、不构成投资建议。",
            "政策与美日韩市场层使用真实免费日线与公开报道要点（附来源），异常值如实剔除、不填补；"
            "仅用于观察、传导统计与「外围承压」条件情景抽样，未进入回测信号，历史绩效未被改写。",
            "未接入实盘、未使用付费 API、miniQMT 不在首版范围。",
        ],
    }

    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    log(f"[5/5] 快照 {SNAPSHOT.stat().st_size / 1024:.0f} KB · "
        f"订单 {len(bt['orders'])} · 事件 {len(events)} · "
        f"净值 {bt['metrics']['full']['final_nav']} · 耗时 {time.perf_counter() - t0:.1f}s")

    if TEMPLATE.exists():
        payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
        html = TEMPLATE.read_text(encoding="utf-8").replace(
            "/*__SNAPSHOT__*/", "const SNAP = " + payload + ";")
        OUTPUT.write_text(html, encoding="utf-8")
        log(f"      已生成自包含页面 {OUTPUT}（{OUTPUT.stat().st_size / 1024:.0f} KB，可离线双击预览）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
