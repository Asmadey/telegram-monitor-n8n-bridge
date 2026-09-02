// api.js — единственная точка HTTP-доступа фронтенда (задача 5.1).
//
// Пока UI работает со старым монолитом server.py, обёртка повторяет
// прежнее поведение fetch 1:1 (JSON-тело + Content-Type). Сюда же ляжет
// X-CSRF-Token (В11, PLAN.md), когда UI переключится на новую сборку:
// заголовок ставится в ОДНОМ месте, а не в тридцати вызовах.

export async function apiFetch(url, { method = 'GET', body } = {}) {
  const options = { method, credentials: 'same-origin' };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  return fetch(url, options);
}

export function apiGet(url) {
  return apiFetch(url);
}