import assert from 'node:assert/strict';
import { createHmac, randomUUID } from 'node:crypto';

const eros = 'http://eros:8080';
const secret = process.env.EROS_JWT_SECRET;
const genome = process.env.EROS_TEST_GENOME_ID;
const queryBase = 'SYNTH_TIER_PROBE: Which synthetic facts from our shared history are in context?';

assert.ok(secret, 'isolated Eros JWT secret is available');
assert.match(genome ?? '', /^[0-9a-f-]{36}$/i, 'synthetic persona genome id is available');

function jwt(userId) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const head = encode({ alg: 'HS256', typ: 'JWT' });
  const body = encode({
    sub: userId,
    role: 'authenticated',
    aud: 'authenticated',
    iss: 'supabase',
    exp: Math.floor(Date.now() / 1000) + 600,
  });
  const payload = `${head}.${body}`;
  return `${payload}.${createHmac('sha256', secret).update(payload).digest('base64url')}`;
}

async function post(path, token, body) {
  const response = await fetch(`${eros}${path}`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(90_000),
  });
  const text = await response.text();
  return { response, text };
}

const cases = [
  ...[1, 2, 3, 4, 5].map((tier) => ({
    name: `tier_${tier}`, bond_tier: tier, chemistry_tier: tier, memory_scope: 'full',
  })),
  { name: 'bond5_chem1', bond_tier: 5, chemistry_tier: 1, memory_scope: 'full' },
  { name: 'bond1_chem5', bond_tier: 1, chemistry_tier: 5, memory_scope: 'full' },
  { name: 'scope_none', bond_tier: 5, chemistry_tier: 5, memory_scope: 'none' },
  { name: 'scope_relationship_only', bond_tier: 5, chemistry_tier: 5, memory_scope: 'relationship_only' },
  { name: 'scope_neutral_only', bond_tier: 5, chemistry_tier: 5, memory_scope: 'neutral_only' },
];

if (process.argv.includes('--prepare')) {
  for (const item of cases) {
    item.user_id = randomUUID();
    const { response, text } = await post('/comp/chat/start', jwt(item.user_id), {
      genome_id: genome,
      force_new: true,
    });
    assert.equal(response.status, 200, `${item.name}: session start ${text}`);
    const started = JSON.parse(text);
    assert.match(started.session_id ?? '', /^[0-9a-f-]{36}$/i, `${item.name}: session id`);
    assert.match(started.instance_id ?? '', /^[0-9a-f-]{36}$/i, `${item.name}: instance id`);
    item.session_id = started.session_id;
    item.instance_id = started.instance_id;
    item.query = `SYNTH_TIER_CASE=${item.name}\n${queryBase} [${item.name}]`;
  }
  process.stdout.write(`${JSON.stringify(cases)}\n`);
  process.exit(0);
}

if (!process.argv.includes('--verify')) throw new Error('expected --prepare or --verify');
const casesJson = process.env.LOCAL_TIER_FIXTURES_B64;
assert.ok(casesJson, 'tier fixture identities are provided by the disposable test runner');
const fixtures = JSON.parse(Buffer.from(casesJson, 'base64').toString('utf8'));
assert.equal(fixtures.length, cases.length, 'all ten affinity and memory-scope scenarios are prepared');

const profileMarkers = [
  'SYNTH_PROFILE_NEUTRAL_1', 'SYNTH_PROFILE_NEUTRAL_2', 'SYNTH_PROFILE_ROMANCE',
  'SYNTH_PROFILE_FAMILY', 'SYNTH_PROFILE_FINANCE', 'SYNTH_PROFILE_KINK',
];
const relationshipMarkers = ['SYNTH_REL_RAW_1', 'SYNTH_REL_RAW_2', 'SYNTH_REL_RAW_3'];
const storyMarkers = [
  'SYNTH_ARC_OPENING', 'SYNTH_ARC_CURRENT',
  'SYNTH_EPISODE_1', 'SYNTH_EPISODE_2', 'SYNTH_EPISODE_3',
  'SYNTH_JOURNAL_1', 'SYNTH_JOURNAL_2',
];

function expectedProfile(item) {
  if (['none', 'relationship_only', 'neutral_only'].includes(item.memory_scope)) return [];
  const higher = Math.max(item.bond_tier, item.chemistry_tier);
  if (higher < 2) return [];
  const expected = ['SYNTH_PROFILE_NEUTRAL_1'];
  if (higher >= 3) expected.push('SYNTH_PROFILE_NEUTRAL_2');
  if (item.chemistry_tier >= 2) expected.push('SYNTH_PROFILE_ROMANCE');
  if (item.bond_tier >= 3) expected.push('SYNTH_PROFILE_FAMILY');
  if (item.bond_tier >= 4) expected.push('SYNTH_PROFILE_FINANCE');
  if (item.chemistry_tier >= 4) expected.push('SYNTH_PROFILE_KINK');
  return expected;
}

function expectedRelationshipCount(item) {
  if (['none', 'neutral_only'].includes(item.memory_scope)) return 0;
  return Math.min(3, Math.max(item.bond_tier, item.chemistry_tier));
}

function expectedStory(item) {
  return ['none', 'neutral_only'].includes(item.memory_scope) ? [] : storyMarkers;
}

function markersIn(messages, markers) {
  const text = messages.filter((message) => message.role === 'system')
    .map((message) => message.content).join('\n');
  return Object.fromEntries(markers.map((marker) => [marker, text.includes(marker)]));
}

for (const item of fixtures) {
  const token = jwt(item.user_id);
  const { response, text } = await post(`/comp/chat/${item.session_id}/message/stream`, token, {
    content: item.query,
    client_msg_id: randomUUID(),
    memory_scope: item.memory_scope,
  });
  assert.equal(response.status, 200, `${item.name}: chat status ${text}`);
  const frames = text.split(/\r?\n/).filter((line) => line.startsWith('data:'))
    .map((line) => JSON.parse(line.slice(5).trim()));
  const reply = frames.filter((frame) => frame.type === 'delta')
    .map((frame) => frame.content ?? '').join('');
  assert.ok(frames.some((frame) => frame.type === 'done'), `${item.name}: completed stream`);
  const providerEvidence = JSON.parse(reply);
  assert.equal(providerEvidence.case, item.name, `${item.name}: provider capture identity`);
  const seen = providerEvidence.seen;
  assert.ok(seen && typeof seen === 'object', `${item.name}: captured Eros prompt markers`);

  const actual = Object.entries(seen).filter(([, value]) => value).map(([key]) => key);
  const actualRelationship = actual.filter((marker) => relationshipMarkers.includes(marker));
  const actualNonRelationship = actual.filter((marker) => !relationshipMarkers.includes(marker));
  const expected = new Set([...expectedProfile(item), ...expectedStory(item)]);
  assert.deepEqual(new Set(actualNonRelationship), expected,
    `${item.name}: retrieved profile/story content matches upstream gate`);
  assert.equal(actualRelationship.length, expectedRelationshipCount(item),
    `${item.name}: relationship raw lines obey affinity and scope`);

  if (item.memory_scope === 'full') {
    const high = Math.max(item.bond_tier, item.chemistry_tier);
    assert.equal(actual.filter((marker) => marker.startsWith('SYNTH_PROFILE_')).length,
      high === 1 ? 0 : high === 2 ? 2 : 2 + Number(item.bond_tier >= 3) + Number(item.bond_tier >= 4) + Number(item.chemistry_tier >= 2) + Number(item.chemistry_tier >= 4),
      `${item.name}: profile K ramp and per-line sensitive-domain gates`);
    assert.equal(actual.filter((marker) => marker.startsWith('SYNTH_REL_RAW_')).length,
      Math.min(3, high), `${item.name}: relationship raw-line K ramp`);
  }
  console.log(`LOCAL_E2E_AFFINITY_TIER=PASS case=${item.name} bond=${item.bond_tier} chemistry=${item.chemistry_tier} scope=${item.memory_scope} markers=${actual.length}`);
}

console.log('LOCAL_E2E_MEMORY_SCOPE=PASS full_does_not_bypass_affinity=PASS episode_arc_journal_all_tiers=PASS');
console.log('LOCAL_E2E_REAL_INFERENCE=NOT_RUN');
