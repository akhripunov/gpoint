import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

export function gsalesProvisionCommand(candidate, approvedSha, baseline) {
  if (![candidate, approvedSha, baseline].every(value => /^[0-9a-f]{40}$/.test(value ?? '')) || candidate !== approvedSha) {
    throw new Error('GSales provisioning requires exact-SHA OWNER approval');
  }
  const files = Object.fromEntries(['gsales-production-provision.py', 'agata-production-migration.py'].map(name =>
    [name, fs.readFileSync(fileURLToPath(new URL(name, import.meta.url)), 'utf8')]));
  const payload = Buffer.from(JSON.stringify(files)).toString('base64');
  const wrapper = `import base64,json,pathlib,subprocess,tempfile\nwith tempfile.TemporaryDirectory(prefix='gpoint-gsales-provision-') as directory:\n    root=pathlib.Path(directory)\n    for name,source in json.loads(base64.b64decode('${payload}')).items():\n        target=root/name; target.write_text(source); target.chmod(0o600)\n    subprocess.run(['python3',str(root/'gsales-production-provision.py'),'--candidate','${candidate}','--owner-approved-sha','${approvedSha}','--baseline','${baseline}'],check=True)\n`;
  return `set -e; printf '%s' '${Buffer.from(wrapper).toString('base64')}' | base64 -d | python3 -`;
}
