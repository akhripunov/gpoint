export function assertInstalledWorkerMatches(mode, assets) {
  if (!['preflight', 'production', 'task-scoped-auto'].includes(mode)) {
    throw new Error('Explicit release mode is required');
  }
  if (mode === 'preflight') return;
  if (assets.some(asset => !asset.sourceExists || !asset.installedExists || !asset.matches)) {
    throw new Error('Installed Mac worker differs from candidate. Install the published candidate worker before apply.');
  }
}
