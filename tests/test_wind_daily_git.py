from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from china_commodities.wind_daily_git import Publisher, git


class DailyGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'repo'
        self.root.mkdir()
        self.remote=Path(self.tmp.name)/'remote.git'
        git(self.root,'init','--bare','--initial-branch=main',str(self.remote))
        git(self.root,'init','--initial-branch=main')
        git(self.root,'config','user.name','Test')
        git(self.root,'config','user.email','test@example.invalid')
        (self.root/'data').mkdir()
        (self.root/'data/value.txt').write_text('old')
        (self.root/'source.py').write_text('# source\n')
        git(self.root,'add','.')
        git(self.root,'commit','-m','initial')
        git(self.root,'remote','add','origin',str(self.remote))
        git(self.root,'push','-u','origin','main')
        self.stage=Path(self.tmp.name)/'stage'
        self.stage.mkdir()
        (self.stage/'value.txt').write_text('new')
        self.publisher=Publisher(self.root,Path(self.tmp.name)/'cache')
        self.base=git(self.root,'rev-parse','HEAD')

    def test_push_failure_is_recovered_without_new_data_commit(self):
        self.publisher.prepare(self.stage,self.base)
        real_git=git
        def fail_push(root,*args):
            if args[0]=='push':
                raise RuntimeError('offline')
            return real_git(root,*args)
        with patch('china_commodities.wind_daily_git.git',side_effect=fail_push):
            with self.assertRaisesRegex(RuntimeError,'offline'):
                self.publisher.recover()
        commit=git(self.publisher.outbox,'rev-parse','HEAD')
        self.assertEqual((self.root/'data/value.txt').read_text(),'old')
        self.assertTrue(self.publisher.recover())
        self.assertEqual(git(self.root,'rev-parse','HEAD'),commit)
        self.assertEqual((self.root/'data/value.txt').read_text(),'new')
        self.assertFalse(self.publisher.journal.exists())

    def test_remote_race_retains_outbox_and_does_not_overwrite(self):
        self.publisher.prepare(self.stage,self.base)
        (self.root/'data/other.txt').write_text('remote update')
        git(self.root,'add','data')
        git(self.root,'commit','-m','other producer')
        git(self.root,'push','origin','main')
        remote=git(self.root,'rev-parse','HEAD')
        with self.assertRaisesRegex(RuntimeError,'advanced independently'):
            self.publisher.recover()
        self.assertTrue(self.publisher.journal.exists())
        self.assertEqual(git(self.remote,'rev-parse','main'),remote)

    def test_interrupted_cleanup_recovers_without_clone(self):
        self.publisher.prepare(self.stage,self.base)
        from china_commodities.wind_daily_git import remove_checkout
        def interrupted(path):
            remove_checkout(path)
            raise RuntimeError('interrupted cleanup')
        with patch('china_commodities.wind_daily_git.remove_checkout',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'interrupted cleanup'):
                self.publisher.recover()
        self.assertFalse(self.publisher.outbox.exists())
        self.assertTrue(self.publisher.recover())
        self.assertFalse(self.publisher.journal.exists())
        self.assertEqual((self.root/'data/value.txt').read_text(),'new')

    def test_dirty_local_code_is_not_auto_committed(self):
        (self.root/'source.py').write_text('user edit')
        with self.assertRaisesRegex(RuntimeError,'uncommitted'):
            self.publisher.prepare(self.stage,self.base)
        self.assertEqual(git(self.root,'rev-parse','HEAD'),self.base)
