const path = require('node:path');

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // Next owns this directory; keep it separate from all other workspace caches.
  distDir: '../.cache/frontend-next',
  outputFileTracingRoot: path.join(__dirname, '..'),
  turbopack: { root: path.join(__dirname, '..') },
  reactStrictMode: true,
};
module.exports = nextConfig;
