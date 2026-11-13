#!/bin/sh
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
exec ./.venv/bin/python scripts/run_wind_daily.py "$@"
