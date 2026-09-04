// api.js — единственная точка HTTP-доступа фронтенда (задача 5.1).
//
// Фронтенд живёт на Vercel, API — на Railway. Отсюда три обязанности,
// которые собраны здесь, а не размазаны по тридцати вызовам.
//
// 1. Базовый адрес API. По умолчанию — пустой, то есть запросы идут на тот
//    же origin: Vercel переписывает /api/* на Railway (vercel.json). Это
//    рекомендуемый режим — браузер видит один сайт, cookie остаются первой
//    стороной и работают везде. Прямые межсайтовые вызовы включаются
//    заданием window.__TELETON_API__ и требуют CORS + SameSite=None на
//    бэкенде; в Safari такие cookie блокируются, поэтому это запасной путь.
//
// 2. credentials: 'include'. Без него браузер не приложит cookie сессии к
//    межсайтовому запросу, и пользователь окажется разлогинен ровно в тот
//    момент, когда фронтенд переедет на отдельный домен.
//
// 3. X-CSRF-Token на каждом изменяющем запросе (В11). Бэкенд отвергает
//    не-GET без него; токен лежит в НЕ-httponly cookie именно затем, чтобы
//    его мог прочитать этот файл.

const CSRF_COOKIE = 'csrf_token';
const CSRF_HEADER = 'X-CSRF-Token';
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** База API: пусто = тот же origin (режим переписывания на Vercel). */
export function apiBase() {
  const configured = typeof window !== 'undefined' && window.__TELETON_API__;
  return (configured || '').replace(/\/$/, '');
}

export function readCookie(name) {
  const match = document.cookie.match(
    new RegExp('(?:^|; )' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)')
  );
  return match ? decodeURIComponent(match[1]) : '';
}

export async function apiFetch(url, { method = 'GET', body } = {}) {
  const options = {
    method,
    // include, а не same-origin: при отдельном домене фронтенда
    // same-origin молча не приложит cookie сессии
    credentials: 'include',
    headers: {},
  };

  if (!SAFE_METHODS.has(method.toUpperCase())) {
    const token = readCookie(CSRF_COOKIE);
    if (token) options.headers[CSRF_HEADER] = token;
  }

  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  return fetch(apiBase() + url, options);
}

export function apiGet(url) {
  return apiFetch(url);
}
