"""CLI 可用性 + 退出码约定：0 成功 / 2 数据缺失 / 3 网络失败 / 4 校验失败。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant import cli
from quant.errors import (
    ConfigValidationError,
    DataIntegrityError,
    DataUnavailableError,
    NetworkFailureError,
    SchemaError,
)

START, END = "2022-01-04", "2022-12-30"


@pytest.fixture
def temp_config(tmp_path: Path) -> tuple[Path, Path]:
    """写一份绝对路径的临时 config.yaml，避免污染项目根 reports/data。"""
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({
        "database": {"path": str(tmp_path / "cli.db")},
        "data": {"source": "fixture", "start": START, "end": END,
                 "limit": 5, "page_size": 137},
        "report": {"out_dir": str(tmp_path / "reports"), "make_png": False},
    }, ensure_ascii=False), encoding="utf-8")
    return path, tmp_path


def test_top_level_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for token in ("data", "backtest", "paper", "report", "--config", "--db"):
        assert token in text


@pytest.mark.parametrize("argv,tokens", [
    (["data", "update", "--help"], ["--source", "--pool", "--limit", "--start", "--end"]),
    (["data", "status", "--help"], ["--pool"]),
    (["backtest", "--help"], ["--strategy", "--start", "--end", "--limit"]),
    (["paper", "run", "--help"], ["--date", "--dry-run", "--strategy"]),
    (["paper", "status", "--help"], ["usage"]),
    (["report", "--help"], ["--run-id"]),
])
def test_subcommand_help(argv, tokens, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for token in tokens:
        assert token in text


def test_paper_run_help_documents_latest_and_dry_run(capsys):
    with pytest.raises(SystemExit):
        cli.main(["paper", "run", "--help"])
    text = capsys.readouterr().out
    assert "latest" in text and "dry-run" in text


def test_argparse_rejects_unknown_source_and_missing_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["data", "update", "--source", "wind"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc2:
        cli.main([])
    assert exc2.value.code == 2


@pytest.mark.parametrize("error,expected", [
    (DataUnavailableError(["local_cache"], "600000.SH", (START, END)), 2),
    (DataIntegrityError("NaN"), 2),
    (NetworkFailureError("all free sources down"), 3),
    (ConfigValidationError("bad config"), 4),
    (SchemaError("user_version mismatch"), 4),
])
def test_quant_error_exit_codes(monkeypatch, error, expected):
    def _raise(_args):
        raise error

    monkeypatch.setattr(cli, "cmd_report", _raise)
    assert cli.main(["report", "--run-id", "latest"]) == expected
    assert error.exit_code == expected


def test_invalid_config_returns_validation_exit_code(temp_config):
    cfg_path, tmp = temp_config
    bad = tmp / "bad.yaml"
    bad.write_text(json.dumps({"rules": {"price_limit_pct": 0.20}}), encoding="utf-8")
    assert cli.main(["--config", str(bad), "report", "--run-id", "x"]) == 4
    # 合法配置的空库 status 应正常返回 0（校验不误伤正常路径）
    assert cli.main(["--config", str(cfg_path), "data", "status"]) == 0


def test_backtest_on_empty_database_returns_data_missing(temp_config):
    cfg_path, _tmp = temp_config
    code = cli.main([
        "--config", str(cfg_path), "backtest", "--strategy", "dual_ma",
        "--start", START, "--end", END, "--limit", "5",
    ])
    assert code == 2


@pytest.mark.parametrize("source", ["baostock", "akshare"])
def test_network_update_all_sources_down_returns_exit_3(temp_config, monkeypatch, source):
    """网络命令路径本身可运行：全部免费源不可用时应以 exit=3 收尾，不崩溃。"""
    from quant.data import update as upd
    from quant.data.sources import AKShareSource, BaostockSource

    cfg_path, _tmp = temp_config
    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    payload["data"].update({"max_retries": 0, "retry_base_interval": 0.01})
    cfg_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _offline(_self):
        raise ConnectionError("offline in unit test")

    monkeypatch.setattr(BaostockSource, "_client", _offline)
    monkeypatch.setattr(AKShareSource, "_client", _offline)
    monkeypatch.setattr(
        upd, "_hs300_from_akshare",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in unit test")),
    )
    code = cli.main([
        "--config", str(cfg_path), "data", "update", "--source", source,
        "--pool", "hs300", "--limit", "2", "--start", START, "--end", END,
    ])
    assert code == 3


def test_cli_db_override_wins_over_yaml(temp_config, tmp_path):
    """CLI > config.yaml：--db 指定的库被实际创建并写入。"""
    cfg_path, tmp = temp_config
    override_db = tmp_path / "override.db"
    assert cli.main([
        "--config", str(cfg_path), "--db", str(override_db),
        "data", "status", "--pool", "hs300",
    ]) == 0
    assert override_db.exists()
    assert not (tmp / "cli.db").exists()
