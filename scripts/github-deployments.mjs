const REPOSITORY = 'akhripunov/gpoint';
const API = 'https://api.github.com';
export const PRODUCTION_ENVIRONMENT = 'production';
export const PRODUCTION_DEPLOYMENT_TASK = 'gpoint-production-deploy';
export const DEPLOYMENT_SCHEMA = 'gpoint-deployment/v1';

const SHA40 = /^[a-f0-9]{40}$/;
const POSITIVE_ID = /^[1-9][0-9]*$/;

export class DeploymentApiError extends Error {
  constructor(message, {status = null, cause = undefined} = {}) {
    super(message, {cause});
    this.name = 'DeploymentApiError';
    this.status = status;
  }
}

export function selectTrustedPrepareCheck(checkRuns, {candidateSha, prepareRunId} = {}) {
  requireThat(Array.isArray(checkRuns) && SHA40.test(candidateSha ?? '') && POSITIVE_ID.test(String(prepareRunId ?? '')),
    'exact candidate SHA, trusted Prepare run ID, and check-run inventory are required');
  const named = checkRuns.filter(check => check?.name === 'prepare');
  requireThat(named.length === 1, named.length ? 'Prepare check context is ambiguous' : 'Prepare check context is unavailable');
  const check = named[0];
  requireThat(check.head_sha === candidateSha && check.status === 'completed' && check.conclusion === 'success'
    && check.app?.slug === 'github-actions',
  'Prepare check context is not successful, exact-SHA, or from GitHub Actions');
  let details;
  try { details = new URL(check.details_url); }
  catch { throw new DeploymentApiError('Prepare check run details URL is invalid'); }
  const route = /^\/akhripunov\/gpoint\/actions\/runs\/([1-9][0-9]*)\/job\/([1-9][0-9]*)$/;
  const match = route.exec(details.pathname);
  requireThat(details.protocol === 'https:' && details.hostname === 'github.com' && !details.port
    && !details.username && !details.password && !details.search && !details.hash
    && match?.[1] === String(prepareRunId),
  'Prepare check is not bound to the trusted GitHub Actions run');
  return check;
}

function requireThat(condition, message) {
  if (!condition) throw new DeploymentApiError(message);
}

function safeJson(response, text) {
  if (!text) return null;
  try { return JSON.parse(text); }
  catch { throw new DeploymentApiError(`GitHub Deployments returned invalid JSON (HTTP ${response.status})`); }
}

function parsePayload(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return null; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function validateIdentity(identity) {
  requireThat(SHA40.test(identity?.candidateSha ?? ''), 'exact candidate SHA is required for a production Deployment');
  for (const key of ['runId', 'runAttempt', 'prepareRunId', 'prepareRunAttempt']) {
    requireThat(POSITIVE_ID.test(String(identity?.[key] ?? '')), `exact ${key} is required for a production Deployment`);
  }
  requireThat(typeof identity.prepareReceiptDigest === 'string' && /^sha256:[a-f0-9]{64}$/.test(identity.prepareReceiptDigest),
    'exact GHCR Prepare receipt digest is required');
  requireThat(typeof identity.prepareReceiptSha256 === 'string' && /^[a-f0-9]{64}$/.test(identity.prepareReceiptSha256),
    'GHCR Prepare receipt content checksum is required');
  requireThat(identity.imageDigests && ['app', 'agata', 'gsales'].every(role =>
    typeof identity.imageDigests[role] === 'string' && /^sha256:[a-f0-9]{64}$/.test(identity.imageDigests[role])),
  'exact GHCR image digest set is required');
  const route = identity.route ?? 'FULL_RELEASE';
  if (route === 'STATIC_UI_ONLY') {
    requireThat(/^sha256:[a-f0-9]{64}$/.test(identity.staticArtifactDigest ?? '')
      && /^[a-f0-9]{64}$/.test(identity.staticArtifactContentSha256 ?? ''),
    'STATIC_UI_ONLY Deployment requires its exact static OCI artifact identity');
  } else {
    requireThat(route === 'FULL_RELEASE', 'production Deployment route is invalid');
  }
  requireThat(['EXACT_SHA', 'TASK_SCOPED_AUTO'].includes(identity.authorizationMode),
    'production Deployment authorization mode is invalid');
  if (identity.authorizationMode === 'TASK_SCOPED_AUTO')
    requireThat(typeof identity.authorizationId === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/.test(identity.authorizationId),
      'TASK_SCOPED_AUTO Deployment requires its exact authorization ID');
  return identity;
}

export function deploymentPayload(identity) {
  validateIdentity(identity);
  const route = identity.route ?? 'FULL_RELEASE';
  return {
    schema: DEPLOYMENT_SCHEMA,
    repository: REPOSITORY,
    candidateSha: identity.candidateSha,
    workflow: {runId: String(identity.runId), attempt: String(identity.runAttempt),
      url: identity.logUrl},
    prepare: {runId: String(identity.prepareRunId), attempt: String(identity.prepareRunAttempt),
      receiptDigest: identity.prepareReceiptDigest, receiptSha256: identity.prepareReceiptSha256,
      imageDigests: identity.imageDigests,
      ...(route === 'STATIC_UI_ONLY' ? {staticArtifact: {
        digest: identity.staticArtifactDigest, contentSha256: identity.staticArtifactContentSha256}} : {})},
    route,
    authorization: {mode: identity.authorizationMode, id: identity.authorizationId ?? null},
  };
}

export function deploymentIdentity(deployment) {
  const payload = parsePayload(deployment?.payload);
  requireThat(payload?.schema === DEPLOYMENT_SCHEMA, 'Deployment identity payload is unavailable');
  return {candidateSha: deployment.sha, runId: String(payload.workflow.runId),
    runAttempt: String(payload.workflow.attempt), prepareRunId: String(payload.prepare.runId),
    prepareRunAttempt: String(payload.prepare.attempt), prepareReceiptDigest: payload.prepare.receiptDigest,
    prepareReceiptSha256: payload.prepare.receiptSha256, imageDigests: payload.prepare.imageDigests,
    route: payload.route,
    staticArtifactDigest: payload.prepare.staticArtifact?.digest,
    staticArtifactContentSha256: payload.prepare.staticArtifact?.contentSha256,
    authorizationMode: payload.authorization.mode, authorizationId: payload.authorization.id,
    logUrl: payload.workflow.url};
}

function isSameAttempt(deployment, identity) {
  const payload = parsePayload(deployment?.payload);
  return payload?.schema === DEPLOYMENT_SCHEMA && payload.repository === REPOSITORY
    && payload.candidateSha === identity.candidateSha
    && String(payload.workflow?.runId) === String(identity.runId)
    && String(payload.workflow?.attempt) === String(identity.runAttempt);
}

function validateDeploymentRecord(deployment, sha = null, {allowLegacyPayload = false} = {}) {
  requireThat(deployment && Number.isSafeInteger(deployment.id) && deployment.id > 0,
    'GitHub Deployment record has no valid ID');
  requireThat(deployment.environment === PRODUCTION_ENVIRONMENT
    && deployment.production_environment === true && deployment.transient_environment === false,
  'GitHub Deployment environment flags do not identify persistent production');
  requireThat(deployment.task === PRODUCTION_DEPLOYMENT_TASK,
    'GitHub Deployment task does not identify a GPoint production installation');
  requireThat(SHA40.test(deployment.sha ?? '') && (!sha || deployment.sha === sha) && deployment.ref === deployment.sha,
    'GitHub Deployment does not point to its exact full candidate SHA');
  const payload = parsePayload(deployment.payload);
  if (allowLegacyPayload && payload?.schema !== DEPLOYMENT_SCHEMA) return payload;
  requireThat(payload?.schema === DEPLOYMENT_SCHEMA && payload.repository === REPOSITORY
    && payload.candidateSha === deployment.sha && payload.workflow?.runId && payload.workflow?.attempt
    && payload.prepare?.receiptDigest && payload.prepare?.receiptSha256 && payload.prepare?.imageDigests
    && ['EXACT_SHA', 'TASK_SCOPED_AUTO'].includes(payload.authorization?.mode)
    && (payload.authorization.mode !== 'TASK_SCOPED_AUTO' ||
      /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/.test(payload.authorization.id ?? ''))
    && ['FULL_RELEASE', 'STATIC_UI_ONLY'].includes(payload.route)
    && (payload.route !== 'STATIC_UI_ONLY' || /^sha256:[a-f0-9]{64}$/.test(payload.prepare?.staticArtifact?.digest ?? '')
      && /^[a-f0-9]{64}$/.test(payload.prepare?.staticArtifact?.contentSha256 ?? '')),
  'GitHub Deployment provenance payload is incomplete or mismatched');
  return payload;
}

function deploymentRunIdentity(deployment) {
  const payload = parsePayload(deployment?.payload);
  return payload?.workflow?.runId && payload?.workflow?.attempt
    ? `${payload.workflow.runId}/${payload.workflow.attempt}` : null;
}

function isSafePreApplyFailure(status) {
  return status?.state === 'failure' && /(?:^|\s)stage=pre_apply(?:\s|$)/.test(status.description ?? '');
}

export function createGitHubDeploymentsClient({token, fetchImpl = globalThis.fetch, apiBase = API,
  maxPages = 100, timeoutMs = 30_000, sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))} = {}) {
  requireThat(typeof token === 'string' && token.length > 0, 'GITHUB_TOKEN is required for Deployments API access');
  requireThat(typeof fetchImpl === 'function', 'Fetch implementation is unavailable');
  const base = apiBase.replace(/\/$/, '');

  async function request(method, route, body = undefined) {
    let response;
    try {
      response = await fetchImpl(`${base}/repos/${REPOSITORY}/${route}`, {
        method,
        headers: {Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28', ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      throw new DeploymentApiError(`GitHub Deployments ${method} request failed`, {cause: error});
    }
    const text = response.status === 204 ? '' : await response.text();
    const data = safeJson(response, text);
    if (!response.ok) throw new DeploymentApiError(`GitHub Deployments ${method} failed (HTTP ${response.status})`,
      {status: response.status});
    return data;
  }

  async function listDeployments() {
    const values = [];
    for (let page = 1; page <= maxPages; page += 1) {
      const rows = await request('GET', `deployments?environment=${PRODUCTION_ENVIRONMENT}&task=${PRODUCTION_DEPLOYMENT_TASK}&per_page=100&page=${page}`);
      requireThat(Array.isArray(rows), 'GitHub Deployments list response is not an array');
      values.push(...rows);
      if (rows.length < 100) return values;
    }
    throw new DeploymentApiError('GitHub Deployments list exceeded its pagination limit');
  }

  async function listStatuses(deploymentId) {
    requireThat(Number.isSafeInteger(deploymentId) && deploymentId > 0, 'Deployment ID is invalid');
    const values = [];
    for (let page = 1; page <= maxPages; page += 1) {
      const rows = await request('GET', `deployments/${deploymentId}/statuses?per_page=100&page=${page}`);
      requireThat(Array.isArray(rows), 'GitHub Deployment statuses response is not an array');
      values.push(...rows);
      if (rows.length < 100) return values;
    }
    throw new DeploymentApiError('GitHub Deployment status list exceeded its pagination limit');
  }

  async function getDeployment(deploymentId) {
    requireThat(Number.isSafeInteger(deploymentId) && deploymentId > 0, 'Deployment ID is invalid');
    const deployment = await request('GET', `deployments/${deploymentId}`);
    validateDeploymentRecord(deployment);
    return deployment;
  }

  async function findDeploymentForRun(runId) {
    requireThat(POSITIVE_ID.test(String(runId ?? '')), 'workflow run ID is invalid');
    const matches = [];
    for (const item of await listDeployments()) {
      const payload = parsePayload(item.payload);
      // Historical deployments predate the evidence schema. Ignore them while
      // scanning; a record claiming this run must still validate completely.
      if (String(payload?.workflow?.runId ?? '') !== String(runId)) continue;
      validateDeploymentRecord(item);
      matches.push(item);
    }
    if (matches.length > 1) throw new DeploymentApiError('Multiple production Deployments exist for this workflow run');
    return matches[0] ?? null;
  }

  async function latestStatus(deployment) {
    const statuses = await listStatuses(deployment.id);
    if (!statuses.length) throw new DeploymentApiError(`GitHub Deployment ${deployment.id} has no readable current status`);
    for (const status of statuses) {
      if (!status || !['queued', 'pending', 'in_progress', 'success', 'failure', 'error', 'inactive'].includes(status.state))
        throw new DeploymentApiError(`GitHub Deployment ${deployment.id} has an invalid status`);
      if (!status.created_at || !Number.isFinite(Date.parse(status.created_at)))
        throw new DeploymentApiError(`GitHub Deployment ${deployment.id} status timestamp is invalid`);
    }
    const latestAt = Math.max(...statuses.map(status => Date.parse(status.created_at)));
    const newest = statuses.filter(status => Date.parse(status.created_at) === latestAt);
    const signature = status => JSON.stringify([status.state, status.description ?? null,
      status.log_url ?? null, status.environment_url ?? null]);
    if (new Set(newest.map(signature)).size !== 1)
      throw new DeploymentApiError(`GitHub Deployment ${deployment.id} has conflicting statuses at the latest timestamp`);
    return newest[0];
  }

  async function readProductionBaselineFrom(deployments) {
    if (!deployments.length) return {historyExists: false, baselineSha: null, deploymentId: null,
      status: null, preApplyFailure: false};
    for (const deployment of deployments) validateDeploymentRecord(deployment, null, {allowLegacyPayload: true});
    const ordered = [...deployments].sort((a, b) => Date.parse(b.created_at ?? '') - Date.parse(a.created_at ?? ''));
    const observedStatuses = new Map();
    for (const deployment of ordered) {
      const status = await latestStatus(deployment);
      observedStatuses.set(deployment.id, status);
    }
    if (!Number.isFinite(Date.parse(ordered[0]?.created_at ?? '')))
      throw new DeploymentApiError('GitHub Deployment creation time is invalid');
    if (ordered.length > 1 && Date.parse(ordered[0].created_at) === Date.parse(ordered[1].created_at)
        && ordered[0].id !== ordered[1].id)
      throw new DeploymentApiError('GitHub production Deployment order is ambiguous');
    const newest = ordered[0];
    const status = observedStatuses.get(newest.id);
    if (['queued', 'pending', 'in_progress'].includes(status.state) || status.state === 'error')
      throw new DeploymentApiError(`GitHub Deployment ${newest.id} is ${status.state}; fresh Deploy is blocked pending read-only verification`);
    if (status.state === 'success') {
      const olderActive = ordered.slice(1).find(item => ['queued', 'pending', 'in_progress'].includes(observedStatuses.get(item.id).state));
      if (olderActive) throw new DeploymentApiError(`Prior GPoint Deployment ${olderActive.id} is still active; fresh Deploy is blocked`);
      return {historyExists: true, baselineSha: newest.sha,
      deploymentId: newest.id, status, preApplyFailure: false};
    }
    if (!isSafePreApplyFailure(status))
      throw new DeploymentApiError(`Latest GPoint Deployment ${newest.id} failed outside the verified pre-Apply stage`);
    const previousSuccess = ordered.slice(1);
    for (const deployment of previousSuccess) {
      const previousStatus = observedStatuses.get(deployment.id);
      if (previousStatus.state === 'success') return {historyExists: true, baselineSha: deployment.sha,
        deploymentId: deployment.id, status: previousStatus, failedDeploymentId: newest.id, preApplyFailure: true};
      if (['queued', 'pending', 'in_progress', 'error'].includes(previousStatus.state))
        throw new DeploymentApiError(`Prior GPoint Deployment ${deployment.id} is ${previousStatus.state}; fresh Deploy is blocked`);
      if (!isSafePreApplyFailure(previousStatus))
        throw new DeploymentApiError(`Prior GPoint Deployment ${deployment.id} has an unresolved result`);
    }
    return {historyExists: true, baselineSha: null, deploymentId: null, status,
      failedDeploymentId: newest.id, preApplyFailure: true};
  }

  async function readProductionBaseline() {
    return readProductionBaselineFrom(await listDeployments());
  }

  async function readLatestSuccessfulProductionDeployment() {
    const baseline = await readProductionBaseline();
    if (!baseline.historyExists || baseline.status?.state !== 'success' ||
        !Number.isSafeInteger(baseline.deploymentId) || !SHA40.test(baseline.baselineSha ?? ''))
      throw new DeploymentApiError('No unambiguous successful production Deployment is available');
    const deployment = await getDeployment(baseline.deploymentId);
    const status = await latestStatus(deployment);
    if (deployment.sha !== baseline.baselineSha || status.state !== 'success')
      throw new DeploymentApiError('Latest production Deployment changed or is no longer successful');
    return {deployment, status, identity: deploymentIdentity(deployment)};
  }

  async function readSuccessfulProductionDeploymentForSha(candidateSha, route = 'FULL_RELEASE', currentAttempt = null) {
    requireThat(SHA40.test(candidateSha ?? '') && ['FULL_RELEASE', 'STATIC_UI_ONLY'].includes(route),
      'exact SHA and production route are required for Deployment read-back');
    const snapshot = baseline => JSON.stringify({historyExists: baseline.historyExists,
      baselineSha: baseline.baselineSha, deploymentId: baseline.deploymentId,
      state: baseline.status?.state ?? null, createdAt: baseline.status?.created_at ?? null,
      description: baseline.status?.description ?? null, logUrl: baseline.status?.log_url ?? null});
    const normalize = value => Array.isArray(value) ? value.map(normalize)
      : value && typeof value === 'object'
        ? Object.fromEntries(Object.keys(value).sort().map(key => [key, normalize(value[key])])) : value;
    const stableIdentity = value => JSON.stringify(normalize(value));
    const parentSnapshot = async () => {
      const rows = await listDeployments();
      let baselineRows = rows;
      let own = null;
      let ownStatus = null;
      if (currentAttempt !== null) {
        requireThat(currentAttempt && Number.isSafeInteger(currentAttempt.deploymentId)
          && currentAttempt.deploymentId > 0 && currentAttempt.identity && typeof currentAttempt.identity === 'object',
        'current Apply Deployment context is invalid');
        deploymentPayload(currentAttempt.identity);
        const ownRows = rows.filter(item => item?.id === currentAttempt.deploymentId);
        requireThat(ownRows.length === 1, 'current Apply Deployment is missing or ambiguous');
        const listedOwn = ownRows[0];
        validateDeploymentRecord(listedOwn, currentAttempt.identity.candidateSha);
        const readBackOwn = await getDeployment(currentAttempt.deploymentId);
        requireThat(readBackOwn.id === currentAttempt.deploymentId && readBackOwn.created_at === listedOwn.created_at
          && readBackOwn.sha === listedOwn.sha
          && stableIdentity(deploymentIdentity(readBackOwn)) === stableIdentity(deploymentIdentity(listedOwn)),
        'current Apply Deployment ID read-back does not match its listed record');
        own = readBackOwn;
        requireThat(stableIdentity(deploymentIdentity(own)) === stableIdentity(currentAttempt.identity),
          'current Apply Deployment identity changed');
        ownStatus = await latestStatus(own);
        const expectedDescription = `stage=pre_apply sha=${currentAttempt.identity.candidateSha.slice(0, 12)} ` +
          `run=${currentAttempt.identity.runId}/${currentAttempt.identity.runAttempt}`;
        requireThat(ownStatus.state === 'in_progress' && ownStatus.description === expectedDescription
          && ownStatus.log_url === currentAttempt.identity.logUrl,
        'current Apply Deployment is not in the exact pre-Apply in-progress state');
        baselineRows = rows.filter(item => item?.id !== currentAttempt.deploymentId);
      }
      const baseline = await readProductionBaselineFrom(baselineRows);
      const ownSnapshot = own ? stableIdentity({id: own.id, sha: own.sha, created_at: own.created_at,
        identity: deploymentIdentity(own), state: ownStatus.state, description: ownStatus.description,
        log_url: ownStatus.log_url, created_at_status: ownStatus.created_at}) : null;
      const rowsSnapshot = currentAttempt ? stableIdentity(rows.map(item => ({id: item.id,
        sha: item.sha, ref: item.ref, task: item.task, created_at: item.created_at, payload: item.payload}))
        .sort((left, right) => left.id - right.id)) : null;
      return {baseline, rows, ownSnapshot, rowsSnapshot};
    };
    const before = await parentSnapshot();
    const matchingRoute = rows => {
      const matches = [];
      for (const item of rows) {
        validateDeploymentRecord(item, null, {allowLegacyPayload: true});
        if (currentAttempt && item.id === currentAttempt.deploymentId) continue;
        if (item.sha !== candidateSha) continue;
        if (validateDeploymentRecord(item, candidateSha).route === route) matches.push(item);
      }
      return matches;
    };
    const matchesBefore = matchingRoute(before.rows);
    const successful = [];
    const statusesBefore = [];
    for (const item of matchesBefore) {
      const status = await latestStatus(item);
      statusesBefore.push({id: item.id, state: status.state, description: status.description ?? null,
        log_url: status.log_url ?? null, created_at: status.created_at});
      if (status.state === 'success') {
        const identity = deploymentIdentity(item);
        const expectedDescription = `stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`;
        if (status.description !== expectedDescription || status.log_url !== identity.logUrl)
          throw new DeploymentApiError(`Exact production Deployment ${item.id} success status is not bound to its run attempt`);
        successful.push({item, status});
      } else if (['pending', 'queued', 'in_progress', 'error'].includes(status.state) ||
          !isSafePreApplyFailure(status)) {
        throw new DeploymentApiError(`Exact production Deployment ${item.id} is ${status.state} or unresolved`);
      }
    }
    if (successful.length > 1)
      throw new DeploymentApiError(`Successful ${route} Deployments for the exact SHA are ambiguous`);
    if (successful.length === 0) {
      if (currentAttempt) {
        const after = await parentSnapshot();
        const matchesAfter = matchingRoute(after.rows);
        const statusesAfter = [];
        for (const item of matchesAfter) {
          const status = await latestStatus(item);
          statusesAfter.push({id: item.id, state: status.state, description: status.description ?? null,
            log_url: status.log_url ?? null, created_at: status.created_at});
        }
        if (snapshot(before.baseline) !== snapshot(after.baseline) || before.ownSnapshot !== after.ownSnapshot ||
            before.rowsSnapshot !== after.rowsSnapshot ||
            JSON.stringify(matchesBefore.map(value => value.id).sort((a, b) => a - b)) !==
              JSON.stringify(matchesAfter.map(value => value.id).sort((a, b) => a - b)) ||
            stableIdentity(statusesBefore) !== stableIdentity(statusesAfter))
          throw new DeploymentApiError('Production Deployment history changed during exact parent verification');
      }
      throw new DeploymentApiError(`No successful ${route} Deployment exists for the exact SHA`);
    }
    const {item, status} = successful[0];
    const deployment = await getDeployment(item.id);
    const currentStatus = await latestStatus(deployment);
    if (deployment.sha !== candidateSha || deployment.created_at !== item.created_at ||
        JSON.stringify(deploymentIdentity(deployment)) !== JSON.stringify(deploymentIdentity(item)) ||
        JSON.stringify([currentStatus.state, currentStatus.description, currentStatus.log_url, currentStatus.created_at]) !==
        JSON.stringify([status.state, status.description, status.log_url, status.created_at]))
      throw new DeploymentApiError('Exact production Deployment changed during ID-bound read-back');
    const after = await parentSnapshot();
    const matchesAfter = matchingRoute(after.rows);
    const statusesAfter = [];
    for (const matched of matchesAfter) {
      const matchedStatus = await latestStatus(matched);
      statusesAfter.push({id: matched.id, state: matchedStatus.state, description: matchedStatus.description ?? null,
        log_url: matchedStatus.log_url ?? null, created_at: matchedStatus.created_at});
    }
    if (snapshot(before.baseline) !== snapshot(after.baseline) || before.ownSnapshot !== after.ownSnapshot ||
        before.rowsSnapshot !== after.rowsSnapshot ||
        JSON.stringify(matchesBefore.map(value => value.id).sort((a, b) => a - b)) !==
          JSON.stringify(matchesAfter.map(value => value.id).sort((a, b) => a - b)) ||
        stableIdentity(statusesBefore) !== stableIdentity(statusesAfter))
      throw new DeploymentApiError('Production Deployment history changed during exact parent verification');
    return {deployment, status: currentStatus, identity: deploymentIdentity(deployment)};
  }

  async function readBackCreate(identity) {
    const matches = (await listDeployments()).filter(item => isSameAttempt(item, identity));
    if (matches.length > 1) throw new DeploymentApiError('Duplicate GitHub Deployments exist for this workflow run attempt');
    return matches[0] ?? null;
  }

  async function publishStatus(deployment, identity, {state, description, logUrl, environmentUrl = 'https://gpoint.one'} = {}) {
    validateDeploymentRecord(deployment, identity.candidateSha);
    requireThat(deployment.id > 0 && isSameAttempt(deployment, identity),
      'Deployment status target does not match the exact workflow run attempt');
    requireThat(['in_progress', 'success', 'failure', 'error'].includes(state), 'Deployment status state is invalid');
    requireThat(typeof description === 'string' && description.length > 0 && description.length <= 140,
      'Deployment status description must be 1–140 characters');
    requireThat(typeof logUrl === 'string' && logUrl.startsWith('https://github.com/akhripunov/gpoint/actions/runs/'),
      'Deployment status must link to the exact workflow attempt');
    const expected = {state, description, log_url: logUrl, environment_url: environmentUrl};
    const isExact = status => status.state === expected.state && status.description === expected.description
      && status.log_url === expected.log_url && status.environment_url === expected.environment_url;
    try {
      return await request('POST', `deployments/${deployment.id}/statuses`, expected);
    } catch (postError) {
      // A timed-out POST may already have been accepted. Read the original Deployment's
      // statuses before retrying; never create a replacement Deployment or repeat Apply.
      let matches = [];
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const readback = await listStatuses(deployment.id);
        matches = readback.filter(isExact);
        if (matches.length) break;
        if (attempt < 2) await sleep(250 * (attempt + 1));
      }
      if (matches.length === 1) return {...matches[0], readBackAfterAmbiguousPost: true};
      if (matches.length > 1) throw new DeploymentApiError('Duplicate matching Deployment statuses exist after an ambiguous POST');
      try {
        return await request('POST', `deployments/${deployment.id}/statuses`, expected);
      } catch (retryError) {
        throw new DeploymentApiError(`Deployment ${state} status remains unconfirmed after read-back and same-ID retry`,
          {status: retryError.status ?? postError.status, cause: retryError});
      }
    }
  }

  async function beginProductionDeployment(identity, {requiredContexts} = {}) {
    validateIdentity(identity);
    requireThat(Array.isArray(requiredContexts) && requiredContexts.length > 0
      && requiredContexts.every(value => typeof value === 'string' && value.trim().length > 0),
    'required_contexts must explicitly name the verified Prepare status contexts; an empty list would bypass checks');
    const deployments = await listDeployments();
    for (const deployment of deployments) validateDeploymentRecord(deployment);
    const priorRun = deployments.filter(item => {
      const payload = parsePayload(item.payload);
      return String(payload?.workflow?.runId ?? '') === String(identity.runId);
    });
    if (priorRun.length) throw new DeploymentApiError('This workflow run already has a Deployment; refusing to Apply twice across attempts');
    const baseline = await readProductionBaseline();
    const payload = deploymentPayload(identity);
    const body = {ref: identity.candidateSha, task: PRODUCTION_DEPLOYMENT_TASK,
      auto_merge: false, required_contexts: requiredContexts, payload: JSON.stringify(payload),
      environment: PRODUCTION_ENVIRONMENT, description: `GPoint production ${identity.candidateSha.slice(0, 12)}; run ${identity.runId}/${identity.runAttempt}`,
      production_environment: true, transient_environment: false};
    let deployment;
    try {
      deployment = await request('POST', 'deployments', body);
    } catch (postError) {
      // Reconcile an accepted-but-timed-out create with GET. Never POST a second
      // Deployment for the same exact workflow attempt.
      const found = await readBackCreate(identity);
      if (!found) throw postError;
      deployment = found;
    }
    const actualPayload = validateDeploymentRecord(deployment, identity.candidateSha);
    requireThat(isSameAttempt(deployment, identity), 'Created Deployment did not retain the exact run/attempt identity');
    requireThat(JSON.stringify(actualPayload) === JSON.stringify(payload), 'Created Deployment payload read-back does not match the request');
    const logUrl = identity.logUrl;
    let inProgress;
    try {
      inProgress = await publishStatus(deployment, identity, {state: 'in_progress',
        description: `stage=pre_apply sha=${identity.candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`,
        logUrl});
    } catch (error) {
      error.deployment = deployment;
      error.baseline = baseline;
      throw error;
    }
    return {deployment, baseline, inProgress};
  }

  return {listDeployments, listStatuses, latestStatus, getDeployment, findDeploymentForRun, readProductionBaseline,
    readLatestSuccessfulProductionDeployment,
    readSuccessfulProductionDeploymentForSha,
    beginProductionDeployment, publishStatus};
}

export function assertDeploymentMatchesLive(baseline, {runtimeSha, miniappSha, prismaHead} = {}) {
  requireThat(SHA40.test(runtimeSha ?? '') && SHA40.test(miniappSha ?? ''),
    'populated VPS runtime and Mini App source identities are required');
  if (baseline?.baselineSha && (baseline.baselineSha !== runtimeSha || baseline.baselineSha !== miniappSha))
    throw new DeploymentApiError(`GitHub Deployment baseline ${baseline.baselineSha} does not match populated VPS runtime/Mini App ${runtimeSha}/${miniappSha}`);
  requireThat(prismaHead && prismaHead !== 'NONE' && prismaHead !== 'READ_ONLY_DB_INSPECTION_FAILED',
    'populated VPS Prisma migration head is not verified');
  return true;
}
