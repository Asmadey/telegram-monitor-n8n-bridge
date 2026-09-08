// secrets.js — правило обращения с секретными полями форм (задача 9.13).
//
// Бэкенд различает три состояния секрета (контракт С16, задача 0.3):
// поля нет в теле запроса — «не трогай», `""` — «сотри», значение —
// «запиши». Формы же слали `input.value.trim()` всегда, а поле секрета
// после загрузки страницы пустое по построению: сырой ключ наружу не
// отдаётся (К3), подставить его обратно нечем. Каждое сохранение и каждое
// переключение тумблера уходило как «сотри» — и стирало только что
// сохранённый ключ, отвечая при этом 200 и «Настройки сохранены».
//
// Здесь это правило собрано в одном месте, чтобы его нельзя было обойти
// по невнимательности: секрет попадает в тело только через withSecret,
// а очистка — отдельное осознанное действие (clearSecret).

import { apiFetch } from './api.js';

/**
 * Добавить секрет в тело запроса, ЕСЛИ пользователь его ввёл.
 * Пустое поле не добавляется вовсе — сервер прочитает это как
 * «поле не передано» и сохранённое значение не тронет.
 */
export function withSecret(body, name, input) {
  const typed = ((input && input.value) || '').trim();
  if (typed !== '') body[name] = typed;
  return body;
}

/** Осознанная очистка: явная пустая строка — единственный способ стереть. */
export function clearSecret(url, name, extra = {}) {
  return apiFetch(url, { method: 'POST', body: { ...extra, [name]: '' } });
}

// Исходный плейсхолдер поля (пример формата) — чтобы вернуть его, когда
// секрета в базе нет. Читается один раз, до первой подмены маской.
const emptyPlaceholder = new WeakMap();

function basePlaceholder(input) {
  if (!emptyPlaceholder.has(input)) {
    emptyPlaceholder.set(input, input.getAttribute('placeholder') || '');
  }
  return emptyPlaceholder.get(input);
}

/**
 * Показать, что лежит на сервере.
 *
 * Сырой секрет не возвращается никогда, поэтому эта строка — единственный
 * сигнал «значение на месте». Раньше маска выставлялась только в ветке
 * «ключ есть» и обратно не сбрасывалась: после очистки поле продолжало
 * показывать старую маску, то есть интерфейс утверждал то, чего в базе
 * уже не было. Обе ветки — здесь, поэтому забыть одну нельзя.
 *
 * @param {HTMLInputElement} input поле ввода секрета
 * @param {HTMLElement|null} statusEl строка состояния под полем
 * @param {boolean} present есть ли секрет в базе (has_key / has_token / has_webhook)
 * @param {string} masked маска значения с сервера
 * @param {string} noun «Ключ» / «Токен» / «Адрес» — для человеческой формулировки
 */
export function showSecretState(input, statusEl, present, masked, noun) {
  if (!input) return;
  const base = basePlaceholder(input);
  input.value = '';
  input.placeholder = present && masked ? `${noun} сохранён: ${masked}` : base;
  if (!statusEl) return;
  if (present) {
    statusEl.textContent =
      `${noun} сохранён (${masked}). Поле пустое — прежнее значение останется, ` +
      `введите новое, чтобы заменить.`;
    statusEl.style.color = '#008715';
  } else {
    statusEl.textContent = `${noun} не задан.`;
    statusEl.style.color = 'var(--body-mid)';
  }
}
