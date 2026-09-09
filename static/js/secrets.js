// secrets.js — как поля интеграций показывают и отправляют значения.
//
// Здесь сходятся два требования, которые легко перепутать.
//
// 1. **Поле должно показывать, что лежит в базе** (требование владельца,
//    задача 9.14). Пустая строка на месте заполненного ключа — не
//    осторожность, а дезинформация: по ней нельзя отличить «ключ есть» от
//    «ключа нет», и именно это скрыло дефект 9.13 на несколько дней.
//
// 2. **Пустое поле не значит «сотри»** (задача 9.13). Сервер различает
//    три состояния: поля нет в теле — не трогать, "" — стереть, значение —
//    записать. Форма, отправляющая `input.value` всегда, стирала секреты
//    при каждом сохранении и каждом переключении тумблера.
//
// Отсюда устройство: значение в поле бывает трёх видов — маска (показать,
// но не отправлять), настоящее (после «глазика» или ввода) и пустое. Что
// именно там лежит, помнят два флага на самом элементе, а не догадка по
// содержимому строки: маску, дополненную одним символом, от ключа по виду
// не отличить, и такая «правка» тихо записала бы мусор вместо ключа.

import { apiFetch } from './api.js';

const MASKED = 'masked'; // в поле маска — отправлять её нельзя
const TOUCHED = 'touched'; // пользователь правил поле руками

/**
 * Заполнить поле тем, что вернул сервер, и начать следить за правками.
 *
 * @param {HTMLInputElement} input поле
 * @param {HTMLElement|null} statusEl строка состояния под полем
 * @param {object} state
 *   @param {boolean} state.present есть ли значение в базе
 *   @param {string}  state.masked маска (для секретов)
 *   @param {string|null} state.value открытое значение — для полей, которые
 *          секретами не являются (адрес вебхука): их прячут зря, проверить
 *          и починить адрес, не видя его, нельзя
 *   @param {string}  state.noun «Ключ» / «Токен» / «Адрес»
 */
export function fillSecretField(input, statusEl, { present, masked, value = null, noun }) {
  if (!input) return;
  attachGuards(input);
  if (value !== null && value !== undefined) {
    setValue(input, value, false);
  } else if (present && masked) {
    setValue(input, masked, true);
  } else {
    setValue(input, '', false);
  }
  input.dataset.noun = noun;
  if (!statusEl) return;
  if (present) {
    statusEl.textContent =
      value !== null && value !== undefined
        ? `${noun} сохранён.`
        : `${noun} сохранён. Нажмите «глазик», чтобы показать значение целиком.`;
    statusEl.style.color = '#008715';
  } else {
    statusEl.textContent = `${noun} не задан.`;
    statusEl.style.color = 'var(--body-mid)';
  }
}

function setValue(input, text, masked) {
  input.value = text;
  if (masked) input.dataset[MASKED] = '1';
  else delete input.dataset[MASKED];
  delete input.dataset[TOUCHED];
}

/**
 * Маска в поле — только для показа. При первом же клике она уходит, иначе
 * дописанный к ней символ ушёл бы на сервер как новый ключ.
 */
function attachGuards(input) {
  if (input.dataset.guarded === '1') return;
  input.dataset.guarded = '1';
  input.addEventListener('focus', () => {
    if (input.dataset[MASKED] === '1') setValue(input, '', false);
  });
  input.addEventListener('input', () => {
    delete input.dataset[MASKED];
    input.dataset[TOUCHED] = '1';
  });
}

/**
 * Положить секрет в тело запроса — только если пользователь его ввёл.
 * Нетронутое поле и поле с маской не отправляются вовсе: сервер прочитает
 * отсутствие поля как «не трогай» и сохранённое значение не изменит.
 */
export function withSecret(body, name, input) {
  if (!input) return body;
  const typed = (input.value || '').trim();
  const untouched = input.dataset[TOUCHED] !== '1';
  if (input.dataset[MASKED] === '1' || untouched || typed === '') return body;
  body[name] = typed;
  return body;
}

/** Осознанная очистка: явная пустая строка — единственный способ стереть. */
export function clearSecret(url, name, extra = {}) {
  return apiFetch(url, { method: 'POST', body: { ...extra, [name]: '' } });
}

/**
 * «Глазик»: показать значение целиком.
 *
 * Секрет за ним приходит отдельным запросом, а не вместе с настройками:
 * ответ настроек ходит при каждом открытии вкладки, и удостоверению в нём
 * делать нечего. Показ виден в журнале — секрет, показанный молча, ничем
 * не отличается от секрета, вынутого через угнанную сессию.
 */
export async function toggleSecretVisibility(input, button, revealUrl) {
  if (!input) return;
  if (input.type === 'text') {
    input.type = 'password';
    button.textContent = '👁️';
    return;
  }
  if (input.dataset[MASKED] === '1' && revealUrl) {
    try {
      const res = await apiFetch(revealUrl, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data.secret) setValue(input, data.secret, false);
      }
    } catch (e) {
      console.error('Не удалось показать значение:', e);
    }
  }
  input.type = 'text';
  button.textContent = '🙈';
}
