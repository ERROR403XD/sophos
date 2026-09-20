/* Sophos Service Worker（R6.1 PWA）。
 *
 * 策略（面向局域网单用户媒体工具，刻意保守）：
 * - /api/* 一律直连（视频流/转码管道/缩略图/鉴权绝不进 SW 缓存——
 *   流是渐进管道、缩略图已有 immutable HTTP 缓存头，SW 不掺和）；
 * - 页面导航：网络优先，失败回落缓存中的 SPA 壳（断网/重启时可打开）；
 * - 同源静态资源（/assets/* 哈希文件、图标）：缓存优先 + 后台刷新
 *   （stale-while-revalidate，哈希文件名天然可长期缓存）；
 * - 缓存名带版本，新 SW 激活时清掉旧版本。改动 SW 后升版本号即可强制刷新。
 */
const CACHE = 'sophos-v1';
const SHELL = '/';

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll([SHELL, '/icon.png', '/favicon.png', '/manifest.webmanifest']))
      .catch(() => {})  // 预缓存失败不阻塞安装（离线首访场景）
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 跨域不处理
  if (url.pathname.startsWith('/api/')) return; // API/流/缩略图：直连

  // 页面导航：网络优先，回落 SPA 壳
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const cache = await caches.open(CACHE);
        cache.put(SHELL, fresh.clone()).catch(() => {});
        return fresh;
      } catch (err) {
        return (await caches.match(SHELL)) || Response.error();
      }
    })());
    return;
  }

  // 其余同源静态资源：缓存优先 + 后台刷新
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req);
    const refresh = fetch(req)
      .then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone()).catch(() => {});
        return resp;
      })
      .catch(() => hit);
    return hit || refresh;
  })());
});
