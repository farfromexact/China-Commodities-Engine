"""Path resolution tests only; these are not Windows native SDK validation."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from china_commodities.collectors.wind_runtime import sdk_directory, load_wind


class WindRuntimeTests(unittest.TestCase):
    def test_saved_project_path_precedes_platform_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            sdk=root/'custom-sdk'
            sdk.mkdir()
            (sdk/'WindPy.py').write_text('# fake SDK; never imported\n')
            (root/'WindPy.pth').write_text(str(sdk)+'\n')
            with patch('sys.platform','win32'),patch.dict('os.environ',{},clear=True),patch('sysconfig.get_path',return_value=str(root)):
                self.assertEqual(sdk_directory(),sdk.resolve())

    def test_explicit_directory_wins_over_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            (path/'WindPy.py').write_text('# fake SDK; never imported\n')
            with patch.dict('os.environ',{'WIND_PYTHON_DIR':'missing-sdk'}):
                self.assertEqual(sdk_directory(path),path.resolve())

    def test_windows_default_matches_trading_desk_convention(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            sdk=home/'Wind/Wind.NET.Client/WindNET/x64'
            sdk.mkdir(parents=True)
            (sdk/'WindPy.py').write_text('# fake SDK; never imported\n')
            with patch('sys.platform','win32'),patch('pathlib.Path.home',return_value=home),patch.dict('os.environ',{},clear=True):
                self.assertEqual(sdk_directory(),sdk.resolve())

    def test_environment_matches_trading_desk_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            (path/'WindPy.py').write_text('# fake SDK; never imported\n')
            with patch.dict('os.environ',{'WIND_PYTHON_DIR':str(path),'WIND_PYTHON_PATH':'invalid'}):
                self.assertEqual(sdk_directory(),path.resolve())

    def test_missing_explicit_sdk_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'official WindPy'):
                sdk_directory(tmp)

    def test_installed_module_load_does_not_login_or_logout(self):
        client=object()
        with patch('sys.platform','win32'),patch.dict('os.environ',{},clear=True),patch('pathlib.Path.is_file',return_value=False),patch('importlib.import_module',return_value=SimpleNamespace(w=client)) as importer:
            self.assertIs(load_wind(),client)
            importer.assert_called_once_with('WindPy')
