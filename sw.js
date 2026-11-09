const C='v1';
self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(['./','index.html','manifest.webmanifest'])));self.skipWaiting()});
self.addEventListener('fetch',e=>{if(new URL(e.request.url).pathname.includes('/data/'))return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)))});
