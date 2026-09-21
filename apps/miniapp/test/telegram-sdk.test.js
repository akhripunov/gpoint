import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = dirname(dirname(fileURLToPath(import.meta.url)));

test('production entrypoint loads the Telegram WebApp SDK before the app', async () => {
  const html = await readFile(join(root, 'index.html'), 'utf8');
  const sdk = '<script src="https://telegram.org/js/telegram-web-app.js"></script>';
  assert.ok(html.includes(sdk));
  assert.ok(html.indexOf(sdk) < html.indexOf('<script type="module"'));
});

test('API client disables conditional caching and bounds request time', async () => {
  const source = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  assert.match(source, /cache:'no-store'/);
  assert.match(source, /setTimeout\(\(\)=>controller\.abort\(\),timeoutMs\)/);
});

test('media asset URLs resolve from the same origin, not the /api path base', async () => {
  const source = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  assert.match(source, /new URL\(path,window\.location\.origin\)/);
  assert.doesNotMatch(source, /new URL\(path,apiBase\|\|window\.location\.origin\)/);
});

test('creator onboarding uses the official startchannel deep link', async () => {
  const client = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  const main = await readFile(join(root, 'src/main.tsx'), 'utf8');
  assert.match(client, /openTelegramLink/);
  assert.match(client, /startchannel&admin=post_messages/);
  assert.match(client, /creator\/channel\/start/);
  assert.doesNotMatch(main, /Prepare channel selector/);
  assert.match(main, /Connect Telegram channel/);
  assert.match(main, /openTelegramStartChannel/);
  assert.doesNotMatch(main, /NativeCreatorOnboardingBridge/);
  assert.match(main, /state === "IDLE"/);
  assert.match(main, /WAITING_FOR_CHANNEL/);
  assert.match(main, /telegramChannelConnectionStatus/);
  assert.match(main, /deadline > Date\.now\(\) \? 1500 : false/);
  assert.match(main, /Channel connected/);
  assert.match(main, /Continue to Creator tools/);
  assert.match(main, /Try again/);
  assert.doesNotMatch(main, /requestCreatorTelegramChannel/);
});

test('disconnected channel control opens Telegram after the start request succeeds', async () => {
  const platform = await readFile(join(root, 'src', 'platform-ui.tsx'), 'utf8');
  assert.match(platform, /openTelegramStartChannel/);
  assert.match(platform, /mutationFn: startTelegramChannelConnection/);
  assert.match(platform, /onSuccess: \(\) =>/);
  assert.match(platform, /openTelegramStartChannel\(\)/);
});

test('connected creators get a dedicated workspace without moderation or recommendation controls', async () => {
  const main = await readFile(join(root, 'src', 'main.tsx'), 'utf8');
  const platform = await readFile(join(root, 'src', 'platform-ui.tsx'), 'utf8');

  assert.match(main, /GPoint is connected and ready to import new posts automatically\./);
  assert.match(main, /Continue to Creator tools/);
  assert.match(main, /<CreatorWorkspaceDashboard \/>/);
  assert.doesNotMatch(main, /<PlatformDashboard \/>/);

  const dashboardStart = platform.indexOf('export function CreatorWorkspaceDashboard');
  assert.notEqual(dashboardStart, -1);
  const dashboard = platform.slice(dashboardStart);
  assert.match(dashboard, /Creator workspace active/);
  assert.match(dashboard, /Profile settings/);
  assert.match(dashboard, /Imported Posts/);
  assert.match(dashboard, /Stats/);
  assert.doesNotMatch(dashboard, /Submit for Moderation/);
  assert.doesNotMatch(dashboard, /Under review/);
  assert.doesNotMatch(dashboard, /Recommendation controls/);
});

test('connected Telegram channel card exposes operational settings', async () => {
  const platform = await readFile(join(root, 'src', 'platform-ui.tsx'), 'utf8');
  assert.match(platform, /MY TELEGRAM CHANNEL/);
  assert.match(platform, /c\.title/);
  assert.match(platform, /status="Connected"/);
  assert.match(platform, /<TelegramChannelCard/);
  assert.match(platform, /lastSync=/);
  assert.match(platform, /export function TelegramImportSettings/);
  assert.match(platform, /disabled={!dirty \|\| update\.isPending}/);
});

test('disconnected creator channels use native selection so existing bot membership can be reconnected', async () => {
  const client = await readFile(join(root, 'src', 'api', 'client.ts'), 'utf8');
  const platform = await readFile(join(root, 'src', 'platform-ui.tsx'), 'utf8');
  assert.match(client, /prepareCreatorChannelSelection/);
  assert.match(client, /requestCreatorTelegramChannel/);
  assert.match(client, /creatorChannelSelectionStatus/);
  const controls = platform.slice(platform.indexOf('export function TelegramChannelControls'), platform.indexOf('export function TelegramImportSettings'));
  assert.match(controls, /prepareCreatorChannelSelection/);
  assert.match(controls, /requestCreatorTelegramChannel/);
  assert.match(controls, /creatorChannelSelectionStatus/);
  assert.doesNotMatch(controls, /openTelegramStartChannel/);
});

test('creator workspace derives its hero state from the durable channel query', async () => {
  const platform = await readFile(join(root, 'src', 'platform-ui.tsx'), 'utf8');
  const dashboardStart = platform.indexOf('export function CreatorWorkspaceDashboard');
  assert.notEqual(dashboardStart, -1);
  const dashboard = platform.slice(dashboardStart);
  assert.match(dashboard, /queryKey: \["telegram-channel"\], queryFn: telegramChannel/);
  assert.match(dashboard, /const channelConnected = channelQ\.data\?\.status === "CONNECTED"/);
  assert.match(dashboard, /title=\{channelConnected \? "Creator workspace active" : "Reconnect your Telegram channel"\}/);
  assert.match(dashboard, /body=\{channelConnected \? "Telegram channel connected and ready for ingestion\." : "Reconnect your Telegram channel to resume ingestion\."\}/);
});

test('authenticated app passes the single authenticated user into the app shell', async () => {
  const main = await readFile(join(root, 'src', 'main.tsx'), 'utf8');
  const appStart = main.indexOf('function App({ initialUser }');
  const authStart = main.indexOf('function AuthenticatedApp()');
  assert.notEqual(appStart, -1);
  assert.notEqual(authStart, -1);
  const app = main.slice(appStart, authStart);
  const authenticatedApp = main.slice(authStart);
  assert.doesNotMatch(app, /\bauth\(\)/);
  assert.match(authenticatedApp, /\.then\(\(authenticatedUser\) =>/);
  assert.match(authenticatedApp, /<App initialUser=\{user\} \/>/);
});
