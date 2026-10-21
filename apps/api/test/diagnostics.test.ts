import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DiagnosticsController } from '../src/diagnostics.js';

const originalWarn = console.warn;
let logged: unknown[] = [];
function makeController() {
  logged = [];
  console.warn = (...args: unknown[]) => { logged.push(args); };
  return new DiagnosticsController();
}
afterEach(() => { console.warn = originalWarn; });
const peer = (remoteAddress = '127.0.0.1') => ({ ip: 'spoofed', headers: { 'x-forwarded-for': 'spoofed' }, socket: { remoteAddress } });
const report = (extra: Record<string, unknown> = {}) => ({ stage: 'RUNTIME_ERROR', errorMessage: 'failure', ...extra });

test('accepts the miniapp DOM fixture but drops private and unknown DOM properties', () => {
  const api = makeController();
  api.client({ reports: [report({ dom: {
    readyState: 'complete', bodyChildElementCount: 2, rootExists: true, rootChildElementCount: 1, rootHtmlLength: 20,
    rootRect: { x: 0, y: 1, width: 320, height: 640 }, bodyRect: null, innerWidth: 320, innerHeight: 640,
    clientWidth: 320, clientHeight: 640,
    appStyle: { display: 'block', visibility: 'visible', opacity: '1', position: 'relative', zIndex: '1', width: '320px', height: '640px', overflow: 'hidden', private: 'secret' },
    centerElement: { tag: 'DIV', id: 'root', className: 'app', rect: { x: 1, y: 2, width: 3, height: 4 }, display: 'block', visibility: 'visible', opacity: '1', zIndex: '2', html: '<secret>' },
    privateHtml: '<input value="secret">',
  } })] }, peer());
  const output = JSON.stringify(logged);
  assert.match(output, /"readyState":"complete"/);
  assert.match(output, /"width":320/);
  assert.doesNotMatch(output, /secret|privateHtml|"html"/);
});

test('does not invoke getters or inherited properties while reading reports', () => {
  const api = makeController();
  let touched = false;
  const item = Object.create({ errorMessage: 'inherited private value' });
  Object.defineProperty(item, 'stage', { enumerable: true, get() { touched = true; throw Error('getter'); } });
  Object.defineProperty(item, 'dom', { enumerable: true, get() { touched = true; return { privateHtml: 'secret' }; } });
  api.client({ reports: [item] }, peer());
  assert.equal(touched, false);
  assert.doesNotMatch(JSON.stringify(logged), /getter|inherited private value|secret/);
});

test('never logs free-form error messages or stacks', () => {
  const api = makeController();
  api.client({ reports: [report({ errorName: 'TypeError', errorMessage: 'ACCESS_TOKEN_PRIVATE_123', errorStack: 'PRIVATE_STACK_VALUE_456' })] }, peer());
  const output = JSON.stringify(logged);
  assert.match(output, /TypeError/);
  assert.doesNotMatch(output, /ACCESS_TOKEN_PRIVATE_123|PRIVATE_STACK_VALUE_456|failure/);
  assert.ok(Buffer.byteLength(output) <= 32 * 1024);
});

test('caps one peer at five requests per minute and ignores forwarded headers', () => {
  const api = makeController();
  for (let i = 0; i < 7; i++) api.client({ reports: [report({ diagnosticId: String(i) })] }, peer());
  assert.equal(logged.length, 5);
  api.client({ reports: [report()] }, peer('127.0.0.2'));
  assert.equal(logged.length, 6);
});

test('caps aggregate traffic at thirty requests per minute', () => {
  const api = makeController();
  for (let i = 0; i < 35; i++) api.client({ reports: [report()] }, peer(`192.0.2.${i + 1}`));
  assert.equal(logged.length, 30);
});

test('peer-denied requests do not consume the global logging allowance', () => {
  const api = makeController();
  for (let i = 0; i < 100; i++) api.client({ reports: [report()] }, peer('192.0.2.1'));
  assert.equal(logged.length, 5);
  for (let i = 0; i < 30; i++) api.client({ reports: [report()] }, peer(`192.0.2.${i + 2}`));
  assert.equal(logged.length, 30);
});

test('bounds peer tracking during address churn', () => {
  const api = makeController();
  for (let i = 0; i < 1100; i++) api.client({ reports: [report()] }, peer(`198.51.${Math.floor(i / 256)}.${i % 256}`));
  assert.equal(logged.length, 30);
  assert.ok((api as unknown as { peerBuckets: Map<unknown, unknown> }).peerBuckets.size <= 1024);
});
