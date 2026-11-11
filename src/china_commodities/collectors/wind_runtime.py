"""Load an already-installed official Wind SDK without changing shared state."""
import importlib
import os
from pathlib import Path
import sys
import sysconfig


def sdk_directory(sdk_dir=None):
    directory = sdk_dir or os.environ.get('WIND_PYTHON_DIR') or os.environ.get('WIND_PYTHON_PATH')
    if not directory:
        pth=Path(sysconfig.get_path('purelib'))/'WindPy.pth'
        if pth.is_file():
            for line in pth.read_text(encoding='utf-8-sig').splitlines():
                line=line.strip()
                if not line or line.startswith(('#','import ', 'import\t')):
                    continue
                candidate=Path(line)
                if not candidate.is_absolute():
                    candidate=pth.parent/candidate
                if (candidate/'WindPy.py').is_file() or (candidate/'WindPy/__init__.py').is_file():
                    directory=candidate
                    break
    if not directory and sys.platform == 'darwin':
        directory = '/Applications/Wind API.app/Contents/python'
    if not directory and sys.platform == 'win32':
        default = Path.home()/'Wind/Wind.NET.Client/WindNET/x64'
        if (default/'WindPy.py').is_file():
            directory = default
    if directory:
        path = Path(directory).expanduser()
        if not (path/'WindPy.py').is_file() and not (path/'WindPy/__init__.py').is_file():
            raise ValueError('SDK directory must contain the official WindPy.py or WindPy package')
        return path.resolve()
    return None


def load_wind(sdk_dir=None):
    path = sdk_directory(sdk_dir)
    if path:
        sys.path.insert(0, str(path))
    try:
        return importlib.import_module('WindPy').w
    except ImportError as exc:
        raise RuntimeError('Install the official Wind SDK for this Python, or set WIND_PYTHON_DIR/--sdk-dir') from exc
