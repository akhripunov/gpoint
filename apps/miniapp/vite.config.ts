import { execFileSync } from 'node:child_process';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const buildSha = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
const buildTime = new Date().toISOString();
const apiPaths = ['health', 'auth', 'me', 'creators', 'reels', 'vip', 'creator', 'admin', 'media', 'events', 'following', 'discovery', 'preferences', 'internal', 'feed'];
const apiProxy = Object.fromEntries(apiPaths.map(path => [`/${path}`, { target: 'http://127.0.0.1:3000', changeOrigin: false }]));

export default defineConfig({ define: { __GPOINT_BUILD_SHA__: JSON.stringify(buildSha), __GPOINT_BUILD_TIME__: JSON.stringify(buildTime) }, plugins: [react()], server: { allowedHosts: process.env.PUBLIC_APP_HOST ? [process.env.PUBLIC_APP_HOST] : [], proxy: apiProxy } });
