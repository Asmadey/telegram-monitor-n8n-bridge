// messages.js — вкладка «Сообщения»: таблица, фильтры, JSON-модалка,
// выгрузка в n8n (задача 5.1, разрез index.html).
//
// currentMessages живёт здесь: каналам (runMonitor) отдаются
// mergeMessages и setFilterChatOptions — ребро channels → messages,
// без цикла импортов.

import { apiFetch, apiGet } from './api.js';
import { html, raw, formatTelegramText, showToast, openModalAnimated, closeModalAnimated } from './render.js';

const tabMessagesCount = document.getElementById('tabMessagesCount');
const messagesTableBody = document.getElementById('messagesTableBody');
const tableSearch = document.getElementById('tableSearch');
const filterChatSelect = document.getElementById('filterChatSelect');
const viewJsonBtn = document.getElementById('viewJsonBtn');
const jsonModal = document.getElementById('jsonModal');
const jsonViewerCode = document.getElementById('jsonViewerCode');

const filterMinViews = document.getElementById('filterMinViews');
const filterMinReactions = document.getElementById('filterMinReactions');
const sortMessagesSelect = document.getElementById('sortMessagesSelect');

let currentMessages = [];

export function getFilteredMessages() {
  const query = tableSearch.value.toLowerCase();
  const filterChat = filterChatSelect.value;
  const minViews = parseInt(String(filterMinViews.value || '').replace(/\D/g, '')) || 0;
  const minReactions = parseInt(String(filterMinReactions.value || '').replace(/\D/g, '')) || 0;
  const sortBy = sortMessagesSelect.value;

  let filtered = currentMessages.filter(msg => {
    const text = (msg.text || '').trim();
    // Фильтрация сообщений без текста / пустых медиа
    if (!text || text === '📎 [Медиа/Вложение]') return false;

    const matchText = text.toLowerCase().includes(query) || (msg.sender || '').toLowerCase().includes(query);
    const matchChat = filterChat === 'ALL' || String(msg.chat_id) === String(filterChat);
    const matchViews = (msg.views || 0) >= minViews;
    const matchReactions = (msg.reactions_count || 0) >= minReactions;

    return matchText && matchChat && matchViews && matchReactions;
  });

  filtered.sort((a, b) => {
    if (sortBy === 'views_desc') return (b.views || 0) - (a.views || 0);
    if (sortBy === 'reactions_desc') return (b.reactions_count || 0) - (a.reactions_count || 0);
    if (sortBy === 'date_asc') return new Date(a.date) - new Date(b.date);
    return new Date(b.date) - new Date(a.date);
  });

  return filtered;
}

export function renderTable() {
  const filtered = getFilteredMessages();
  tabMessagesCount.textContent = filtered.length;

  if (filtered.length === 0) {
    messagesTableBody.innerHTML = html`
      <tr>
        <td colspan="6" style="text-align: center; padding: 48px; color: var(--mute);">
          Сообщения по заданным фильтрам не найдены.
        </td>
      </tr>
    `;
    return;
  }

  messagesTableBody.innerHTML = filtered.map(msg => {
    const reactionsList = Array.isArray(msg.reactions) ? msg.reactions : [];
    const hasReactions = (msg.reactions_count && msg.reactions_count > 0) || reactionsList.length > 0;
    const totalReactions = msg.reactions_count || reactionsList.reduce((sum, r) => sum + (r.count || 0), 0);
    const reactionsTitle = reactionsList.length > 0 ? reactionsList.map(r => (r.emoji || '👍') + ' ' + r.count).join(' ') : `${totalReactions} реакций`;

    return html`
    <tr>
      <td><span class="badge-metric">${msg.id}</span></td>
      <td style="color: var(--body-mid); font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap;">
        ${new Date(msg.date).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'})}
      </td>
      <td>
        <div style="font-weight: 600; color: var(--ink);">${msg.chat_title || 'Канал'}</div>
        <div style="font-size: 11px; color: var(--body-mid);">${msg.sender || ''}</div>
      </td>
      <td class="msg-text-cell">
        <div class="msg-text" id="msg-text-${msg.chat_id}-${msg.id}">${raw(formatTelegramText(msg.text))}</div>
        ${msg.text && msg.text.length > 140 ? raw(html`<button class="expand-btn" onclick="toggleExpand('${msg.chat_id}-${msg.id}')">Развернуть / Свернуть</button>`) : ''}
      </td>
      <td>
        <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
          ${msg.views !== null && msg.views !== undefined ? raw(`<span class="badge-metric" title="Просмотры">👁️ ${Number(msg.views).toLocaleString('ru-RU')}</span>`) : ''}
          ${hasReactions ? raw(html`<span class="badge-metric" style="color: #ed52cb; background: rgba(237, 82, 203, 0.08); border-color: rgba(237, 82, 203, 0.25);" title="${reactionsTitle}">❤️ ${totalReactions}</span>`) : ''}
          ${msg.forwards ? raw(`<span class="badge-metric" title="Пересылки">↗️ ${Number(msg.forwards).toLocaleString('ru-RU')}</span>`) : ''}
          ${msg.has_media ? raw(`<span class="badge-media">📎 Медиа</span>`) : ''}
        </div>
      </td>
      <td>
        ${msg.post_url ? raw(html`
          <a href="${msg.post_url}" target="_blank" class="post-link-btn">
            🔗 Открыть
          </a>
        `) : '<span style="color: var(--mute);">-</span>'}
      </td>
    </tr>
    `;
  }).join('');
}

function toggleExpand(key) {
  const el = document.getElementById(`msg-text-${key}`);
  if (el) el.classList.toggle('expanded');
}

window.toggleExpand = toggleExpand;

export function setFilterChatOptions(monitors) {
  // опции фильтра «Канал» наполняет вкладка каналов после renderMonitors
  const options = monitors.map(m => html`<option value="${m.chat_id}">${m.chat_title}</option>`).join('');
  filterChatSelect.innerHTML = html`<option value="ALL">Все каналы</option>${raw(options)}`;
}

export function mergeMessages(messages, meta) {
  // ручной запуск канала приносит свежие посты прямо в таблицу
  const newFormatted = messages.map(msg => ({ ...msg, ...meta }));
  newFormatted.forEach(msg => {
    const idx = currentMessages.findIndex(m => String(m.chat_id) === String(msg.chat_id) && String(m.id) === String(msg.id));
    if (idx >= 0) {
      currentMessages[idx] = { ...currentMessages[idx], ...msg };
    } else {
      currentMessages.unshift(msg);
    }
  });
  renderTable();
}

export async function loadSavedMessages() {
  try {
    const res = await apiGet('/api/messages?limit=100');
    const data = await res.json();
    if (data.messages && data.messages.length > 0) {
      currentMessages = data.messages;
      renderTable();
    }
  } catch (e) {
    console.error('Error loading saved messages:', e);
  }
}

tableSearch.addEventListener('input', renderTable);
filterChatSelect.addEventListener('change', renderTable);
filterMinViews.addEventListener('input', renderTable);
filterMinReactions.addEventListener('input', renderTable);
sortMessagesSelect.addEventListener('change', renderTable);

document.getElementById('sendTableToN8nBtn').addEventListener('click', async () => {
  const filtered = getFilteredMessages();

  if (filtered.length === 0) {
    showToast('Нет сообщений для отправки!', true);
    return;
  }

  // Группируем сообщения по уникальным каналам (chat_id)
  const groupedByChat = {};
  filtered.forEach(msg => {
    const cId = msg.chat_id || 'unknown';
    if (!groupedByChat[cId]) {
      groupedByChat[cId] = {
        chat_id: msg.chat_id,
        chat_title: msg.chat_title,
        chat_username: msg.chat_username,
        messages: []
      };
    }
    groupedByChat[cId].messages.push(msg);
  });

  const chatEntries = Object.values(groupedByChat);
  showToast(`Отправка ${chatEntries.length} отдельн. вебхуков по каждому каналу в n8n...`);

  let successCount = 0;
  for (const entry of chatEntries) {
    try {
      const res = await apiFetch('/api/webhook/send-payload', {
        method: 'POST',
        body: {
          source: "telethon_monitor",
          event: "telegram_messages_batch",
          timestamp: new Date().toISOString(),
          chat_id: entry.chat_id,
          chat_title: entry.chat_title,
          chat_username: entry.chat_username,
          messages_count: entry.messages.length,
          messages: entry.messages
        }
      });
      if (res.ok) successCount++;
    } catch (e) {
      console.error(e);
    }
  }

  if (successCount === chatEntries.length) {
    showToast(`✅ Отправлено ${successCount} отдельных вебхуков по каналам в n8n!`);
  } else {
    showToast(`Отправлено ${successCount} из ${chatEntries.length} вебхуков`, true);
  }
});

viewJsonBtn.addEventListener('click', () => {
  const payload = {
    chat_title: currentMessages[0]?.chat_title || "Текущая выборка",
    messages_count: currentMessages.length,
    messages: currentMessages
  };
  jsonViewerCode.textContent = JSON.stringify(payload, null, 2);
  openModalAnimated(jsonModal);
});

document.getElementById('closeJsonModal').addEventListener('click', () => closeModalAnimated(jsonModal));
document.getElementById('copyJsonBtn').addEventListener('click', () => {
  navigator.clipboard.writeText(jsonViewerCode.textContent);
  showToast('JSON скопирован в буфер обмена!');
});
document.getElementById('downloadJsonBtn').addEventListener('click', () => {
  const blob = new Blob([jsonViewerCode.textContent], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `telegram_export_${Date.now()}.json`;
  a.click();
});