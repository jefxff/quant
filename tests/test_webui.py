"""本地 Web 操作台的离线测试：命令白名单、参数校验、HTTP 端点、报告目录逃逸防护。

全程不访问网络；子进程命令只用临时 SQLite，不改动项目运行期数据库。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from quant import cli, webui

# 不走系统代理，确保 localhost 请求直达测试服务器
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(scope="module")
def server():
    httpd = webui._Server(("127.0.0.1", 0), webui.QuantHandler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def call(base: str, method: str, path: str, payload=None, host: str | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def as_json(body: bytes):
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- 命令白名单
def test_every_action_builds_a_valid_argv():
    for action in webui.ACTIONS:
        argv = webui.build_argv(action, {"db": "", "start": "2022-01-04",
                                         "end": "2022-12-30", "limit": 5})
        assert isinstance(argv, list) and argv[0] == sys.executable
        assert argv[1] == "-m" and argv[2] in ("quant", "pytest")
        # 命令以 argv 列表执行，绝不拼接 shell 字符串
        assert not any(tok in argv for tok in (";", "&&", "|", "`"))


def test_pipeline_actions_are_all_whitelisted():
    assert len(webui.PIPELINE) == 7
    assert all(a in webui.ACTIONS for a in webui.PIPELINE)
    assert webui.PIPELINE.count("paper.run") == 2  # 第二次用于验证幂等


def test_unknown_action_rejected():
    with pytest.raises(ValueError, match="未知操作"):
        webui.build_argv("rm -rf /", {})
    result = webui.run_command("data.update --source wind", {})
    assert result["ok"] is False and result["status"] == 400


@pytest.mark.parametrize("params,message", [
    ({"limit": "5; echo hacked"}, "limit 必须是整数"),
    ({"limit": 99999}, "limit 必须在"),
    ({"start": "2022/01/04"}, "start 必须是"),
    ({"end": "tomorrow"}, "end 必须是"),
    ({"pool": "hs300' OR 1=1"}, "pool 含非法字符"),
    ({"db": "../../etc/passwd"}, "db 路径非法"),
    ({"db": "--config=/etc/passwd"}, "db 路径非法"),
    ({"db": "C:/tmp/x.txt"}, "db 路径非法"),
])
def test_parameter_validation_blocks_injection(params, message):
    # 用 backtest 校验：fixture 动作会钳制日期到演示区间，不消费用户传的 start/end。
    with pytest.raises(ValueError, match=message):
        webui.build_argv("backtest", params)


def test_report_run_id_rejects_traversal():
    with pytest.raises(ValueError, match="run_id 非法"):
        webui.build_argv("report", {"run_id": "../config.yaml"})
    assert webui.build_argv("report", {"run_id": "latest"})[-1] == "latest"


def test_fixture_command_contains_expected_flags():
    argv = webui.build_argv("data.update.fixture", {
        "db": "data/quant.db", "pool": "hs300", "limit": 5,
        "start": "2022-01-04", "end": "2022-12-30",
    })
    assert argv[3:5] == ["--db", "data/quant.db"]
    assert argv[5:8] == ["data", "update", "--source"]
    assert argv[8] == "fixture"
    assert "--limit" in argv and "--start" in argv and "--end" in argv
    assert "5" in argv and "2022-01-04" in argv


def test_fixture_range_is_derived_from_calendar():
    assert webui.fixture_range() == ("2022-01-04", "2022-12-30")


def test_fixture_action_clamps_out_of_range_dates():
    """用户把日期改成 2026 时，离线灌数据仍用演示区间，绝不伪造其他年份行情。"""
    argv = webui.build_argv("data.update.fixture", {
        "start": "2026-01-01", "end": "2026-12-30", "limit": 5,
    })
    assert "2022-01-04" in argv and "2022-12-30" in argv
    assert not any("2026" in str(tok) for tok in argv)
    # 回测/联网动作不受钳制：真实数据场景下日期由用户决定，由前端闭环传演示区间。
    bt = webui.build_argv("backtest", {"start": "2026-01-01", "end": "2026-12-30"})
    assert "2026-01-01" in bt and "2026-12-30" in bt


def test_run_command_fixture_with_wrong_year_returns_note(tmp_path: Path):
    """复现用户报告的第一步失败：2026 年参数下 fixture 更新应成功并给出说明。"""
    result = webui.run_command("data.update.fixture", {
        "db": str(tmp_path / "demo.db"), "pool": "hs300", "limit": 5,
        "start": "2026-01-01", "end": "2026-12-30",
    })
    assert result["ok"] is True and result["exit_code"] == 0
    assert "--start 2022-01-04" in result["cmd"] and "--end 2022-12-30" in result["cmd"]
    assert "离线演示数据仅覆盖" in result["note"] and "2026-01-01" in result["note"]
    assert '"rows_written"' in result["stdout"]


def test_display_command_quotes_paths_with_spaces():
    rendered = webui.display_command(["python", "-m", "quant", "--db", "C:/my data/q.db"])
    assert '"C:/my data/q.db"' in rendered


# ---------------------------------------------------------------- 命令执行
def test_run_command_returns_structured_receipt(tmp_path: Path):
    db = tmp_path / "web.db"
    result = webui.run_command("data.status", {"db": str(db), "pool": "hs300"})
    assert result["ok"] is True and result["exit_code"] == 0
    assert result["action"] == "data.status" and result["label"]
    assert "--db" in result["cmd"] and "data status" in result["cmd"]
    assert result["elapsed_ms"] >= 0 and result["started_at"]
    assert '"schema_version"' in result["stdout"]
    assert db.exists()  # 只读审计也会确保 schema 就绪


def test_run_command_reports_nonzero_exit(tmp_path: Path):
    """空库回测应返回退出码 2（数据缺失），且响应里保留原始错误文本。"""
    result = webui.run_command("backtest", {
        "db": str(tmp_path / "empty.db"), "start": "2022-01-04",
        "end": "2022-12-30", "limit": 5, "strategy": "dual_ma", "pool": "hs300",
    })
    assert result["ok"] is False and result["exit_code"] == 2
    assert "股票池为空" in result["stdout"]


# ---------------------------------------------------------------- HTTP 端点
def test_static_assets_and_meta(server):
    code, body, ctype = call(server, "GET", "/")
    assert code == 200 and "text/html" in ctype
    page = body.decode("utf-8")
    assert "QUANT 操作台" in page and "运行回执" in page and "离线闭环" in page
    for path, needle in (("/app.css", "--font-sans"), ("/app.js", "runPipeline")):
        code, body, _ = call(server, "GET", path)
        assert code == 200 and needle.encode() in body
    code, body, _ = call(server, "GET", "/api/meta")
    meta = as_json(body)
    assert code == 200 and meta["ok"] is True
    assert len(meta["actions"]) == len(webui.ACTIONS)
    assert meta["pipeline"] == webui.PIPELINE
    assert meta["defaults"]["start"] == "2022-01-04"
    assert meta["demo_range"] == {"start": "2022-01-04", "end": "2022-12-30"}
    assert not any(a["network"] for a in meta["actions"] if a["id"] == "backtest")


def test_api_run_over_http(server, tmp_path: Path):
    code, body, _ = call(server, "POST", "/api/run", {
        "action": "data.status",
        "params": {"db": str(tmp_path / "http.db"), "pool": "hs300"},
    })
    result = as_json(body)
    assert code == 200 and result["ok"] is True and result["exit_code"] == 0
    assert "status" not in result  # 内部字段不暴露给前端


def test_api_run_rejects_bad_payload(server):
    code, body, _ = call(server, "POST", "/api/run", {"action": "not-a-command"})
    assert code == 400 and as_json(body)["ok"] is False
    code, body, _ = call(server, "POST", "/api/run", {"action": "backtest", "params": "bad"})
    assert code == 400
    code, _, _ = call(server, "POST", "/api/nope", {})
    assert code == 404


def test_host_header_guard(server):
    code, body, _ = call(server, "GET", "/api/meta", host="evil.example.com")
    assert code == 403 and "仅允许本机" in as_json(body)["error"]


# ---------------------------------------------------------------- 报告读取
@pytest.fixture
def fake_reports(tmp_path: Path, monkeypatch):
    root = tmp_path / "reports"
    run = root / "dual_ma_2022-01-04_2022-12-30_123"
    run.mkdir(parents=True)
    (run / "report.md").write_text("# 回测报告\n\n## 假设与限制\n- 内容", encoding="utf-8")
    (run / "equity.csv").write_text("date,equity\n2022-01-04,1000000\n", encoding="utf-8")
    (run / "run_meta.json").write_text(json.dumps({"kind": "backtest", "strategy": "dual_ma"}),
                                       encoding="utf-8")
    (root / "latest").write_text(run.name, encoding="utf-8")
    monkeypatch.setattr(webui, "report_dir", lambda: root)
    return root, run


def test_report_endpoints(fake_reports, server):
    _root, run = fake_reports
    code, body, _ = call(server, "GET", "/api/reports")
    reports = as_json(body)["reports"]
    assert code == 200 and len(reports) == 1 and reports[0]["kind"] == "backtest"
    assert "report.md" in reports[0]["files"]

    code, body, _ = call(server, "GET", "/api/report?run_id=latest")
    rep = as_json(body)
    assert code == 200 and rep["run_id"] == run.name
    assert "假设与限制" in rep["markdown"] and rep["meta"]["kind"] == "backtest"
    assert any(f["name"] == "equity.csv" for f in rep["files"])

    code, body, ctype = call(server, "GET", f"/assets/{run.name}/report.md")
    assert code == 200 and "text/markdown" in ctype and b"\xe5\x9b\x9e\xe6\xb5\x8b" in body


def test_asset_path_traversal_blocked(fake_reports, server):
    for path in ("/assets/../config.yaml", "/assets/..%2f..%2fconfig.yaml",
                 "/assets/run/../../config.yaml", "/assets/run/report.md/../x"):
        code, body, _ = call(server, "GET", path)
        assert code in (400, 404), path
        assert as_json(body)["ok"] is False


def test_safe_report_path_rejects_escape(fake_reports):
    root, run = fake_reports
    with pytest.raises(ValueError):
        webui._safe_report_path("../config.yaml")
    with pytest.raises(ValueError):
        webui._safe_report_path(run.name, "../../config.yaml")
    assert webui._safe_report_path("latest").name == run.name


# ---------------------------------------------------------------- CLI 集成
def test_cli_ui_help_documents_port_and_binding(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["ui", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for token in ("--port", "--host", "--open", "127.0.0.1"):
        assert token in text


def test_top_level_help_still_lists_original_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    assert "ui" in text and all(t in text for t in ("data", "backtest", "paper", "report"))
