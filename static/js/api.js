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
//
// 4. Видимое состояние отказа (12.8). Перехват 401 появился после живого
//    деплоя 7 сентября и был единственным; 5xx и обрыв сети оставались без
//    хозяина. Каждый модуль показывал свой тост, а feed.js на ошибке
//    загрузки писал в console.error и молчал: пустая лента при лежащем
//    сервере выглядела как пустая лента. Тост тут не помощник — он живёт три
//    секунды, а сервер лежит минутами. Поэтому состояние ДЕРЖИТСЯ полосой
//    наверху, пока не пройдёт успешный запрос, и оно одно на всё приложение.

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

// Истёкшая или отсутствующая сессия — не ошибка загрузки данных, а повод
// вернуть человека на вход. Перехват ОДИН на все вызовы: иначе каждый
// модуль показывает свой тост, и вместо «войдите» получается стена ошибок
// (ровно это увидел владелец на живом деплое 7 сентября).
let redirecting = false;

function toLogin() {
  if (redirecting) return;
  redirecting = true;
  const back = window.location.pathname + window.location.search;
  const next = back && back !== '/' ? `?next=${encodeURIComponent(back)}` : '';
  window.location.replace(`/login${next}`);
}

// Полоса состояния связи. Ищется при показе, а НЕ при импорте: этот модуль
// обязан оставаться импортируемым без DOM. На этом стоит поведенческая
// проверка секретов — test_102 гоняет secrets.js в node, а тот тянет api.js;
// одна строка `document.getElementById` на верхнем уровне уронила её сразу.
// Элемента может не быть и в браузере: страницы входа собраны отдельно и
// этот модуль не грузят — тогда всё работает по-старому.
function connectionBanner() {
  if (typeof document === 'undefined') return [null, null];
  return [
    document.getElementById('connectionBanner'),
    document.getElementById('connectionBannerText'),
  ];
}

const OFFLINE_TEXT = 'Нет связи с сервером. Проверьте интернет — данные на экране могут устареть';
const SERVER_DOWN_TEXT = 'Сервер отвечает ошибкой. Данные на экране могут устареть, попробуйте позже';

function showConnectionState(text) {
  const [banner, label] = connectionBanner();
  if (!banner) return;
  if (label) label.textContent = text;
  banner.classList.add('open');
}

function clearConnectionState() {
  const [banner] = connectionBanner();
  if (banner) banner.classList.remove('open');
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

  let response;
  try {
    response = await fetch(apiBase() + url, options);
  } catch (e) {
    // fetch бросает только на сетевом отказе. Браузер говорит об этом
    // «Failed to fetch» (Safari — «Load failed»): по-английски и без
    // подсказки, что делать. Вызывающий код показывает e.message, поэтому
    // сообщение подменяется здесь, а не в двадцати catch-блоках.
    showConnectionState(OFFLINE_TEXT);
    throw new Error(OFFLINE_TEXT);
  }

  if (response.status === 401) {
    toLogin();
  } else if (response.status >= 500) {
    showConnectionState(SERVER_DOWN_TEXT);
  } else {
    // Сервер ответил по существу — состояние снимается. Держать его дольше
    // значит врать ровно так же, как молчать.
    clearConnectionState();
  }
  return response;
}

export function apiGet(url) {
  return apiFetch(url);
}
