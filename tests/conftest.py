"""离线测试公共 fixture：临时 SQLite + checked-in CSV，绝不访问网络。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.config import load_config
from quant.data.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
START = "2022-01-04"
END = "2022-12-30"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def cfg(tmp_path: Path):
    return load_config(
        None,
        overrides={
            "database.path": str(tmp_path / "quant.db"),
            "report.out_dir": str(tmp_path / "reports"),
            "report.make_png": False,
            "data.page_size": 137,          # 非默认值，验证分页可配置
        },
        env={},
        root=ROOT,
    )


@pytest.fixture
def store(tmp_path: Path):
    db = Store(tmp_path / "quant.db")
    db.upsert_universe(pd.read_csv(FIXTURES / "universe.csv", dtype=str))
    db.upsert_calendar(pd.read_csv(FIXTURES / "trade_calendar.csv", dtype={"date": str}))
    db.upsert_daily(pd.read_csv(FIXTURES / "daily_prices.csv",
                                dtype={"symbol": str, "date": str}))
    yield db
    db.close()


@pytest.fixture
def main_symbols(store: Store) -> list[str]:
    return store.get_universe("hs300", limit=5, main_only=True)["symbol"].tolist()


@pytest.fixture
def trading_days(store: Store) -> list[str]:
    return store.trading_days(START, END)
