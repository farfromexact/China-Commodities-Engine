"""Register the official SDK only in this repository's virtual environment.

Matches the trading-desk Windows setup convention: WIND_PYTHON_DIR and a
project-local WindPy.pth. Does not load native libraries or start/end sessions.
"""
import argparse
from pathlib import Path
import sys
import sysconfig

from china_commodities.collectors.wind_runtime import sdk_directory

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sdk-dir','--wind-python-dir',dest='sdk_dir')
    p.add_argument('--check',action='store_true')
    a=p.parse_args()
    if sys.prefix==sys.base_prefix or Path(sys.prefix).resolve()!=(ROOT/'.venv').resolve():
        p.error('Run with this repository\'s .venv Python; other environments are not modified')
    path=sdk_directory(a.sdk_dir)
    if path is None:
        p.error('Official SDK not found; supply --sdk-dir or WIND_PYTHON_DIR')
    destination=Path(sysconfig.get_path('purelib'))/'WindPy.pth'
    if not destination.resolve().is_relative_to(Path(sys.prefix).resolve()):
        p.error('site-packages is outside this project virtual environment')
    print(f'Python: {sys.executable}\nSDK: {path}\nPath file: {destination}')
    if not a.check:
        destination.write_text(str(path)+'\n',encoding='utf-8')
        print('Configured. Login and live data access still require a separate canary.')
    else:
        print('Paths checked only; native loading, login and data access NOT tested.')


if __name__=='__main__':
    main()
