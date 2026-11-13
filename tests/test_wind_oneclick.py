from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from scripts import wind_oneclick as launcher


class LauncherTests(unittest.TestCase):
    def test_repository_entry_is_found_without_prompts_or_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/run_wind_daily.py').touch()
            with patch.object(launcher, 'BASE', root / 'scripts'), patch('builtins.input', side_effect=AssertionError):
                self.assertEqual(launcher.repository(), root.resolve())

    def test_installed_launcher_uses_only_commodity_repository(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / 'commodity'
            (repo / 'scripts').mkdir(parents=True)
            (repo / 'scripts/run_wind_daily.py').touch()
            (base / 'launcher.json').write_text(json.dumps(dict(commodities=str(repo), stock='missing')))
            with patch.object(launcher, 'BASE', base):
                self.assertEqual(launcher.repository(), repo.resolve())
