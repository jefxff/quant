"""QUANT-UI：本地 Web 操作台（零新增依赖，仅标准库）。

设计要点
--------
* 只监听 ``127.0.0.1``，并校验 Host，避免 DNS rebinding / 跨站调用。
* **命令白名单**：前端只能传 ``action`` id + 受校验的参数，服务端据此构造 argv 列表，
  使用 ``subprocess.run(..., shell=False)`` 执行，绝不拼接 shell 字符串（R-004/R-007 的
  "不联网、不接实盘" 约束同样适用于本层：回测/模拟盘命令本身只读本地 SQLite）。
* 同一时刻只允许一条命令运行（SQLite 单写者），并发请求直接返回 409，不排队堆积。
* 静态资源与报告文件都做路径白名单 + ``relative_to`` 目录逃逸校验。

提供的接口::

    GET  /                 操作台页面
    GET  /app.css         样式
    GET  /app.js          脚本
    GET  /api/meta        项目信息、命令清单、默认参数、报告列表
    GET  /api/reports     报告列表
    GET  /api/report      ?run_id=... 返回 Markdown / meta / 文件清单
    GET  /assets/<run>/<file>   报告目录内文件（白名单扩展名）
    POST /api/run         {"action": "...", "params": {...}} → 运行回执
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parent / "web"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUNID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_DBPATH_RE = re.compile(r"^[A-Za-z0-9_./\\:()\- ]{1,240}$")
_ASSET_EXT = {".md", ".csv", ".png", ".json"}

# 联网动作必须显式标注，前端会给出提示；fixture / 回测 / 模拟盘 / 报告 / pytest 全部离线。
ACTIONS: dict[str, dict[str, Any]] = {}


@lru_cache(maxsize=1)
def fixture_range() -> tuple[str, str]:
    """离线演示数据覆盖的真实区间，从交易日历推导（不硬编码日期）。

    R-005 禁止伪造 / 填补行情：演示数据只有这段，用户填其他年份时不能凭空造数据，
    操作台会把 fixture 动作钳制到本区间并明确告知，而不是静默返回空表。
    """
    path = PROJECT_ROOT / "tests" / "fixtures" / "trade_calendar.csv"
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            days = sorted({
                row["date"] for row in csv.DictReader(fh)
                if str(row.get("is_open", "")).strip() in ("1", "True", "true")
            })
        if days:
            return days[0], days[-1]
    except (OSError, KeyError, ValueError):
        pass
    return "2022-01-04", "2022-12-30"


def _action(action_id: str, label: str, group: str, desc: str,
            build: Callable[[dict[str, Any]], list[str]],
            *, network: bool = False, timeout: int = 300) -> None:
    ACTIONS[action_id] = {
        "id": action_id, "label": label, "group": group, "desc": desc,
        "network": network, "timeout": timeout, "build": build,
    }


# ---------------------------------------------------------------- 参数校验

def _date(params: dict[str, Any], key: str, default: str) -> str:
    value = str(params.get(key) or default).strip()
    if not _DATE_RE.match(value):
        raise ValueError(f"{key} 必须是 YYYY-MM-DD，收到：{value!r}")
    return value


def _int(params: dict[str, Any], key: str, default: int,
         low: int = 0, high: int = 5000) -> int:
    raw = params.get(key)
    try:
        value = int(default if raw in (None, "") else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数，收到：{raw!r}") from exc
    if not low <= value <= high:
        raise ValueError(f"{key} 必须在 {low}~{high} 之间")
    return value


def _ident(params: dict[str, Any], key: str, default: str,
           allowed: set[str] | None = None) -> str:
    value = str(params.get(key) or default).strip()
    if not _IDENT_RE.match(value) and not (allowed and value in allowed):
        raise ValueError(f"{key} 含非法字符：{value!r}")
    if allowed and value not in allowed:
        raise ValueError(f"{key} 必须是 {sorted(allowed)} 之一，收到：{value!r}")
    return value


def _db(params: dict[str, Any]) -> str:
    """数据库路径：只允许普通路径字符，禁止前导 '-'（参数注入）与 '..'（目录逃逸）。"""
    value = str(params.get("db") or "").strip()
    if not value:
        return ""
    if (not _DBPATH_RE.match(value) or ".." in value or value.startswith("-")
            or not value.lower().endswith((".db", ".sqlite", ".sqlite3"))):
        raise ValueError(f"db 路径非法（需为 .db/.sqlite/.sqlite3 且不含 ..）：{value!r}")
    return value


def _run_id(value: Any) -> str:
    text = str(value or "latest").strip()
    if text != "latest" and (not _RUNID_RE.match(text) or ".." in text):
        raise ValueError(f"run_id 非法：{text!r}")
    return text


def _date_or_latest(params: dict[str, Any], key: str) -> str:
    text = str(params.get(key) or "latest").strip()
    if text == "latest":
        return text
    if not _DATE_RE.match(text):
        raise ValueError(f"{key} 必须是 latest 或 YYYY-MM-DD，收到：{text!r}")
    return text


def _prefix(params: dict[str, Any]) -> list[str]:
    head = [sys.executable, "-m", "quant"]
    db = _db(params)
    return head + (["--db", db] if db else [])


def _update(params: dict[str, Any], source: str) -> list[str]:
    argv = _prefix(params) + ["data", "update", "--source", source]
    argv += ["--pool", _ident(params, "pool", "hs300")]
    argv += ["--limit", str(_int(params, "limit", 5, 0, 5000))]
    if source == "fixture":
        # 演示数据只有 2022 年；钳制到真实覆盖区间，绝不按用户请求年份伪造行情。
        start, end = fixture_range()
    else:
        start = _date(params, "start", fixture_range()[0])
        end = _date(params, "end", fixture_range()[1])
    argv += ["--start", start, "--end", end]
    return argv


# ---------------------------------------------------------------- 命令白名单
_action("data.update.fixture", "灌入离线数据", "数据",
        "载入 tests/fixtures 演示行情；仅覆盖离线演示区间，其他年份请先联网更新",
        lambda p: _update(p, "fixture"), timeout=180)
_action("data.update.baostock", "更新 · Baostock", "数据",
        "匿名免费源拉取沪深日线主源；失败自动退避重试并降级",
        lambda p: _update(p, "baostock"), network=True, timeout=900)
_action("data.update.akshare", "更新 · AKShare", "数据",
        "免费源补充行业分类与备用行情；失败自动退避重试并降级",
        lambda p: _update(p, "akshare"), network=True, timeout=900)
_action("data.status", "数据审计", "数据",
        "查看 expected_days / actual_days / missing_days 与板块分布",
        lambda p: _prefix(p) + ["data", "status", "--pool", _ident(p, "pool", "hs300")],
        timeout=120)

_action("backtest", "运行回测", "回测",
        "事件驱动回测：T 日信号 → T+1 开盘成交，只读本地库，自动生成报告",
        lambda p: _prefix(p) + [
            "backtest",
            "--strategy", _ident(p, "strategy", "dual_ma"),
            "--start", _date(p, "start", "2022-01-04"),
            "--end", _date(p, "end", "2022-12-30"),
            "--limit", str(_int(p, "limit", 5, 1, 5000)),
            "--pool", _ident(p, "pool", "hs300"),
        ], timeout=600)


def _paper_run(params: dict[str, Any], dry: bool) -> list[str]:
    argv = _prefix(params) + ["paper", "run", "--date", _date_or_latest(params, "date")]
    if dry:
        argv.append("--dry-run")
    argv += ["--strategy", _ident(params, "strategy", "dual_ma")]
    argv += ["--limit", str(_int(params, "limit", 5, 1, 5000))]
    return argv


_action("paper.run.dry", "模拟盘预演", "模拟盘",
        "走完整流程但回滚事务，不写库（--dry-run）",
        lambda p: _paper_run(p, True), timeout=300)
_action("paper.run", "模拟盘运行", "模拟盘",
        "先结算 ≤date 的 pending，再基于 date 生成新 pending；同一事务、幂等",
        lambda p: _paper_run(p, False), timeout=300)
_action("paper.status", "订单 / 持仓状态", "模拟盘",
        "查看订单状态机计数、持仓与最新权益",
        lambda p: _prefix(p) + ["paper", "status"], timeout=120)

_action("report", "重新渲染报告", "报告",
        "按 run_id 重新渲染 Markdown / CSV / PNG 与程序化假设章节",
        lambda p: _prefix(p) + ["report", "--run-id", _run_id(p.get("run_id"))],
        timeout=180)
_action("cli.help", "CLI 帮助", "系统",
        "查看 python -m quant 全部子命令",
        lambda p: [sys.executable, "-m", "quant", "--help"], timeout=60)
_action("pytest", "离线测试套件", "系统",
        "运行 pytest（默认跳过 network 标记，全程离线）",
        lambda p: [sys.executable, "-m", "pytest", "-q"], timeout=600)

# 一键闭环由前端按此顺序编排；服务端仍逐条走白名单校验。
PIPELINE: list[str] = [
    "data.update.fixture", "data.status", "backtest",
    "paper.run.dry", "paper.run", "paper.run", "report",
]


def build_argv(action: str, params: dict[str, Any] | None = None) -> list[str]:
    """白名单构造 argv。任何非法 action / 参数都抛 ValueError，绝不落到 shell。"""
    if action not in ACTIONS:
        raise ValueError(f"未知操作：{action!r}")
    return ACTIONS[action]["build"](params or {})


def display_command(argv: list[str]) -> str:
    """把 argv 渲染成可复制的命令行（仅用于展示，不参与执行）。"""
    out = []
    for token in argv:
        text = str(token)
        out.append('"' + text.replace('"', "'") + '"' if " " in text else text)
    return " ".join(out)


# ---------------------------------------------------------------- 命令执行
_RUN_LOCK = threading.Lock()
_RUNNING: dict[str, Any] = {"action": None, "since": None}


def run_command(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步执行一条白名单命令，返回结构化「运行回执」。"""
    try:
        argv = build_argv(action, params or {})
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400, "action": action}

    if not _RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有命令正在运行，请等待完成后再试",
                "status": 409, "action": action, "running": _RUNNING["action"]}

    spec = ACTIONS[action]
    note = ""
    if action == "data.update.fixture":
        demo_start, demo_end = fixture_range()
        asked = (str((params or {}).get("start") or ""), str((params or {}).get("end") or ""))
        if any(asked) and asked != (demo_start, demo_end):
            note = (f"离线演示数据仅覆盖 {demo_start} ~ {demo_end}，已按该区间执行；"
                    f"表单日期 {asked[0] or '—'} ~ {asked[1] or '—'} 未采用。"
                    "其他年份请先执行联网更新，本地库有数据后再回测。")
    _RUNNING.update({"action": action, "since": datetime.now(timezone.utc).isoformat()})
    started_at = _RUNNING["since"]
    t0 = time.perf_counter()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1", TERM="dumb", FORCE_COLOR="0")
    try:
        proc = subprocess.run(
            argv, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=spec["timeout"], env=env,
            shell=False, check=False,
        )
        return {
            "ok": proc.returncode == 0, "action": action, "label": spec["label"], "note": note,
            "cmd": display_command(argv), "argv": argv[2:],
            "exit_code": proc.returncode,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "stdout": proc.stdout or "", "stderr": proc.stderr or "",
            "started_at": started_at, "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "action": action, "label": spec["label"], "note": note,
            "cmd": display_command(argv), "argv": argv[2:],
            "exit_code": 124, "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": f"命令超时（{spec['timeout']}s）已终止",
            "started_at": started_at, "timed_out": True,
            "error": "命令执行超时",
        }
    except OSError as exc:  # pragma: no cover - 解释器缺失等极端情况
        return {"ok": False, "action": action, "label": spec["label"], "note": note,
                "cmd": display_command(argv), "exit_code": 1,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "stdout": "", "stderr": str(exc), "started_at": started_at,
                "error": f"无法启动子进程：{exc}"}
    finally:
        _RUNNING.update({"action": None, "since": None})
        _RUN_LOCK.release()


# ---------------------------------------------------------------- 项目元数据
def _config_defaults() -> dict[str, Any]:
    try:
        from .config import load_config
        cfg = load_config(None, root=PROJECT_ROOT)
        return {
            "db": cfg.get("database.path"),
            "start": cfg.get("data.start"), "end": cfg.get("data.end"),
            "limit": cfg.get("data.limit"), "pool": cfg.get("data.pool"),
            "source": cfg.get("data.source"),
            "strategy": cfg.get("backtest.strategy"),
        }
    except Exception:  # noqa: BLE001 - 配置损坏时操作台仍应可用
        return {}


def report_dir() -> Path:
    from .config import load_config
    try:
        cfg = load_config(None, root=PROJECT_ROOT)
        raw = Path(str(cfg.get("report.out_dir", "reports")))
    except Exception:  # noqa: BLE001
        raw = Path("reports")
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def _safe_report_path(run_id: str, filename: str = "") -> Path:
    """解析报告路径并阻止目录穿越；不合法抛 ValueError。"""
    rid = _run_id(run_id)
    if rid == "latest":
        pointer = report_dir() / "latest"
        if pointer.exists():
            rid = _run_id(pointer.read_text(encoding="utf-8").strip())
    if rid == "latest" or not _RUNID_RE.match(rid) or ".." in rid:
        raise ValueError("无法解析报告 run_id")
    base = report_dir().resolve()
    target = (base / rid / filename).resolve() if filename else (base / rid).resolve()
    target.relative_to(base)  # 目录逃逸直接 ValueError
    return target


def list_reports() -> list[dict[str, Any]]:
    base = report_dir()
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir() or not _RUNID_RE.match(entry.name) or ".." in entry.name:
            continue
        meta: dict[str, Any] = {}
        meta_file = entry / "run_meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        rows.append({
            "run_id": entry.name,
            "kind": str(meta.get("kind", "unknown")),
            "strategy": str(meta.get("strategy", "")),
            "mtime": datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc).isoformat(),
            "files": sorted(f.name for f in entry.iterdir()
                            if f.is_file() and f.suffix.lower() in _ASSET_EXT),
        })
    return rows[:40]


def read_report(run_id: str) -> dict[str, Any]:
    folder = _safe_report_path(run_id)
    if not folder.is_dir():
        raise ValueError(f"报告不存在：{run_id}")

    def text(name: str) -> str:
        path = folder / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def obj(name: str) -> Any:
        raw = text(name)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    md = text("report.md")
    return {
        "ok": True, "run_id": folder.name, "markdown": md,
        "meta": obj("run_meta.json"), "assumptions": obj("assumptions.json"),
        "files": [
            {"name": f.name, "size": f.stat().st_size,
             "url": f"/assets/{folder.name}/{f.name}"}
            for f in sorted(folder.iterdir())
            if f.is_file() and f.suffix.lower() in _ASSET_EXT
        ],
    }


def project_meta() -> dict[str, Any]:
    cfg = _config_defaults()
    return {
        "ok": True,
        "project": "QUANT-001 · 零付费 API / 离线优先的 A 股日频量化系统",
        "project_root": str(PROJECT_ROOT),
        "python": sys.version.split()[0],
        "report_dir": str(report_dir()),
        "running": _RUNNING["action"],
        "actions": [
            {k: v for k, v in spec.items() if k != "build"} for spec in ACTIONS.values()
        ],
        "pipeline": PIPELINE,
        # 前端默认用离线演示区间，开箱即用、无需网络。
        "demo_range": {"start": fixture_range()[0], "end": fixture_range()[1]},
        "defaults": {
            "db": str(cfg.get("db") or "data/quant.db"),
            "start": fixture_range()[0], "end": fixture_range()[1],
            "limit": 5, "pool": "hs300", "strategy": "dual_ma",
            "source": "fixture", "date": "latest", "run_id": "latest",
        },
        "config_defaults": cfg,
        "notes": [
            "未接入实盘、未使用付费 API、miniQMT 不在首版范围",
            "回测与模拟盘只读本地 SQLite，循环内绝不联网",
            "测试默认跳过 network 标记，全部离线可跑",
        ],
    }


# ---------------------------------------------------------------- HTTP 层
_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


class QuantHandler(BaseHTTPRequestHandler):
    server_version = "QuantUI/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    # -- 基础响应 ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # 用户提前关闭页面
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"ok": False, "error": "仅允许本机访问"}, 403)
            return False
        return True

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html", "/app.css", "/app.js"):
                name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
                target = WEB_DIR / name
                self._send(200, target.read_bytes(), _CONTENT_TYPES[name])
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path == "/api/meta":
                self._json(project_meta())
                return
            if path == "/api/reports":
                self._json({"ok": True, "reports": list_reports()})
                return
            if path == "/api/report":
                self._json(read_report(_run_id((query.get("run_id") or ["latest"])[0])))
                return
            if path.startswith("/assets/"):
                parts = path[len("/assets/"):].split("/")
                if len(parts) != 2 or Path(parts[1]).suffix.lower() not in _ASSET_EXT:
                    self._json({"ok": False, "error": "非法资源路径"}, 400)
                    return
                target = _safe_report_path(parts[0], parts[1])
                if not target.is_file():
                    self._json({"ok": False, "error": "资源不存在"}, 404)
                    return
                suffix = target.suffix.lower()
                ctype = {".png": "image/png", ".md": "text/markdown; charset=utf-8",
                         ".csv": "text/csv; charset=utf-8",
                         ".json": "application/json; charset=utf-8"}[suffix]
                self._send(200, target.read_bytes(), ctype)
                return
            self._json({"ok": False, "error": f"未知路径：{path}"}, 404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except OSError as exc:
            self._json({"ok": False, "error": f"读取失败：{exc}"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlparse(unquote(self.path)).path
        if path != "/api/run":
            self._json({"ok": False, "error": "未知接口"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求体为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            action = str(payload.get("action") or "")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params 必须是对象")
            result = run_command(action, params)
            code = int(result.pop("status", 200))
            self._json(result, code)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": f"请求无效：{exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = False) -> int:
    """启动操作台；端口被占用时向后顺延最多 12 个端口。Ctrl+C 退出。"""
    httpd: _Server | None = None
    last_error: OSError | None = None
    for candidate in range(int(port), int(port) + 12):
        try:
            httpd = _Server((host, candidate), QuantHandler)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        print(f"无法启动操作台（端口 {port}~{int(port) + 11} 均不可用）：{last_error}")
        return 1
    url = f"http://{host}:{httpd.server_port}/"
    print(f"QUANT 操作台已启动：{url}")
    print(f"项目根目录：{PROJECT_ROOT}")
    print("按 Ctrl+C 停止；页面仅本机可访问，不对外开放。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n操作台已停止。")
    finally:
        httpd.server_close()
    return 0
