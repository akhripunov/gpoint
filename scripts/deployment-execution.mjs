export function deploymentResultStatus({evidenceStatus, mode, authorizationConsumed = false,
  deploymentExists = false, installationVerified = false, successStatusConfirmed = false}) {
  if (evidenceStatus === 'COMPLETE' && mode === 'task-scoped-auto' && !authorizationConsumed)
    return 'INSTALLATION_UNCONFIRMED';
  if (evidenceStatus === 'COMPLETE' && deploymentExists && !installationVerified)
    return 'INSTALLATION_UNCONFIRMED';
  if (deploymentExists && installationVerified && !successStatusConfirmed)
    return 'INSTALLATION_UNCONFIRMED';
  return evidenceStatus;
}

/**
 * Run one production Apply and publish its status only after independent readback.
 * All effects are injected so the state machine can be exercised without GitHub,
 * VPS, database, or release tooling access.
 */
export async function executeProductionDeployment({createDeployment, apply, inspectVps,
  verifyInstalled, afterVerified = async () => {}, publishStatus, identity, candidateSha, logUrl}) {
  let deployment = null;
  let applyStarted = false;
  let installationVerified = false;
  let successStatusStarted = false;
  let applyResult;
  let live = null;
  let failure = null;

  try {
    const created = await createDeployment();
    deployment = created?.deployment ?? created;
    applyStarted = true;
    applyResult = await apply(deployment);
    if (applyResult?.status !== 0) {
      failure = {stage: 'POST_APPLY_READBACK', executionStatus: 'FAILED',
        diagnostic: 'Apply returned a nonzero status'};
    }
  } catch (error) {
    deployment ??= error?.deployment ?? null;
    failure = {stage: applyStarted ? 'POST_APPLY_READBACK_OR_STATUS' : 'PRE_APPLY',
      executionStatus: 'FAILED', diagnostic: String(error?.message ?? error)};
  }

  if (applyStarted) {
    try {
      live = await inspectVps();
      if (!failure) {
        await verifyInstalled(live);
        installationVerified = true;
        await afterVerified(live);
      }
    } catch (error) {
      failure ??= {stage: 'POST_APPLY_READBACK', executionStatus: 'FAILED',
        diagnostic: String(error?.message ?? error)};
    }
  }

  if (deployment) {
    if (!failure && installationVerified) {
      successStatusStarted = true;
      try {
        await publishStatus(deployment, identity, {state: 'success',
          description: `stage=complete sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`,
          logUrl});
      } catch (error) {
        // The client owns same-ID status readback/retry. If it remains uncertain,
        // do not append a contradictory error status after verified installation.
        failure = {stage: 'STATUS_UNCONFIRMED', executionStatus: 'UNCONFIRMED',
          diagnostic: String(error?.message ?? error)};
      }
    } else {
      try {
        await publishStatus(deployment, identity, {state: applyStarted ? 'error' : 'failure',
          description: `stage=${applyStarted ? 'post_apply' : 'pre_apply'} sha=${candidateSha.slice(0, 12)} run=${identity.runId}/${identity.runAttempt}`,
          logUrl});
      } catch (error) {
        failure ??= {stage: 'STATUS_UNCONFIRMED', executionStatus: 'UNCONFIRMED',
          diagnostic: String(error?.message ?? error)};
      }
    }
  }

  return {deployment, applyStarted, applyResult, live, installationVerified,
    successStatusStarted, releaseCode: failure ? 1 : 0, failure};
}
