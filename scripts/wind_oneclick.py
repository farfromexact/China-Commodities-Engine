"""Compatibility launcher: delegate directly to the commodity repository entry."""
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent


def repository():
    candidates = [BASE.parent]
    settings_path = BASE / 'launcher.json'
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
        if settings.get('commodities'):
            candidates.append(Path(settings['commodities']).expanduser())
    candidates.append(BASE.parent / 'China-Commodities-Engine')
    for root in candidates:
        if (root / 'scripts/run_wind_daily.py').is_file():
            return root.resolve()
    raise RuntimeError('找不到商品仓库，请使用仓库中的 START_WIND.cmd。')


def main(argv=None):
    root = repository()
    python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not python.is_file():
        raise RuntimeError(f'找不到仓库 Python: {python}')
    args = list(sys.argv[1:] if argv is None else argv)
    return subprocess.call([str(python), str(root / 'scripts/run_wind_daily.py'), *args], cwd=root,
                           env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'))


if __name__ == '__main__':
    raise SystemExit(main())
