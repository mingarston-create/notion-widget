/* Specify a stable HTTPS hostname when the data service is ready. */
(function (root) {
  'use strict';
  const apiOrigin = 'https://notion-widgets-api.mingarston.workers.dev';

  root.WidgetConfig = Object.freeze({
    apiUrl(path) {
      if (!apiOrigin) throw new Error('Адрес сервиса данных ещё не настроен.');
      const origin = new URL(apiOrigin);
      if (origin.protocol !== 'https:' || origin.username || origin.password ||
          origin.pathname !== '/' || origin.search || origin.hash ||
          !path.startsWith('/api/')) {
        throw new Error('Некорректный HTTPS-адрес сервиса данных.');
      }
      return origin.origin + path;
    },
    authHeaders(key) {
      return {'X-Widget-Key': key};
    },
    hasApi: Boolean(apiOrigin)
  });
})(window);
