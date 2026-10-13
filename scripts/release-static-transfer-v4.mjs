import fs from 'node:fs';
import path from 'node:path';

const SHA = /^[0-9a-f]{40}$/;
function exactSha(value) {
  if (!SHA.test(value ?? '')) throw new Error('untrusted exact static release SHA');
  return value;
}

function transactionOutput(output, phase, candidate) {
  const marker = 'GPOINT_V4_STATIC_TRANSACTION=';
  const line = String(output).split(/\r?\n/).find(s => s.startsWith(marker));
  if (!line) throw new Error('V4 remote static transaction evidence missing');
  let evidence;
  try { evidence = JSON.parse(line.slice(marker.length)); }
  catch { throw new Error('malformed static transaction evidence'); }
  if (evidence.candidateSha !== candidate ||
      (phase && evidence.phase !== phase && evidence.status !== phase))
    throw new Error('unexpected static transaction identity/state');
  return evidence;
}

export function staticScriptPaths(root) {
  return ['release_static_executor_v4.py','release_static_exchange_v4.py','release_ui_artifact_v4.py']
    .map(name => ({ name, local: path.join(root, 'scripts', name) }));
}

/** Called only from canonical V3 runApply while owning its production lock. */
export function applyStaticArtifactV4({
  candidate, baseline, bundle, images, sourceRoot, upload, runRemote, healthCheck,
  nativeProof, onSwitchStart = () => {},
}) {
  exactSha(candidate); exactSha(baseline);
  if (candidate === baseline || bundle?.candidateSha !== candidate ||
      bundle?.productionBaselineSha !== baseline || bundle?.staticOnly !== true ||
      !fs.existsSync(bundle.archive) ||
      !['app','agata','gsales'].every(role => SHA.test(images?.[role]?.sourceSha ?? '')))
    throw new Error('static apply lacks exact immutable receipt and inherited roles');
  if (typeof runRemote !== 'function' || typeof upload !== 'function' ||
      typeof healthCheck !== 'function' || typeof nativeProof !== 'function')
    throw new Error('static apply requires canonical privileged adapters');

  const tmp = '/tmp/gpoint-v4-static-' + candidate;
  const executor = tmp + '/release_static_executor_v4.py';
  const archiveRemote = '/tmp/gpoint-v4-static-' + candidate + '.zip';
  const base = 'python3 ' + executor + ' --baseline ' + baseline + ' --candidate ' + candidate;
  // argparse allows options anywhere, but place command first for auditability.
  const command = (phase, extra = '') =>
    'python3 ' + executor + ' ' + phase + ' --baseline ' + baseline +
      ' --candidate ' + candidate + (extra ? ' ' + extra : '');

  let mutationStarted = false;
  let rollbackProven = false;
  const elapsed = {};
  const timed = (label, operation) => {
    const before = Date.now();
    try { return operation(); }
    finally { elapsed[label] = (Date.now() - before) / 1000; }
  };
  try {
    timed('stage', () => {
      runRemote('set -eu; umask 077; mkdir -p ' + tmp + '; chmod 700 ' + tmp);
      for (const script of staticScriptPaths(sourceRoot)) {
        if (!fs.statSync(script.local).isFile() || fs.lstatSync(script.local).isSymbolicLink())
          throw new Error('untrusted static executor source');
        upload(script.local, tmp + '/' + script.name);
      }
      upload(bundle.archive, archiveRemote);
      const receipt = runRemote(command('stage', '--archive ' + archiveRemote));
      transactionOutput(receipt, 'STAGED', candidate);
    });

    // Atomic RENAME_EXCHANGE switches two real same-filesystem directories.
    onSwitchStart();
    mutationStarted = true;
    timed('switch', () => {
      const receipt = runRemote(command('switch'));
      transactionOutput(receipt, 'SWITCHED', candidate);
    });
    timed('external_health_before_env', () => healthCheck(candidate));
    timed('native_after_switch', () => nativeProof(images));

    // Static-only release deliberately does not modify RELEASE_SHA or
    // production.env: runtime source identity remains immutable and reusable.
    timed('runtime_env_unchanged', () => {
      const expected = images.app.sourceSha;
      const observed = runRemote(
        "set -eu; test \"$(sed -n 's/^RELEASE_SHA=//p' /opt/gpoint/env/production.env | head -1)\" = '" +
        expected + "'; echo GPOINT_V4_RUNTIME_ENV_UNCHANGED=PASS");
      if (!observed.includes('GPOINT_V4_RUNTIME_ENV_UNCHANGED=PASS'))
        throw new Error('static-only release altered runtime identity');
    });
    timed('external_health_final', () => healthCheck(candidate));
    timed('final_native_parity', () => nativeProof(images));
    timed('durable_transaction_complete', () => {
      const receipt = runRemote(command('complete'));
      transactionOutput(receipt, 'COMPLETE', candidate);
    });
    return { status: 'COMPLETE', candidateSha: candidate, previousProductionSha: baseline,
      inheritedRoles: ['app','agata','gsales'], databaseMutationOccurred: false,
      containersRestarted: false, timings: elapsed };
  } catch (original) {
    if (mutationStarted) {
      try {
        const rolled = runRemote(command('rollback'));
        transactionOutput(rolled, 'ROLLED_BACK', candidate);
        healthCheck(baseline);
        nativeProof(images);
        rollbackProven = true;
      } catch (rollbackError) {
        original.rollbackFailure = rollbackError.message;
      }
    }
    original.switchStarted = mutationStarted;
    original.rollbackProven = rollbackProven;
    throw original;
  }
}
