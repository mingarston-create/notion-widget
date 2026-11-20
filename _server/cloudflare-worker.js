const pagesOrigin = 'https://mingarston-create.github.io';
const routes = new Map([
  ['/health', 'GET'],
  ['/api/habits/status', 'GET'],
  ['/api/habits/events', 'POST'],
  ['/api/habits/settings/timezone', 'POST'],
  ['/api/tasks/status', 'GET'],
  ['/api/donation/status', 'GET'],
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = routes.get(url.pathname);
    if (!method) return new Response('Not found', { status: 404 });

    const origin = request.headers.get('Origin');
    if (origin && origin !== pagesOrigin) {
      return new Response('Forbidden', { status: 403 });
    }
    if (request.method !== method && request.method !== 'OPTIONS') {
      return new Response('Method not allowed', { status: 405 });
    }
    const headers = new Headers();
    for (const name of ['Origin', 'Authorization', 'Content-Type', 'Access-Control-Request-Method', 'Access-Control-Request-Headers']) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }

    try {
      const target = new URL(url.pathname + url.search, 'http://127.0.0.1:8080');
      const result = await env.WIDGET_ORIGIN.fetch(target, {
        method: request.method,
        headers,
        body: request.method === 'POST' ? request.body : undefined,
        redirect: 'manual',
      });
      const outgoing = new Headers(result.headers);
      outgoing.set('Cache-Control', 'private, no-store');
      return new Response(result.status === 204 ? null : result.body, { status: result.status, headers: outgoing });
    } catch {
      const outgoing = { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' };
      if (origin === pagesOrigin) outgoing['Access-Control-Allow-Origin'] = pagesOrigin;
      return new Response('{"error":"Сервис виджетов временно недоступен."}', { status: 502, headers: outgoing });
    }
  },
};
