// logs.js — вкладка «Журнал»: таблица логов, фильтр по статусу,
// очистка журнала (задача 5.1, разрез index.html).

import { apiFetch, apiGet } from './api.js';
import { html, showToast } from './render.js';

const tabLogsCount = document.getElementById('tabLogsCount');
const logsTableBody = document.getElementById('logsTableBody');
const filterLogStatus = document.getElementById('filterLogStatus');
const refreshLogsBtn = document.getElementById('refreshLogsBtn');
const loadMoreLogsBtn = document.getElementById('loadMoreLogsBtn');
const clearLogsBtn = document.getElementById('clearLogsBtn');

let currentLogs = [];
const LOGS_PAGE_SIZE = 150;
let totalLogs = 0;

const opsDb = document.getElementById('opsDb');
const opsWorker = document.getElementById('opsWorker');
const opsQueue = document.getElementById('opsQueue');
const opsKey = document.getElementById('opsKey');

function age(seconds) {
  if (seconds === null || seconds === undefined) return '';
  if (seconds < 90) return `${seconds} с назад`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} мин назад`;
  return `${Math.round(seconds / 3600)} ч назад`;
}

export async function loadOpsStatus() {
  if (!opsDb) return;
  try {
    const res = await apiGet('/api/ops/health');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const s = await res.json();

    opsDb.textContent = s.database.ok ? 'доступна' : 'НЕДОСТУПНА';
    opsDb.style.color = s.database.ok ? '' : 'var(--accent-red)';

    if (!s.worker.seen) {
      opsWorker.textContent = 'не запускался';
    } else {
      opsWorker.textContent = (s.worker.alive ? 'жив' : 'МОЛЧИТ') + ', ' + age(s.worker.seconds_since_beat);
    }
    // Молчащий воркер — самый вероятный отказ: лента пуста, задача висит,
    // ошибок нет нигде. Поэтому он выделен, а не спрятан в общий текст.
    opsWorker.style.color = s.worker.alive ? '' : 'var(--accent-red)';

    const oldest = s.jobs.oldest_pending_seconds;
    opsQueue.textContent = s.jobs.pending === 0
      ? 'пусто'
      : `${s.jobs.pending} в ожидании, старшей ${age(oldest)}`;
    opsQueue.style.color = s.jobs.pending > 0 && oldest > 600 ? 'var(--accent-red)' : '';

    // Отпечатки, а не ключи: по восьми знакам хеша ключ не восстановить,
    // но видно, одним ли ключом работают web и воркер. Разные ключи —
    // самый дорогой по времени отказ этой сборки (7 сентября).
    const enc = s.encryption || {};
    if (enc.match === true) {
      opsKey.textContent = `совпадает (${enc.web})`;
      opsKey.style.color = '';
    } else if (enc.match === false) {
      opsKey.textContent = `РАЗНЫЙ: web ${enc.web}, воркер ${enc.worker}`;
      opsKey.style.color = 'var(--accent-red)';
    } else {
      opsKey.textContent = `web ${enc.web || '—'}, воркер не отмечался`;
      opsKey.style.color = '';
    }
  } catch (e) {
    opsDb.textContent = opsWorker.textContent = opsQueue.textContent = 'нет связи';
    if (opsKey) opsKey.textContent = '—';
  }
}

export async function loadLogs(append = false) {
  const status = filterLogStatus ? filterLogStatus.value : 'ALL';
  try {
    const offset = append ? currentLogs.length : 0;
    const res = await apiGet(`/api/logs?limit=${LOGS_PAGE_SIZE}&offset=${offset}&status=${encodeURIComponent(status)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const page = data.logs || [];
    currentLogs = append ? currentLogs.concat(page) : page;
    totalLogs = data.total || 0;
    loadMoreLogsBtn.hidden = currentLogs.length >= totalLogs;
    if (tabLogsCount) tabLogsCount.textContent = totalLogs;
    renderLogs();
  } catch (e) {
    console.error('Logs fetch error:', e);
    showToast('Ошибка загрузки логов', true);
  }
}

function renderLogs() {
  if (currentLogs.length === 0) {
    logsTableBody.innerHTML = html`
      <tr>
        <td colspan="6" class="u-ta-center u-p-3xl u-c-mute">
          Записи в журнале логов отсутствуют.
        </td>
      </tr>
    `;
    return;
  }

  logsTableBody.innerHTML = currentLogs.map(log => html`
    <tr>
      <td class="u-c-body-mid u-fs-caption u-fvn-tabular-nums u-ws-nowrap">
        ${new Date(log.timestamp).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'})}
      </td>
      <td>
        <span class="meta-tag u-fs-eyebrow-sm" >${log.event_type}</span>
      </td>
      <td>
        <div class="u-fw-500 u-c-ink">${log.chat_title || '—'}</div>
      </td>
      <td>
        <span class="status-tag ${log.status}">${log.status}</span>
      </td>
      <td class="u-fs-caption u-c-body u-wb-break-word">
        ${log.details || ''}
      </td>
      <td class="u-ta-center">
        <button class="btn btn-secondary btn-icon-sm" type="button" data-action="delete-log" data-log-id="${log.id}" title="Удалить запись">✕</button>
      </td>
    </tr>
  `).join('');
}

// Крестик в строке (13.28): одна запись, без подтверждения — журнал не
// данные клиента, а след работы, и строку убирают, чтобы её не было. Отметки
// «прочитано» сервер при этом не трогает. Обработчик один на таблицу: строки
// перерисовываются, а встроенные обработчики запрещает CSP (9.8).
logsTableBody.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action="delete-log"]');
  if (!button) return;
  const id = Number(button.dataset.logId);
  button.disabled = true;
  try {
    const res = await apiFetch(`/api/logs/${id}`, { method: 'DELETE' });
    // 404 — записи уже нет (удалили из другой вкладки): убрать её и здесь
    if (!res.ok && res.status !== 404) throw new Error('HTTP ' + res.status);
    currentLogs = currentLogs.filter(log => log.id !== id);
    totalLogs = Math.max(0, totalLogs - 1);
    if (tabLogsCount) tabLogsCount.textContent = totalLogs;
    renderLogs();
  } catch (e) {
    button.disabled = false;
    showToast('Не удалось удалить запись', true);
  }
});

filterLogStatus.addEventListener('change', () => loadLogs());
refreshLogsBtn.addEventListener('click', () => loadLogs());
loadMoreLogsBtn.addEventListener('click', () => loadLogs(true));
clearLogsBtn.addEventListener('click', async () => {
  if (!confirm('Очистить весь журнал логов?')) return;
  try {
    const res = await apiFetch('/api/logs', { method: 'DELETE' });
    if (res.ok) {
      showToast('Журнал логов очищен');
      loadLogs();
    }
  } catch (e) {
    showToast('Ошибка очистки логов', true);
  }
});
