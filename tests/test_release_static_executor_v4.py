import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from release_static_executor_v4 import (preflight,stage,switch,rollback,complete,_entry_sha)
from release_ui_artifact_v4 import create_bundle
A='a'*40
B='b'*40

class StaticExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        app=self.root/'app'
        app.mkdir()
        (self.root/'env').mkdir()
        env=self.root/'env/production.env'
        env.write_text('RELEASE_SHA='+A+'\nGSALES_OUTREACH_ENABLED=false\n')
        env.chmod(0o600)
        live=app/'miniapp'
        live.mkdir()
        (live/'index.html').write_text('<meta name="gpoint-build-sha" content="'+A+'">')
        dist=self.root/'dist'
        (dist/'assets').mkdir(parents=True)
        (dist/'index.html').write_text('<meta name="gpoint-build-sha" content="'+B+'">')
        (dist/'assets/app.js').write_text('console.log(1)')
        self.archive=self.root/'site.zip'
        create_bundle(dist,B,self.archive)

    def test_stage_switch_complete_and_block_replay(self):
        self.assertEqual(preflight(self.root,A,B)['status'],'PASS')
        self.assertEqual(stage(self.root,self.archive,A,B)['status'],'STAGED')
        self.assertEqual(switch(self.root,A,B)['phase'],'SWITCHED')
        self.assertEqual(_entry_sha(self.root/'app/miniapp'),B)
        self.assertEqual(complete(self.root,A,B)['phase'],'COMPLETE')
        with self.assertRaises(RuntimeError):
            switch(self.root,A,B)

    def test_crash_like_switch_can_rollback(self):
        stage(self.root,self.archive,A,B)
        switch(self.root,A,B)
        self.assertEqual(rollback(self.root,A,B)['phase'],'ROLLED_BACK')
        self.assertIn('RELEASE_SHA='+A,(self.root/'env/production.env').read_text())
        self.assertEqual(_entry_sha(self.root/'app/miniapp'),A)

    def test_crash_after_armed_before_exchange_can_rollback(self):
        stage(self.root,self.archive,A,B)
        from release_static_executor_v4 import _save_journal
        _save_journal(self.root,B,{'candidateSha':B,'baselineSha':A,'phase':'ARMED'})
        self.assertEqual(rollback(self.root,A,B)['phase'],'ROLLED_BACK')

    def test_dirty_journal_blocks_preflight(self):
        from release_static_executor_v4 import _save_journal
        _save_journal(self.root,B,{'candidateSha':B,'baselineSha':A,'phase':'SWITCHED'})
        with self.assertRaises(RuntimeError):
            preflight(self.root,A,B)

    def test_bad_input_cannot_switch(self):
        stage(self.root,self.archive,A,B)
        with self.assertRaises(ValueError):
            switch(self.root,B,A)
        self.assertEqual(_entry_sha(self.root/'app/miniapp'),A)

if __name__=='__main__':
    unittest.main()
