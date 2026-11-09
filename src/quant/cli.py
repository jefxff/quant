"""``python -m quant ...`` 命令行入口。

退出码：0 成功 / 2 数据缺失 / 3 网络失败 / 4 校验失败 / 1 其他错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .errors import QuantError

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from rich.console import Console
    _console = Console()
except ImportError:  # pragma: no cover
    _console = None


def _out(text: str) -> None:
    if _console is not None:
        _console.print(text, soft_wrap=True)
    else:
        import re as _re
        print(_re.sub(r"\[/?(?:bold|green|red)(?:\s+[^\]]*)?\]", "", text))


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quant",
        description="QUANT-001 零付费 API / 离线优先的 A 股日频量化研究与模拟盘系统",
    )
    parser.add_argument("--config", default=None, help="config.yaml 路径（默认项目根/config.yaml）")
    parser.add_argument("--db", default=None, help="覆盖 database.path（CLI 优先级最高）")
    parser.add_argument("--verbose", "-v", action="store_true")
    # 允许全局选项出现在子命令之后（argparse 的 SUPPRESS 技巧）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--db", default=argparse.SUPPRESS)
    common.add_argument("--verbose", "-v", action="store_true", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- data ----
    data = sub.add_parser("data", help="数据更新与状态", parents=[common])
    dsub = data.add_subparsers(dest="data_command", required=True)
    upd = dsub.add_parser("update", help="拉取行情并落地本地 SQLite", parents=[common])
    upd.add_argument("--source", default=None, choices=["baostock", "akshare", "fixture"])
    upd.add_argument("--pool", default=None)
    upd.add_argument("--limit", type=int, default=None, help="按股票池顺序取前 N（非随机）")
    upd.add_argument("--start", default=None)
    upd.add_argument("--end", default=None)
    upd.add_argument("--fixture-dir", default=None, help="离线 fixture 目录")
    st = dsub.add_parser("status", help="本地缓存审计", parents=[common])
    st.add_argument("--pool", default="hs300")

    # ---- backtest ----
    bt = sub.add_parser("backtest", help="事件驱动回测（只读本地库，绝不联网）", parents=[common])
    bt.add_argument("--strategy", default=None)
    bt.add_argument("--start", default=None)
    bt.add_argument("--end", default=None)
    bt.add_argument("--limit", type=int, default=None)
    bt.add_argument("--pool", default=None)

    # ---- paper ----
    paper = sub.add_parser("paper", help="每日模拟盘（只写本地 SQLite）", parents=[common])
    psub = paper.add_subparsers(dest="paper_command", required=True)
    run = psub.add_parser("run", help="先结算 <=date 的 pending，再基于 date 生成新 pending",
                          parents=[common])
    run.add_argument("--date", default="latest", help="YYYY-MM-DD 或 latest(=库中最大交易日)")
    run.add_argument("--dry-run", action="store_true", help="完整流程但 rollback，不写库")
    run.add_argument("--strategy", default=None)
    run.add_argument("--limit", type=int, default=None, help="股票池前 N（非随机）")
    psub.add_parser("status", help="查看模拟盘状态", parents=[common])

    # ---- report ----
    rep = sub.add_parser("report", help="读取/重新渲染报告", parents=[common])
    rep.add_argument("--run-id", default="latest")

    # ---- ui ----
    ui = sub.add_parser("ui", help="启动本地 Web 操作台（只监听 127.0.0.1）",
                        parents=[common])
    ui.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    ui.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    ui.add_argument("--open", action="store_true", dest="open_browser",
                    help="启动后自动打开浏览器")

    return parser


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "db", None):
        overrides["database.path"] = args.db
    for attr, dotted in (
        ("source", "data.source"), ("pool", "data.pool"), ("limit", "data.limit"),
        ("start", "data.start"), ("end", "data.end"), ("strategy", "backtest.strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[dotted] = value
    return overrides


def _load(args: argparse.Namespace):
    overrides = _overrides_from_args(args)
    if getattr(args, "strategy", None) and getattr(args, "command", None) == "paper":
        overrides["paper.strategy"] = args.strategy
    cfg = load_config(args.config, overrides=overrides, root=PROJECT_ROOT)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return cfg


def cmd_data_update(args: argparse.Namespace) -> int:
    from .data.store import Store
    from .data.update import update_store

    cfg = _load(args)
    store = Store(cfg.path("database.path"))
    try:
        result = update_store(
            cfg, store,
            source=getattr(args, "source", None),
            pool=getattr(args, "pool", None),
            limit=getattr(args, "limit", None),
            start=getattr(args, "start", None),
            end=getattr(args, "end", None),
            fixture_dir=getattr(args, "fixture_dir", None),
        )
    finally:
        store.close()
    _out("[bold green]data update 完成[/bold green]" if _console else "data update 完成")
    _out(_json({
        "source": result.source, "symbols": len(result.symbols),
        "rows_written": result.rows_written,
        "universe_rows": result.universe_rows,
        "industry_rows": result.industry_rows,
        "calendar": result.calendar,
        "audit": {k: v for k, v in result.audit.items() if k != "per_symbol"},
        "warnings": result.warnings,
    }))
    return 0


def cmd_data_status(args: argparse.Namespace) -> int:
    from .data.store import Store
    from .data.update import data_status

    cfg = _load(args)
    store = Store(cfg.path("database.path"))
    try:
        _out(_json(data_status(store, args.pool)))
    finally:
        store.close()
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest.engine import BacktestEngine
    from .data.store import Store
    from .report.render import make_run_id, render_backtest

    cfg = _load(args)
    store = Store(cfg.path("database.path"))
    try:
        start = str(args.start or cfg.get("data.start"))
        end = str(args.end or cfg.get("data.end"))
        limit = int(args.limit if args.limit is not None else cfg.get("data.limit", 30))
        pool = str(args.pool or cfg.get("data.pool", "hs300"))
        strategy = str(args.strategy or cfg.get("backtest.strategy", "dual_ma"))
        symbols = store.get_universe(pool, limit=limit if limit > 0 else None,
                                     main_only=True)["symbol"].tolist()
        if not symbols:
            _out("股票池为空，请先执行 data update（R-014 首版只回测 MAIN 标的）")
            return 2
        run_id = make_run_id(strategy, start, end)
        engine = BacktestEngine(cfg, store, strategy_name=strategy, run_id=run_id)
        result = engine.run(start, end, symbols)
        info = render_backtest(result, cfg, store)
        full = info["metrics"]["full"]
        _out("[bold green]回测完成[/bold green]" if _console else "回测完成")
        _out(_json({
            "run_id": run_id, "report_md": info["report_md"], "png": info["png"],
            "trading_days": len(result.trading_days), "symbols": len(symbols),
            "oos_start": result.oos_start, "trades": int(full["trades"]),
            "cum_return": full["cum_return"], "max_drawdown": full["max_drawdown"],
            "sharpe": full["sharpe"], "cost_ratio": full["cost_ratio"],
            "missing_days": result.audit["missing_days"],
            "warnings": result.warnings[:10],
        }))
        return 0
    finally:
        store.close()


def cmd_paper_run(args: argparse.Namespace) -> int:
    from .data.store import Store
    from .paper.engine import PaperEngine
    from .report.render import render_paper

    cfg = _load(args)
    store = Store(cfg.path("database.path"))
    try:
        engine = PaperEngine(cfg, store, strategy_name=getattr(args, "strategy", None))
        result = engine.run(args.date, dry_run=bool(args.dry_run))
        if not args.dry_run:
            info = render_paper(result, cfg, store)
            _out(f"报告: {info['report_md']}")
        _out("[bold green]paper run 完成[/bold green]" if _console else "paper run 完成")
        _out(_json(result.as_dict()))
        return 0
    finally:
        store.close()


def cmd_paper_status(args: argparse.Namespace) -> int:
    from .data.store import Store
    from .paper.engine import PaperEngine

    cfg = _load(args)
    store = Store(cfg.path("database.path"))
    try:
        _out(_json(PaperEngine(cfg, store).status()))
    finally:
        store.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report.render import show_report

    cfg = _load(args)
    info = show_report(cfg, args.run_id)
    _out("[bold green]报告已重新渲染[/bold green]" if _console else "报告已重新渲染")
    _out(_json({
        "run_id": info["run_id"], "kind": info["kind"],
        "report_md": info["report_md"], "run_dir": info["run_dir"],
        "assumptions_count": info["assumptions_count"],
        "metrics": info["metrics"], "audit": {k: v for k, v in info["audit"].items() if k != "per_symbol"},
    }))
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """本地 Web 操作台：把 CLI 命令与响应搬到浏览器，仍只写本地 SQLite。"""
    from .webui import serve

    return serve(host=args.host, port=args.port,
                 open_browser=bool(getattr(args, "open_browser", False)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ui":
            return cmd_ui(args)
        if args.command == "data" and args.data_command == "update":
            return cmd_data_update(args)
        if args.command == "data" and args.data_command == "status":
            return cmd_data_status(args)
        if args.command == "backtest":
            return cmd_backtest(args)
        if args.command == "paper" and args.paper_command == "run":
            return cmd_paper_run(args)
        if args.command == "paper" and args.paper_command == "status":
            return cmd_paper_status(args)
        if args.command == "report":
            return cmd_report(args)
        parser.print_help()
        return 4
    except QuantError as exc:
        _out(f"[bold red]{type(exc).__name__}[/bold red] (exit={exc.exit_code}): {exc}"
             if _console else f"{type(exc).__name__} (exit={exc.exit_code}): {exc}")
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        _out(f"[bold red]运行失败[/bold red]: {type(exc).__name__}: {exc}"
             if _console else f"运行失败: {type(exc).__name__}: {exc}")
        logging.exception("unhandled error")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
