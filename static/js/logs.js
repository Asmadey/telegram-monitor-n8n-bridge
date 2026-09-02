// logs.js — вкладка «Журнал»: таблица логов, фильтр по статусу,
// очистка журнала (задача 5.1, разрез index.html).

import { apiFetch, apiGet } from './api.js';
import { escapeHtml, esc, showToast } from './render.js';

const tabLogsCount = document.getElementById('tabLogsCount');
const logsTableBody = document.getElementById('logsTableBody');
const filterLogStatus = document.getElementById('filterLogStatus');
const refreshLogsBtn = document.getElementById('refreshLogsBtn');
const clearLogsBtn = document.getElementById('clearLogsBtn');

let currentLogs = [];

export async function loadLogs() {
  const status = filterLogStatus ? filterLogStatus.value : 'ALL';
  try {
    const res = await apiGet(`/api/logs?limit=150&status=${status}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    currentLogs = data.logs || [];
    if (tabLogsCount) tabLogsCount.textContent = data.total || 0;
    renderLogs();
  } catch (e) {
    console.error('Logs fetch error:', e);
    showToast('Ошибка загрузки логов', true);
  }
}

function renderLogs() {
  if (currentLogs.length === 0) {
    logsTableBody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align: center; padding: 48px; color: var(--mute);">
          Записи в журнале логов отсутствуют.
        </td>
      </tr>
    `;
    return;
  }

  logsTableBody.innerHTML = currentLogs.map(log => `
    <tr>
      <td style="color: var(--body-mid); font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap;">
        ${new Date(log.timestamp).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'})}
      </td>
      <td>
        <span class="meta-tag" style="font-size: 11px;">${esc(log.event_type)}</span>
      </td>
      <td>
        <div style="font-weight: 500; color: var(--ink);">${esc(log.chat_title || '—')}</div>
      </td>
      <td>
        <span class="status-tag ${esc(log.status)}">${esc(log.status)}</span>
      </td>
      <td style="font-size: 12.5px; color: var(--body); word-break: break-word;">
        ${escapeHtml(log.details || '')}
      </td>
    </tr>
  `).join('');
}

filterLogStatus.addEventListener('change', loadLogs);
refreshLogsBtn.addEventListener('click', loadLogs);
clearLogsBtn.addEventListener('click', async () => {
  if (!confirm('Очистить весь журнал логов в SQLite?')) return;
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