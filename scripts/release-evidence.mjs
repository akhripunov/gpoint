/** Add canonical preflight and no-mutation facts to release-result evidence. */
export function withPreflightEvidence(receipt, status, mode) {
  const preflight = status === 'PLANNED' || status === 'PASS' ? 'PASS' : status === 'FAILED' ? 'FAILED' : 'BLOCKED';
  return {
    ...receipt,
    preflight,
    ...(mode === 'preflight' ? {productionChanged: false} : {}),
  };
}
