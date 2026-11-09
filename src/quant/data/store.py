"""SQLite persistence layer.

* ``PRAGMA user_version`` records the schema version (currently 1).
* R-014: ``universe.board`` has a SQL ``CHECK`` enum constraint.
* R-013: every bulk read goes through a keyset-paginated generator; the
  backtest engine never materialises a multi-year full-market frame.
* Idempotency (paper trading) is enforced by SQL UNIQUE constraints, not by
  application-level best effort.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd

from ..errors import DataIntegrityError, SchemaError

SCHEMA_VERSION = 1
BOARDS = ("MAIN", "CYB", "KCB", "BSE", "ST", "OTHER")
ORDER_STATUSES = ("pending", "filled", "rejected", "cancelled", "expired")
PRICE_FIELDS = (
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "adj_factor", "source", "updated_at",
)
UNIVERSE_FIELDS = ("symbol", "name", "industry", "board", "in_date", "out_date", "source", "pool")
EQUITY_FIELDS = ("strategy", "date", "cash", "market_value", "equity",
                 "drawdown", "stale_price", "updated_at")
TRADE_FIELDS = ("trade_id", "order_id", "fill_date", "symbol", "side", "qty", "price",
                "commission", "stamp_tax", "slippage_cost", "created_at")
ORDER_FIELDS = ("order_id", "trade_date", "strategy", "symbol", "side", "qty",
                "order_type", "status", "reason", "created_at", "placed_date",
                "valid_from", "attempts", "est_price")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS daily_price (
    symbol      TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    open        REAL    NOT NULL CHECK(open  > 0),
    high        REAL    NOT NULL CHECK(high  > 0),
    low         REAL    NOT NULL CHECK(low   > 0),
    close       REAL    NOT NULL CHECK(close > 0),
    volume      REAL    NOT NULL CHECK(volume >= 0),
    amount      REAL    NOT NULL CHECK(amount >= 0),
    adj_factor  REAL,
    source      TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_price(date, symbol);

CREATE TABLE IF NOT EXISTS trade_calendar (
    date     TEXT PRIMARY KEY,
    is_open  INTEGER NOT NULL CHECK(is_open IN (0, 1)),
    source   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS universe (
    symbol    TEXT NOT NULL,
    name      TEXT,
    industry  TEXT,
    board     TEXT NOT NULL DEFAULT 'MAIN'
              CHECK (board IN ('MAIN','CYB','KCB','BSE','ST','OTHER')),
    in_date   TEXT,
    out_date  TEXT,
    source    TEXT,
    pool      TEXT NOT NULL DEFAULT 'hs300',
    PRIMARY KEY (symbol, pool)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id    TEXT PRIMARY KEY,
    trade_date  TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty         INTEGER NOT NULL CHECK(qty >= 0),
    order_type  TEXT NOT NULL DEFAULT 'MKT_OPEN_NEXT',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','filled','rejected','cancelled','expired')),
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    placed_date TEXT,
    valid_from  TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    est_price   REAL,
    UNIQUE (trade_date, strategy, symbol, side)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON paper_orders(status, valid_from);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id       TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    fill_date      TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    price          REAL NOT NULL,
    commission     REAL NOT NULL DEFAULT 0,
    stamp_tax      REAL NOT NULL DEFAULT 0,
    slippage_cost  REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    UNIQUE (order_id, fill_date)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    strategy   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    avg_cost   REAL NOT NULL,
    last_price REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (strategy, symbol)
);

CREATE TABLE IF NOT EXISTS paper_equity (
    strategy     TEXT NOT NULL,
    date         TEXT NOT NULL,
    cash         REAL NOT NULL,
    market_value REAL NOT NULL,
    equity       REAL NOT NULL,
    drawdown     REAL NOT NULL DEFAULT 0,
    stale_price  INTEGER NOT NULL DEFAULT 0 CHECK(stale_price IN (0,1)),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (strategy, date)
);

-- Extra state table (single row per strategy) so paper run is resumable.
CREATE TABLE IF NOT EXISTS paper_state (
    strategy    TEXT PRIMARY KEY,
    cash        REAL NOT NULL,
    peak_equity REAL NOT NULL,
    risk_mode   TEXT NOT NULL DEFAULT 'NORMAL',
    risk_days   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- Persisted run metadata so `report --run-id` can re-render offline.
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    audit_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._in_tx = False
        self.init_schema()

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._in_tx:
            self.rollback()
        self.conn.close()

    def begin(self) -> None:
        if not self._in_tx:
            self.conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self.conn.execute("COMMIT")
            self._in_tx = False

    def rollback(self) -> None:
        if self._in_tx:
            self.conn.execute("ROLLBACK")
            self._in_tx = False

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Batch bulk upserts into ONE transaction.

        ``isolation_level=None`` means autocommit: without this, every row of a
        multi-year backfill triggers its own fsync (thousands of commits).
        Nested usage (paper run holds an outer transaction) is a no-op.
        """
        if self._in_tx:
            yield
            return
        self.begin()
        try:
            yield
            self.commit()
        except Exception:
            self.rollback()
            raise

    def init_schema(self) -> None:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise SchemaError(f"数据库 schema 版本 {version} 高于程序支持的 {SCHEMA_VERSION}")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------------ #
    # daily_price
    # ------------------------------------------------------------------ #
    def upsert_daily(self, frame: pd.DataFrame, page_size: int = 500) -> int:
        """R-013 paged batch write. Returns number of rows upserted."""
        if frame is None or frame.empty:
            return 0
        required = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "source"]
        missing = [c for c in required if c not in frame.columns]
        if missing:
            raise DataIntegrityError(f"upsert_daily 缺字段 {missing}")
        data = frame.copy()
        if "adj_factor" not in data.columns:
            data["adj_factor"] = None  # 明确记录缺失（不填补），当日不生成信号
        if data[required[:2] + ["open", "high", "low", "close", "volume", "amount"]].isna().any().any():
            raise DataIntegrityError("行情含 NaN，拒绝写入（R-005）")
        if (data[["open", "high", "low", "close"]] <= 0).any().any():
            raise DataIntegrityError("行情含非正价格，拒绝写入（R-005）")
        now = utcnow()
        sql = """
            INSERT INTO daily_price
              (symbol,date,open,high,low,close,volume,amount,adj_factor,source,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,date) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume, amount=excluded.amount,
              adj_factor=excluded.adj_factor, source=excluded.source,
              updated_at=excluded.updated_at
        """
        rows = [
            (
                r.symbol, str(r.date)[:10], float(r.open), float(r.high), float(r.low),
                float(r.close), float(r.volume), float(r.amount),
                None if pd.isna(r.adj_factor) else float(r.adj_factor),
                str(r.source), now,
            )
            for r in data.itertuples(index=False)
        ]
        batch = max(1, int(page_size))
        with self._transaction():
            for offset in range(0, len(rows), batch):
                self.conn.executemany(sql, rows[offset: offset + batch])
        return len(rows)

    def read_daily(
        self, symbols: Sequence[str] | None, start: str, end: str, page_size: int = 500
    ) -> pd.DataFrame:
        """Convenience full read for a *single* symbol or a small range.

        Engines must use ``iter_daily`` / ``iter_daily_on_date`` instead.
        """
        if not symbols:
            return pd.DataFrame(columns=list(PRICE_FIELDS))
        pages = list(self.iter_daily(symbols, start, end, page_size))
        if not pages:
            return pd.DataFrame(columns=list(PRICE_FIELDS))
        return pd.concat(pages, ignore_index=True)

    def iter_daily(
        self,
        symbols: Sequence[str] | None,
        start: str,
        end: str,
        page_size: int = 500,
    ) -> Iterator[pd.DataFrame]:
        """R-013 keyset pagination over (date, symbol). Bounded memory."""
        size = max(1, int(page_size))
        placeholders, params = self._in_clause(symbols)
        where = f"date BETWEEN ? AND ? {placeholders}"
        base: list[Any] = [str(start), str(end), *params]
        last_date, last_symbol = "", ""
        while True:
            sql = (
                f"SELECT {','.join(PRICE_FIELDS)} FROM daily_price "
                f"WHERE {where} AND (date > ? OR (date = ? AND symbol > ?)) "
                f"ORDER BY date, symbol LIMIT ?"
            )
            rows = self.conn.execute(sql, [*base, last_date, last_date, last_symbol, size]).fetchall()
            if not rows:
                break
            yield self._rows_to_frame(rows)
            last_date, last_symbol = rows[-1]["date"], rows[-1]["symbol"]
            if len(rows) < size:
                break

    def iter_daily_on_date(
        self, date: str, symbols: Sequence[str] | None = None, page_size: int = 500
    ) -> Iterator[pd.DataFrame]:
        """One trading day's bars, paged by symbol. Used by the event loop."""
        size = max(1, int(page_size))
        placeholders, params = self._in_clause(symbols)
        last_symbol = ""
        while True:
            sql = (
                f"SELECT {','.join(PRICE_FIELDS)} FROM daily_price "
                f"WHERE date = ? {placeholders} AND symbol > ? "
                f"ORDER BY symbol LIMIT ?"
            )
            rows = self.conn.execute(sql, [str(date), *params, last_symbol, size]).fetchall()
            if not rows:
                break
            yield self._rows_to_frame(rows)
            last_symbol = rows[-1]["symbol"]
            if len(rows) < size:
                break

    @staticmethod
    def _in_clause(symbols: Sequence[str] | None) -> tuple[str, list[Any]]:
        if not symbols:
            return "", []
        values = [str(s) for s in symbols]
        return f"AND symbol IN ({','.join('?' for _ in values)})", values

    @staticmethod
    def _rows_to_frame(rows: Sequence[sqlite3.Row]) -> pd.DataFrame:
        return pd.DataFrame([dict(r) for r in rows], columns=list(PRICE_FIELDS))

    def latest_trade_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(date) AS d FROM daily_price").fetchone()
        return row["d"] if row and row["d"] else None

    def all_dates(self, start: str | None = None, end: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT date FROM daily_price WHERE 1=1"
        params: list[Any] = []
        if start:
            sql += " AND date >= ?"
            params.append(str(start))
        if end:
            sql += " AND date <= ?"
            params.append(str(end))
        sql += " ORDER BY date"
        return [r["date"] for r in self.conn.execute(sql, params).fetchall()]

    def count_daily(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM daily_price").fetchone()["c"])

    # ------------------------------------------------------------------ #
    # trade_calendar (R-011)
    # ------------------------------------------------------------------ #
    def upsert_calendar(self, frame: pd.DataFrame, source: str | None = None) -> int:
        if frame is None or frame.empty:
            return 0
        now_source = source or (frame["source"].iloc[0] if "source" in frame.columns else "unknown")
        rows = [
            (str(r.date)[:10], int(r.is_open), now_source)
            for r in frame[["date", "is_open"]].itertuples(index=False)
        ]
        with self._transaction():
            self.conn.executemany(
                "INSERT INTO trade_calendar(date,is_open,source) VALUES(?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET is_open=excluded.is_open, source=excluded.source",
                rows,
            )
        return len(rows)

    def trading_days(self, start: str | None = None, end: str | None = None) -> list[str]:
        """Only ``is_open=1`` rows from trade_calendar. Never pd.bdate_range."""
        sql = "SELECT date FROM trade_calendar WHERE is_open = 1"
        params: list[Any] = []
        if start:
            sql += " AND date >= ?"
            params.append(str(start))
        if end:
            sql += " AND date <= ?"
            params.append(str(end))
        sql += " ORDER BY date"
        return [r["date"] for r in self.conn.execute(sql, params).fetchall()]

    def next_trading_day(self, date: str) -> str | None:
        row = self.conn.execute(
            "SELECT date FROM trade_calendar WHERE is_open=1 AND date > ? ORDER BY date LIMIT 1",
            (str(date),),
        ).fetchone()
        return row["date"] if row else None

    def calendar_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM trade_calendar").fetchone()["c"])

    # ------------------------------------------------------------------ #
    # universe (R-014)
    # ------------------------------------------------------------------ #
    def upsert_universe(self, frame: pd.DataFrame, pool: str = "hs300") -> int:
        if frame is None or frame.empty:
            return 0
        data = frame.copy()
        if "board" not in data.columns:
            data["board"] = "MAIN"
        invalid = set(data["board"].astype(str)) - set(BOARDS)
        if invalid:
            raise DataIntegrityError(f"universe.board 非法枚举: {sorted(invalid)}")
        for col in ("name", "industry", "in_date", "out_date", "source"):
            if col not in data.columns:
                data[col] = None
        def clean(value: Any) -> Any:
            return None if value is None or (isinstance(value, float) and pd.isna(value)) else value

        rows = [
            (
                str(r.symbol), clean(r.name), clean(r.industry), str(r.board),
                clean(r.in_date), clean(r.out_date), clean(r.source) or "unknown", pool,
            )
            for r in data.itertuples(index=False)
        ]
        with self._transaction():
            self.conn.executemany(
                "INSERT INTO universe(symbol,name,industry,board,in_date,out_date,source,pool) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,pool) DO UPDATE SET "
                "name=excluded.name, industry=excluded.industry, board=excluded.board, "
                "in_date=excluded.in_date, out_date=excluded.out_date, source=excluded.source",
                rows,
            )
        return len(rows)

    def get_universe(
        self, pool: str = "hs300", limit: int | None = None, main_only: bool = True
    ) -> pd.DataFrame:
        """Deterministic order: ORDER BY symbol. ``limit`` = first N, never random."""
        sql = "SELECT symbol,name,industry,board,in_date,out_date,source,pool FROM universe WHERE pool = ?"
        params: list[Any] = [pool]
        if main_only:
            sql += " AND board = 'MAIN'"
        sql += " AND (out_date IS NULL OR out_date = '') ORDER BY symbol"
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(UNIVERSE_FIELDS))

    def board_counts(self, pool: str = "hs300") -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT board, COUNT(*) AS c FROM universe WHERE pool=? GROUP BY board", (pool,)
        ).fetchall()
        return {r["board"]: int(r["c"]) for r in rows}

    # ------------------------------------------------------------------ #
    # audit
    # ------------------------------------------------------------------ #
    def missing_days_audit(
        self, symbols: Sequence[str], start: str, end: str
    ) -> dict[str, Any]:
        expected = self.trading_days(start, end)
        n_expected = len(expected)
        placeholders, params = self._in_clause(symbols)
        sql = (
            f"SELECT symbol, COUNT(DISTINCT date) AS c FROM daily_price "
            f"WHERE date BETWEEN ? AND ? {placeholders} GROUP BY symbol"
        )
        counts = {
            r["symbol"]: int(r["c"])
            for r in self.conn.execute(sql, [str(start), str(end), *params]).fetchall()
        }
        per_symbol = {
            s: {
                "expected_days": n_expected,
                "actual_days": counts.get(s, 0),
                "missing_days": max(0, n_expected - counts.get(s, 0)),
            }
            for s in symbols
        }
        total_actual = sum(v["actual_days"] for v in per_symbol.values())
        total_expected = n_expected * len(symbols)
        adj_missing = 0
        if symbols:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS c FROM daily_price WHERE date BETWEEN ? AND ? "
                f"{placeholders} AND adj_factor IS NULL",
                [str(start), str(end), *params],
            ).fetchone()
            adj_missing = int(row["c"])
        return {
            "expected_days": total_expected,
            "actual_days": total_actual,
            "missing_days": max(0, total_expected - total_actual),
            "calendar_days": n_expected,
            "per_symbol": per_symbol,
            "adj_factor_missing": adj_missing,
            "sources": [
                r["source"] for r in self.conn.execute(
                    "SELECT DISTINCT source FROM daily_price ORDER BY source"
                ).fetchall()
            ],
        }

    # ------------------------------------------------------------------ #
    # paper trading helpers
    # ------------------------------------------------------------------ #
    def insert_order(self, order: dict[str, Any]) -> bool:
        """Returns False when the idempotency UNIQUE constraint blocked a duplicate."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO paper_orders "
            "(order_id,trade_date,strategy,symbol,side,qty,order_type,status,reason,"
            " created_at,placed_date,valid_from,attempts,est_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order["order_id"], order["trade_date"], order["strategy"], order["symbol"],
                order["side"], int(order["qty"]), order.get("order_type", "MKT_OPEN_NEXT"),
                order.get("status", "pending"), order.get("reason", ""), order.get("created_at", utcnow()),
                order.get("placed_date", order["trade_date"]), order.get("valid_from"),
                int(order.get("attempts", 0)), order.get("est_price"),
            ),
        )
        return cur.rowcount > 0

    def update_order(self, order_id: str, **fields: Any) -> None:
        allowed = {"status", "reason", "attempts", "valid_from", "qty"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return
        sql = "UPDATE paper_orders SET " + ",".join(f"{k}=?" for k in keys) + " WHERE order_id=?"
        self.conn.execute(sql, [fields[k] for k in keys] + [order_id])

    def pending_orders(self, strategy: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM paper_orders WHERE strategy=? AND status='pending' "
            "ORDER BY trade_date, order_id",
            (strategy,),
        ).fetchall()
        return [dict(r) for r in rows]

    def orders_on_date(self, strategy: str, trade_date: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM paper_orders WHERE strategy=? AND trade_date=?",
            (strategy, str(trade_date)),
        ).fetchall()]

    def insert_trade(self, trade: dict[str, Any]) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO paper_trades "
            "(trade_id,order_id,fill_date,symbol,side,qty,price,commission,stamp_tax,"
            " slippage_cost,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade["trade_id"], trade["order_id"], trade["fill_date"], trade["symbol"],
                trade["side"], int(trade["qty"]), float(trade["price"]),
                float(trade["commission"]), float(trade["stamp_tax"]),
                float(trade["slippage_cost"]), trade.get("created_at", utcnow()),
            ),
        )
        return cur.rowcount > 0

    def replace_positions(self, strategy: str, positions: Iterable[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM paper_positions WHERE strategy=?", (strategy,))
        rows = [
            (strategy, p["symbol"], int(p["qty"]), float(p["avg_cost"]),
             float(p["last_price"]), utcnow())
            for p in positions
            if int(p["qty"]) > 0
        ]
        if rows:
            self.conn.executemany(
                "INSERT INTO paper_positions(strategy,symbol,qty,avg_cost,last_price,updated_at) "
                "VALUES(?,?,?,?,?,?)", rows,
            )

    def get_positions(self, strategy: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM paper_positions WHERE strategy=?", (strategy,)
        ).fetchall()]

    def replace_equity(self, strategy: str, record: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO paper_equity(strategy,date,cash,market_value,equity,drawdown,"
            "stale_price,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(strategy,date) DO UPDATE SET cash=excluded.cash, "
            "market_value=excluded.market_value, equity=excluded.equity, "
            "drawdown=excluded.drawdown, stale_price=excluded.stale_price, "
            "updated_at=excluded.updated_at",
            (
                strategy, record["date"], float(record["cash"]), float(record["market_value"]),
                float(record["equity"]), float(record["drawdown"]),
                int(record.get("stale_price", 0)), utcnow(),
            ),
        )

    def get_equity(self, strategy: str) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT * FROM paper_equity WHERE strategy=? ORDER BY date", (strategy,)
        ).fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(EQUITY_FIELDS))

    def last_equity_date(self, strategy: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM paper_equity WHERE strategy=?", (strategy,)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def get_trades(self, strategy: str) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT t.* FROM paper_trades t JOIN paper_orders o ON t.order_id=o.order_id "
            "WHERE o.strategy=? ORDER BY t.fill_date, t.trade_id", (strategy,)
        ).fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(TRADE_FIELDS))

    def get_orders(self, strategy: str) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT * FROM paper_orders WHERE strategy=? ORDER BY trade_date, order_id", (strategy,)
        ).fetchall()
        return pd.DataFrame([dict(r) for r in rows], columns=list(ORDER_FIELDS))

    def save_state(self, strategy: str, state: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO paper_state(strategy,cash,peak_equity,risk_mode,risk_days,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(strategy) DO UPDATE SET "
            "cash=excluded.cash, peak_equity=excluded.peak_equity, "
            "risk_mode=excluded.risk_mode, risk_days=excluded.risk_days, "
            "updated_at=excluded.updated_at",
            (
                strategy, float(state["cash"]), float(state["peak_equity"]),
                state.get("risk_mode", "NORMAL"), int(state.get("risk_days", 0)), utcnow(),
            ),
        )

    def get_state(self, strategy: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM paper_state WHERE strategy=?", (strategy,)
        ).fetchone()
        return dict(row) if row else None

    def count_orders(self, strategy: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM paper_orders"
        params: list[Any] = []
        if strategy:
            sql += " WHERE strategy=?"
            params.append(strategy)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    def count_trades(self, strategy: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM paper_trades"
        params: list[Any] = []
        if strategy:
            sql += " JOIN paper_orders o ON paper_trades.order_id=o.order_id WHERE o.strategy=?"
            params.append(strategy)
        return int(self.conn.execute(sql, params).fetchone()["c"])

    # ------------------------------------------------------------------ #
    # run metadata (for `report --run-id`)
    # ------------------------------------------------------------------ #
    def save_run(self, run_id: str, kind: str, config: dict[str, Any], audit: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_id,kind,config_json,audit_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (run_id, kind, json.dumps(config, ensure_ascii=False, default=str),
             json.dumps(audit, ensure_ascii=False, default=str), utcnow()),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"], "kind": row["kind"],
            "config": json.loads(row["config_json"]),
            "audit": json.loads(row["audit_json"]),
            "created_at": row["created_at"],
        }
