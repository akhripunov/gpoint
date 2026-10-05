// Dependency observations never change server outcome and never invoke apply/rollback.
export function workerOperationalStatus(status, {nowMs=Date.now(), maxAgeMs=90_000}={}) {
  if (!status || typeof status !== 'object') return {status:'NOT_VERIFIED',reasons:['STATUS_NOT_VERIFIED']};
  if (status.probeFailure) return {status:status.probeFailure==='TIMEOUT'?'UNAVAILABLE':'NOT_VERIFIED',reasons:[status.probeFailure==='TIMEOUT'?'PROBE_TIMEOUT':'PROBE_NOT_VERIFIED']};
  const timestamp = value => Number.isFinite(Date.parse(value ?? '')) ? new Date(value).toISOString() : null;
  const evidence = {enabled:status.enabled===true,lastPollAt:timestamp(status.last_poll_at),lastHeartbeatAt:timestamp(status.last_heartbeat_at)};
  if (status.enabled !== true) return {status:'UNAVAILABLE',reasons:['WORKER_NOT_ENABLED'],evidence};
  if (status.queue_connectivity !== 'reachable') {
    const queue=String(status.queue_connectivity??'');
    const reason=/auth|401|403/i.test(queue)?'AUTH_OR_STATUS_UNAVAILABLE':/timeout/i.test(queue)?'GATEWAY_TIMEOUT':/worker_target_unavailable/.test(queue)?'TARGET_UNAVAILABLE':'QUEUE_UNAVAILABLE';
    return {status:'UNAVAILABLE',reasons:[reason],evidence};
  }
  const reasons=[];
  for(const field of ['last_poll_at','last_heartbeat_at']) {
    const age=nowMs-Date.parse(status[field]??'');
    if(!Number.isFinite(age)||age < -5000||age > maxAgeMs) reasons.push(field==='last_poll_at'?'POLL_STALE':'HEARTBEAT_STALE');
  }
  return {status:reasons.length?'DEGRADED':'HEALTHY',reasons,evidence};
}
export function observeDeploymentReadiness(serverDeployStatus, probe, options={}) {
  let dependencies;
  try {
    const values=probe();
    dependencies=Object.fromEntries(['GPOINT','GSALES'].map(lane=>[lane,workerOperationalStatus(values?.[lane],options)]));
    if(options.gsalesInferenceReady === false && dependencies.GSALES.status==='HEALTHY') dependencies.GSALES={...dependencies.GSALES,status:'DEGRADED',reasons:['CORE_INFERENCE_NOT_READY']};
  } catch(error) {
    const timeout=error?.code==='ETIMEDOUT'||error?.killed===true;
    dependencies=Object.fromEntries(['GPOINT','GSALES'].map(lane=>[lane,{status:timeout?'UNAVAILABLE':'NOT_VERIFIED',reasons:[timeout?'PROBE_TIMEOUT':'PROBE_NOT_VERIFIED']}]));
  }
  const statuses=Object.values(dependencies).map(value=>value.status);
  const aggregateStatus=['UNAVAILABLE','DEGRADED','NOT_VERIFIED','HEALTHY'].find(value=>statuses.includes(value));
  return {serverDeployStatus,workerOperationalStatus:aggregateStatus,workerDependencies:dependencies,
    overallServiceReadiness:serverDeployStatus==='COMPLETE'&&aggregateStatus==='HEALTHY'?'READY':'DEGRADED',
    dependencyCheckedAt:new Date(options.nowMs??Date.now()).toISOString(),inferenceAcceptance:'NOT_VERIFIED'};
}
export function dependencyRecheckState(current,candidate,readiness) {
  if(current.currentProductionSha!==candidate || current.lastReleaseStatus!=='COMPLETE' || readiness.serverDeployStatus!=='COMPLETE' || current.recoveryRequired===true) throw new Error('Dependency recheck requires the exact completed server release');
  const {serverDeployStatus,workerOperationalStatus,workerDependencies,overallServiceReadiness,dependencyCheckedAt}=readiness;
  return {...current,serverDeployStatus,workerOperationalStatus,workerDependencies,overallServiceReadiness,dependencyCheckedAt};
}

// A failed observation in one lane never erases another lane's evidence.
export function collectWorkerDependencies(probes) {
  return Object.fromEntries(['GPOINT','GSALES'].map(lane => {
    try { return [lane,probes[lane]()]; }
    catch(error) { return [lane,{probeFailure:error?.code==='ETIMEDOUT'||error?.killed===true?'TIMEOUT':'NOT_VERIFIED'}]; }
  }));
}
