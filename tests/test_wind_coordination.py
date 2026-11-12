import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from china_commodities import wind_coordination as coordination


class CoordinationTests(unittest.TestCase):
    def test_lock_blocks_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(coordination, 'collection_lock_path', return_value=Path(tmp)/'shared.lock'):
            code = "from pathlib import Path; from china_commodities import wind_coordination as c; c.collection_lock_path=lambda:Path("+repr(str(Path(tmp)/'shared.lock'))+"); ctx=c.collection_lock(); ctx.__enter__(); print('acquired'); ctx.__exit__(None,None,None)"
            with coordination.collection_lock():
                blocked=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,env=dict(os.environ,PYTHONPATH=str(Path(coordination.__file__).resolve().parents[1])))
                self.assertNotEqual(blocked.returncode,0)
                self.assertIn('CollectionBusy',blocked.stderr)
            released=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,env=dict(os.environ,PYTHONPATH=str(Path(coordination.__file__).resolve().parents[1])))
            self.assertEqual(released.returncode,0,released.stderr)

    def test_pacer_leaves_idle_time_after_slow_call(self):
        with patch.object(coordination.time,'monotonic',side_effect=[10,40,41,45]), patch.object(coordination.time,'sleep') as sleep:
            pacer=coordination.RequestPacer(2)
            with pacer.slot(): pass
            with pacer.slot(): pass
            sleep.assert_called_once_with(1)
            self.assertEqual(pacer.next_at,47)

    def test_invalid_intervals_are_rejected(self):
        for value in (0,.5,-1,True,'2',float('nan'),float('inf')):
            with self.subTest(value=value),self.assertRaises(ValueError):
                coordination.RequestPacer(value)

    def test_error_also_enforces_delay(self):
        with patch.object(coordination.time,'monotonic',return_value=10):
            pacer=coordination.RequestPacer(2)
            with self.assertRaises(RuntimeError),pacer.slot():
                raise RuntimeError('failed native call')
            self.assertEqual(pacer.next_at,12)
