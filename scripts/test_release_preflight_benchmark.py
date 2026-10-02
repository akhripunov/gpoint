import importlib.util,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('coordinator',Path(__file__).with_name('release-preflight-benchmark.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SHA='a'*40
class CoordinatorTest(unittest.TestCase):
 def test_latest_failed_prepare_is_not_replaced_by_older_success(self):
  runs=[dict(head_sha=SHA,head_branch='main',event='push',run_number=n,status='completed',conclusion=c) for n,c in [(1,'success'),(2,'failure')]]
  self.assertEqual(m.select_prepare(runs,SHA)['conclusion'],'failure')
 def test_both_projects_use_only_readonly_mode_and_exact_receipts(self):
  for project in m.PROJECTS:
   with self.subTest(project=project),tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);calls=[]
    def api(path):
     if path.endswith('git/ref/heads/main'):return {'object':{'sha':SHA}}
     return {'workflow_runs':[dict(id=1,head_sha=SHA,head_branch='main',event='push',run_number=1,status='completed',conclusion='success')]}
    def dispatch(p,w,fields):calls.append(fields);return 2
    def cli(*args):
     dest=Path(args[args.index('-D')+1]);dest.mkdir()
     data={'status':'PLANNED','applicationVerification':'NOT_DEPLOYED','candidateSha':SHA} if project=='gpoint' else {'production_changed':False,'preflight':'PASS','candidate_sha':SHA}
     (dest/'receipt.json').write_text(json.dumps(data));return ''
    with patch.object(m,'api',api),patch.object(m,'dispatch',dispatch),patch.object(m,'wait_run',return_value={'id':2}),patch.object(m,'cli',cli):
     r=m.project_run(project,root,SimpleNamespace(timeout_seconds=60,prepare_missing=False,maxibot_migration='revision'))
    self.assertEqual(r['status'],'PASS');self.assertEqual(calls[0]['mode'],'preflight');self.assertEqual(calls[0]['approve_production'],'false');self.assertFalse(r['production_applied'])
 def test_production_without_exact_owner_approval_stops_before_dispatch(self):
  with tempfile.TemporaryDirectory() as temporary:
   with patch.object(m,'api',return_value={'object':{'sha':SHA}}),patch.object(m,'dispatch',side_effect=AssertionError('must not deploy')):
    r=m.project_run('gpoint',Path(temporary),SimpleNamespace(mode='production',approved_gpoint='b'*40))
   self.assertEqual(r['status'],'FAILED');self.assertIn('owner-approved exact SHA',r['error'])
 def test_failed_latest_prepare_stops_before_dispatch(self):
  with tempfile.TemporaryDirectory() as temporary:
   def api(path):
    if path.endswith('git/ref/heads/main'):return {'object':{'sha':SHA}}
    return {'workflow_runs':[dict(id=1,head_sha=SHA,head_branch='main',event='push',run_number=1,status='completed',conclusion='failure')]}
   with patch.object(m,'api',api),patch.object(m,'dispatch',side_effect=AssertionError('must not dispatch')):
    result=m.project_run('gpoint',Path(temporary),SimpleNamespace(timeout_seconds=60,prepare_missing=True))
   self.assertEqual(result['status'],'FAILED');self.assertIn('Latest Prepare failed',result['error'])
if __name__=='__main__':unittest.main()
