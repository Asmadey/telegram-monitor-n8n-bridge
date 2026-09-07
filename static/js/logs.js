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
    opsDb.style.color = s.database.ok ? '' : '#d33';

    if (!s.worker.seen) {
      opsWorker.textContent = 'не запускался';
    } else {
      opsWorker.textContent = (s.worker.alive ? 'жив' : 'МОЛЧИТ') + ', ' + age(s.worker.seconds_since_beat);
    }
    // Молчащий воркер — самый вероятный отказ: лента пуста, задача висит,
    // ошибок нет нигде. Поэтому он выделен, а не спрятан в общий текст.
    opsWorker.style.color = s.worker.alive ? '' : '#d33';

    const oldest = s.jobs.oldest_pending_seconds;
    opsQueue.textContent = s.jobs.pending === 0
      ? 'пусто'
      : `${s.jobs.pending} в ожидании, старшей ${age(oldest)}`;
    opsQueue.style.color = s.jobs.pending > 0 && oldest > 600 ? '#d33' : '';
  } catch (e) {
    opsDb.textContent = opsWorker.textContent = opsQueue.textContent = 'нет связи';
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
        <td colspan="5" style="text-align: center; padding: 48px; color: var(--mute);">
          Записи в журнале логов отсутствуют.
        </td>
      </tr>
    `;
    return;
  }

  logsTableBody.innerHTML = currentLogs.map(log => html`
    <tr>
      <td style="color: var(--body-mid); font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap;">
        ${new Date(log.timestamp).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'})}
      </td>
      <td>
        <span class="meta-tag" style="font-size: 11px;">${log.event_type}</span>
      </td>
      <td>
        <div style="font-weight: 500; color: var(--ink);">${log.chat_title || '—'}</div>
      </td>
      <td>
        <span class="status-tag ${log.status}">${log.status}</span>
      </td>
      <td style="font-size: 12.5px; color: var(--body); word-break: break-word;">
        ${log.details || ''}
      </td>
    </tr>
  `).join('');
}

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
