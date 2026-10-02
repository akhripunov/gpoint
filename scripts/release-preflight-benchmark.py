#!/usr/bin/env python3
"""Coordinate exact-SHA Prepare/preflight and collect all evidence automatically."""
from __future__ import annotations
import argparse,concurrent.futures,datetime,hashlib,json,pathlib,re,subprocess,time,shutil,os
GH=os.environ.get('GH_EXECUTABLE') or shutil.which('gh') or str(pathlib.Path.home()/'.local/bin/gh')
PROJECTS={'gpoint':('gpoint-prepare.yml','gpoint-deploy.yml'),'maxibot':('maxibot-prepare.yml','maxibot-deploy.yml')}
def timestamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def cli(*args):return subprocess.check_output([GH,*args],text=True,timeout=45)
def api(path):return json.loads(cli('api',path))
def seconds(a,b):return (datetime.datetime.fromisoformat(b.replace('Z','+00:00'))-datetime.datetime.fromisoformat(a.replace('Z','+00:00'))).total_seconds()
def select_prepare(runs,sha):
 matches=[r for r in runs if r.get('head_sha')==sha and r.get('head_branch')=='main' and r.get('event') in ('push','workflow_dispatch')]
 return max(matches,key=lambda r:(r['run_number'],r.get('run_attempt',1))) if matches else None

def dispatch(project,workflow,fields):
 command=['workflow','run',workflow,'--repo',f'akhripunov/{project}','--ref','main']
 for key,value in fields.items():command+=['-f',f'{key}={value}']
 output=cli(*command)
 match=re.search(r'/actions/runs/(\d+)',output)
 if not match:raise RuntimeError('Dispatch did not return an exact run ID; refusing ambiguous selection')
 return int(match.group(1))

def wait_run(project,run_id,out,deadline):
 # Watch is a CLI wait; it does not involve repeated agent decisions.
 with (out/f'{run_id}-watch.log').open('w') as log:
  code=subprocess.run([GH,'run','watch',str(run_id),'--repo',f'akhripunov/{project}','--exit-status','--interval','5'],stdout=log,stderr=subprocess.STDOUT,timeout=max(1,deadline-time.monotonic())).returncode
 run=api(f'repos/akhripunov/{project}/actions/runs/{run_id}')
 jobs=api(f'repos/akhripunov/{project}/actions/runs/{run_id}/jobs')['jobs']
 for name,data in [('run',run),('jobs',jobs)]: (out/f'{run_id}-{name}.json').write_text(json.dumps(data,indent=2)+'\n')
 (out/f'{run_id}-full.log').write_text(cli('run','view',str(run_id),'--repo',f'akhripunov/{project}','--log'))
 if code or run['status']!='completed' or run['conclusion']!='success':raise RuntimeError(f'{project} run {run_id} failed; full logs preserved')
 return {'id':run_id,'url':run['html_url'],'created_at':run['created_at'],'finished_at':run['updated_at'],'seconds':seconds(run['created_at'],run['updated_at']),'jobs':[{'name':j['name'],'seconds':seconds(j['started_at'],j['completed_at']),'steps':[{'name':s['name'],'seconds':seconds(s['started_at'],s['completed_at']) if s.get('started_at') and s.get('completed_at') else None,'conclusion':s['conclusion']} for s in j['steps']]} for j in jobs]}

def project_run(project,root,args):
 out=root/project;out.mkdir()
 result={'project':project,'started_at':timestamp(),'status':'FAILED','runs':[]}
 try:
  repo=f'akhripunov/{project}'; sha=api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
  if not re.fullmatch('[0-9a-f]{40}',sha):raise RuntimeError('Invalid exact main SHA')
  result['candidate_sha']=sha
  mode=getattr(args,'mode','preflight')
  if mode=='production' and getattr(args,'approved_'+project,None)!=sha:raise RuntimeError('Production requires owner-approved exact SHA for '+project)
  print(f'{project}: START exact={sha}',flush=True)
  prepare_workflow,deploy_workflow=PROJECTS[project]
  runs=api(f'repos/{repo}/actions/workflows/{prepare_workflow}/runs?branch=main&head_sha={sha}&per_page=100')['workflow_runs']
  latest=select_prepare(runs,sha)
  deadline=time.monotonic()+args.timeout_seconds
  if latest is None:
   if not args.prepare_missing:raise RuntimeError('Exact SHA lacks Prepare; use --prepare-missing')
   prepare_id=dispatch(project,prepare_workflow,{'candidate':sha} if project=='maxibot' else {})
   print(f'{project}: PREPARE dispatched {prepare_id}',flush=True)
   result['runs'].append(wait_run(project,prepare_id,out,deadline))
  elif latest['status']!='completed':
   print(f'{project}: WAIT existing Prepare {latest["id"]}',flush=True)
   result['runs'].append(wait_run(project,latest['id'],out,deadline))
  elif latest['conclusion']!='success':raise RuntimeError('Latest Prepare failed; not silently rerunning')
  else:result['reused_prepare_id']=latest['id']
  fields={'candidate':sha,'mode':mode,'approve_production':'true' if mode=='production' else 'false'}
  if project=='maxibot':fields.update(target_migration=args.maxibot_migration,approve_worker_bootstrap='false')
  # Current main/provenance are checked again by the canonical workflow.
  run_id=dispatch(project,deploy_workflow,fields)
  print(f'{project}: {mode.upper()} dispatched {run_id}',flush=True)
  result['runs'].append(wait_run(project,run_id,out,deadline))
  artifact=f'gpoint-production-result-{run_id}-1' if project=='gpoint' else f'maxibot-release-result-{run_id}-1'
  cli('run','download',str(run_id),'--repo',repo,'-n',artifact,'-D',str(out/'result'))
  receipts=list((out/'result').glob('*.json'))
  if len(receipts)!=1:raise RuntimeError('Missing exact preflight receipt')
  receipt=json.loads(receipts[0].read_text())
  if mode=='production':
   actual=receipt.get('productionShaAfter') if project=='gpoint' else receipt.get('final_runtime_sha')
   if receipt.get('status')!='COMPLETE' or actual!=sha:raise RuntimeError('Production is not technically COMPLETE; reconcile runtime')
  elif project=='gpoint':
   if receipt.get('status')!='PLANNED' or receipt.get('applicationVerification')!='NOT_DEPLOYED' or receipt.get('candidateSha')!=sha:raise RuntimeError('Unexpected GPoint result')
  elif receipt.get('production_changed') is not False or receipt.get('preflight')!='PASS' or receipt.get('candidate_sha')!=sha:raise RuntimeError('Unexpected MaxiBot result')
  after=api(f'repos/{repo}/git/ref/heads/main')['object']['sha']
  if after!=sha:raise RuntimeError('Main changed during preflight')
  result.update(status='PASS',main_unchanged=True,production_applied=mode=='production',receipt=receipt)
 except Exception as exc:result['error']=str(exc)
 result['finished_at']=timestamp();result['elapsed_seconds']=seconds(result['started_at'],result['finished_at'])
 (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
 print(f'{project}: {result["status"]} elapsed={result["elapsed_seconds"]:.1f}s',flush=True)
 return result

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--mode',choices=['preflight','production'],default='preflight')
 p.add_argument('--approved-gpoint',help='Explicit owner-approved exact production SHA')
 p.add_argument('--approved-maxibot',help='Explicit owner-approved exact production SHA')
 p.add_argument('--owner-start',required=True,help='ISO timestamp of owner command, with timezone')
 p.add_argument('--output',required=True)
 p.add_argument('--maxibot-migration',required=True)
 p.add_argument('--prepare-missing',action='store_true')
 p.add_argument('--timeout-seconds',type=int,default=2700)
 args=p.parse_args()
 owner=datetime.datetime.fromisoformat(args.owner_start)
 if owner.tzinfo is None:p.error('owner-start requires timezone')
 root=pathlib.Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
 started=timestamp()
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
  results=list(pool.map(lambda project:project_run(project,root,args),PROJECTS))
 finished=timestamp()
 report={'owner_start':owner.isoformat(),'coordinator_started':started,'finished_at':finished,'owner_elapsed_seconds':seconds(owner.isoformat(),finished),'coordinator_seconds':seconds(started,finished),'status':'PASS' if all(r['status']=='PASS' for r in results) else 'FAIL','projects':results,'scope':'Prepare as needed + read-only preflight; no VPS transfer/import/switch' if args.mode=='preflight' else 'Owner-approved production through canonical workflows'}
 (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
 lines=['# Automated release preflight benchmark','',f'Owner start: {owner.isoformat()}',f'Finished: {finished}',f'Full owner elapsed: {report["owner_elapsed_seconds"]:.1f}s',f'Coordinator duration: {report["coordinator_seconds"]:.1f}s',f'Status: {report["status"]}','',report['scope'],'','| Project | Status | Time |','|---|---|---:|']
 for r in results:lines.append(f'| {r["project"]} | {r["status"]} | {r["elapsed_seconds"]:.1f}s |')
 for r in results:
  lines+=['',f'## {r["project"]}',f'Exact SHA: {r.get("candidate_sha")}',f'Error: {r.get("error","none")}']
  for run in r['runs']:
   lines += [f'- [{run["id"]}]({run["url"]}): {run["seconds"]:.0f}s']
   for job in run['jobs']:
    for step in job['steps']:
     if (step['seconds'] or 0)>=2:lines.append(f'  - {job["name"]} / {step["name"]}: {step["seconds"]:.0f}s')
 (root/'REPORT.md').write_text('\n'.join(lines)+'\n')
 (root/'SHA256SUMS').write_text('\n'.join(hashlib.sha256(f.read_bytes()).hexdigest()+'  '+str(f.relative_to(root)) for f in sorted(root.rglob('*')) if f.is_file() and f.name!='SHA256SUMS')+'\n')
 print(f'ALL={report["status"]} OWNER_SECONDS={report["owner_elapsed_seconds"]:.1f} COORDINATOR_SECONDS={report["coordinator_seconds"]:.1f} REPORT={root / "REPORT.md"}',flush=True)
 return 0 if report['status']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
