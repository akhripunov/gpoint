import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiPaths = ['auth', 'me', 'creators', 'reels', 'vip', 'creator', 'admin', 'media', 'events', 'following', 'discovery', 'preferences', 'internal', 'feed'];
const apiProxy = Object.fromEntries(apiPaths.map(path => [`/${path}`, { target: 'http://127.0.0.1:3000', changeOrigin: false }]));

export default defineConfig({ plugins: [react()], server: { allowedHosts: process.env.PUBLIC_APP_HOST ? [process.env.PUBLIC_APP_HOST] : [], proxy: apiProxy } });
