#!/usr/bin/env bash
# QUANT-001 完全离线端到端 smoke：不访问任何网络接口。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

DB="data/smoke_$(date +%s).db"
START="2022-01-04"
END="2022-12-30"

echo "==> python: $PY"
echo "==> db:     $DB"

"$PY" tests/fixtures/generate_fixtures.py

echo "==> 1/7 data update (offline fixture, 5 MAIN symbols)"
"$PY" -m quant data update --source fixture --pool hs300 --limit 5 \
  --start "$START" --end "$END" --db "$DB"

echo "==> 2/7 data status (expected/actual/missing audit)"
"$PY" -m quant data status --pool hs300 --db "$DB"

echo "==> 3/7 event-driven backtest (local SQLite only)"
"$PY" -m quant backtest --strategy dual_ma --start "$START" --end "$END" \
  --limit 5 --db "$DB"

echo "==> 4/7 paper run --dry-run (no commit)"
"$PY" -m quant paper run --date latest --dry-run --limit 5 --db "$DB"

echo "==> 5/7 paper run (commit)"
"$PY" -m quant paper run --date latest --limit 5 --db "$DB"

echo "==> 6/7 paper run again (idempotency: no duplicate orders/trades)"
"$PY" -m quant paper run --date latest --limit 5 --db "$DB"

echo "==> 7/7 report --run-id latest"
"$PY" -m quant report --run-id latest --db "$DB"

echo "==> offline pytest"
"$PY" -m pytest

echo "==> SMOKE OK (offline, zero paid API, no real orders)"
