// usage.js — расход токенов за месяц в разрезе источника и канала (12.10).
//
// Данные даёт GET /api/usage (задача 12.7). Вопрос, на который отвечает эта
// вкладка, один: КАКОЙ КАНАЛ ЖЖЁТ БЮДЖЕТ. Поэтому каналы показываются в том
// порядке, в каком их отдал сервер (по убыванию), и ничего не сортируется
// заново: два разных порядка на одни данные — это два разных ответа.
//
// usageMarkup вынесена отдельно и НИЧЕГО не знает про DOM: данные на входе,
// строка на выходе. Так её исполняет node в тестах (test_127) — проверять
// разбором исходника «что увидит человек при нулевом расходе» бессмысленно.

import { apiGet } from './api.js';
import { html, raw } from './render.js';

function plural(tokens) {
  return Number(tokens || 0).toLocaleString('ru-RU');
}

export function usageMarkup(data) {
  const period = data?.period || '';
  const total = Number(data?.total || 0);
  const sources = Array.isArray(data?.sources) ? data.sources : [];
  const outside = Number(data?.outside_sources || 0);

  // Пустой расход — законное состояние (месяц начался, AI выключен), и
  // пустое место вместо ответа делает его неотличимым от поломки. Тот же
  // класс ошибки, что ловили всю фазу 9.
  if (!total && !sources.length) {
    return html`<p style="color: var(--body-mid); font-size: 13px;">
      За ${period} ничего не потрачено: расход появится после первого разбора.
    </p>`;
  }

  const limit = Number(data?.limit || 0);
  const rows = sources.map(source => {
    const channels = Array.isArray(source.channels) ? source.channels : [];
    const lines = channels.map(channel => html`
      <div style="display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: 12.5px;">
        <span style="color: var(--body-mid);">${channel.chat_title}</span>
        <span>${plural(channel.tokens)}</span>
      </div>`).join('');
    const summary = source.summary_tokens
      ? html`<div style="display: flex; justify-content: space-between; gap: 12px; padding: 3px 0; font-size: 12.5px;">
          <span style="color: var(--mute);">сведение по каналам</span>
          <span style="color: var(--mute);">${plural(source.summary_tokens)}</span>
        </div>`
      : '';
    return html`
      <div style="padding: 10px 0; border-top: 1px solid var(--hairline-subtle);">
        <div style="display: flex; justify-content: space-between; gap: 12px; font-weight: 600; font-size: 13px;">
          <span>${source.title}</span>
          <span>${plural(source.tokens)}</span>
        </div>
        ${raw(lines)}
        ${raw(summary)}
      </div>`;
  }).join('');

  // Расход вне источников показывается всегда, когда он есть: без него
  // сумма на экране не сходится с общим числом, и веры экрану нет.
  const outsideRow = outside
    ? html`<div style="display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-top: 1px solid var(--hairline-subtle); font-size: 13px;">
        <span style="color: var(--body-mid);">Вне источников (переразбор из ленты)</span>
        <span>${plural(outside)}</span>
      </div>`
    : '';

  return html`
    <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px;">
      <span style="font-size: 13px; color: var(--body-mid);">Период ${period}</span>
      <span style="font-size: 15px; font-weight: 600;">${plural(total)}${limit ? ` из ${plural(limit)}` : ''}</span>
    </div>
    ${raw(rows)}
    ${raw(outsideRow)}`;
}

export async function loadUsage() {
  // Элемент ищется здесь, а не при импорте: модуль обязан подниматься без
  // DOM — на этом стоит проверка разметки в node (test_127), и ровно этой
  // строкой на верхнем уровне задача 12.8 уронила test_102.
  const usageBody = document.getElementById('usageBody');
  if (!usageBody) return;
  try {
    const response = await apiGet('/api/usage');
    if (!response.ok) return;
    usageBody.innerHTML = usageMarkup(await response.json());
  } catch (e) {
    // Полосу состояния связи уже показал api.js — второй раз о том же
    // кричать незачем, а рисовать «ничего не потрачено» при недоступном
    // сервере значило бы соврать.
    console.error('Usage fetch error:', e);
  }
}
