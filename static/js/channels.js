// channels.js — вкладка «Каналы»: список мониторов, добавление/правка/
// удаление, ручной запуск, настройки вебхука n8n, выбор диалога
// (задача 5.1, разрез index.html).
//
// Действия карточки вызываются из inline-onclick шаблона — toggleMonitor
// и компания обязаны быть на window (ES-модули file-scoped).

import { apiFetch, apiGet } from './api.js';
import { html, raw, formatIntervalHuman, formatNextRun, showToast, closeModalAnimated } from './render.js';
import { setFilterChatOptions, mergeMessages } from './messages.js';

const monitorsList = document.getElementById('monitorsList');
const monitorsCount = document.getElementById('monitorsCount');
const tabChannelsCount = document.getElementById('tabChannelsCount');
const refreshAllBtn = document.getElementById('refreshAllBtn');

// Add Channel Drawer
const addChannelDrawer = document.getElementById('addChannelDrawer');
const toggleAddChannelBtn = document.getElementById('toggleAddChannelBtn');
const closeAddChannelBtn = document.getElementById('closeAddChannelBtn');
const addMonitorForm = document.getElementById('addMonitorForm');

// Edit Channel Modal
const editMonitorModal = document.getElementById('editMonitorModal');
const editModalTitle = document.getElementById('editModalTitle');
const editMonitorId = document.getElementById('editMonitorId');
const editIntervalMin = document.getElementById('editIntervalMin');
const editMsgLimit = document.getElementById('editMsgLimit');
const closeEditModalBtn = document.getElementById('closeEditModalBtn');
const cancelEditBtn = document.getElementById('cancelEditBtn');
const saveEditBtn = document.getElementById('saveEditBtn');

// Webhook n8n
const webhookUrlInput = document.getElementById('webhookUrl');
const autoWebhookInput = document.getElementById('autoWebhook');
const saveWebhookBtn = document.getElementById('saveWebhookBtn');
const testWebhookBtn = document.getElementById('testWebhookBtn');

// Dialogs Modal
const dialogsModal = document.getElementById('dialogsModal');
const dialogsModalBody = document.getElementById('dialogsModalBody');

let currentMonitors = [];

// Toggle Add Channel Drawer
toggleAddChannelBtn.addEventListener('click', () => {
  const isOpen = addChannelDrawer.classList.toggle('open');
  toggleAddChannelBtn.textContent = isOpen ? '✕ Закрыть форму' : '➕ Добавить канал';
  toggleAddChannelBtn.classList.toggle('btn-secondary', isOpen);
  toggleAddChannelBtn.classList.toggle('btn-primary', !isOpen);
  if (isOpen) {
    document.getElementById('chatTarget').focus();
  }
});

closeAddChannelBtn.addEventListener('click', () => {
  addChannelDrawer.classList.remove('open');
  toggleAddChannelBtn.textContent = '➕ Добавить канал';
  toggleAddChannelBtn.className = 'btn btn-primary btn-sm';
});

// Load Monitors from SQLite
export async function loadConfig() {
  try {
    const res = await apiGet('/api/monitors');
    const data = await res.json();
    webhookUrlInput.value = data.webhook_url || '';
    autoWebhookInput.checked = data.auto_webhook_enabled ?? true;
    currentMonitors = data.monitors || [];
    renderMonitors();
  } catch (e) {
    showToast('Ошибка загрузки конфигурации', true);
  }
}

// Render Full-Width Horizontal Channels List
function renderMonitors() {
  monitorsCount.textContent = currentMonitors.length;
  tabChannelsCount.textContent = currentMonitors.length;

  if (currentMonitors.length === 0) {
    monitorsList.innerHTML = html`
      <div style="text-align: center; color: var(--mute); padding: 40px; background: var(--canvas); border: 1px solid var(--hairline); border-radius: var(--rounded-md);">
        Нет добавленных каналов. Нажмите <b>«➕ Добавить канал»</b> выше, чтобы настроить первый источник!
      </div>
    `;
    return;
  }

  monitorsList.innerHTML = currentMonitors.map(m => html`
    <div class="monitor-row-card ${m.is_active ? '' : 'inactive'}" id="monitor-${m.id}">

      <!-- Column 1: Channel Info -->
      <div class="channel-main-info">
        <div class="channel-title" title="${m.chat_title}">
          ${m.chat_title}
        </div>
        <div class="channel-target">
          ${m.chat_username ? '@' + m.chat_username : 'ID: ' + m.chat_id}
        </div>
      </div>

      <!-- Column 2: Clean Structured Metadata -->
      <div class="channel-meta-group">
        <div class="meta-pills-row">
          <span class="clean-pill clean-pill-accent" title="Отправлено сообщений с дедубликацией">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
            Отправлено: <b>${m.sent_count || 0}</b> <span class="clean-pill-sub">• ID ${m.last_sent_message_id || 0}</span>
          </span>
          <span class="clean-pill" title="Лимит сообщений за один опрос">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
            Лимит: <b>${m.limit}</b>
          </span>
          ${m.prompt ? raw(html`
            <span class="clean-pill clean-pill-purple" title="Кастомный системный промпт: ${m.prompt}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>
              LLM Промпт
            </span>
          `) : ''}
        </div>

        <div class="meta-timeline-row">
          <span class="timeline-segment" title="Частота автоматического опроса">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            ${raw(formatIntervalHuman(m.interval_minutes))}
          </span>
          <span class="timeline-divider">•</span>
          <span class="timeline-segment" title="Время последней проверки канала">
            Проверен: ${m.last_checked ? new Date(m.last_checked).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}) : 'еще нет'}
          </span>
          <span class="timeline-divider">•</span>
          <span class="timeline-next-status ${m.is_active ? 'active' : ''}" title="Расчетное время следующего запуска">
            <span class="pulse-dot ${m.is_active ? '' : 'paused'}"></span>
            След: ${raw(formatNextRun(m))}
          </span>
        </div>
      </div>

      <!-- Column 3: Actions & Toggle -->
      <div class="channel-actions-group" style="display: flex; align-items: center; gap: 8px;">
        <label class="switch" title="Включить / Приостановить мониторинг">
          <input type="checkbox" ${m.is_active ? 'checked' : ''} onchange="toggleMonitor('${m.id}', this.checked)">
          <span class="slider"></span>
        </label>
        <button class="btn btn-primary btn-sm" onclick="runMonitor('${m.id}')" title="Запустить опрос сейчас">⚡ Запустить</button>
        <button class="btn btn-secondary btn-icon-sm" onclick="openEditModal('${m.id}')" title="Редактировать параметры и промпт">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>
        </button>
        <button class="btn btn-secondary btn-icon-sm" onclick="resetDedup('${m.id}')" title="Сбросить историю дубликатов">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
        </button>
        <button class="btn btn-danger btn-icon-sm" onclick="deleteMonitor('${m.id}')" title="Удалить канал из мониторинга">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>
        </button>
      </div>

    </div>
  `).join('');

  setFilterChatOptions(currentMonitors);

  if (window.gsap && currentMonitors.length > 0) {
    gsap.from(".channel-card", {
      autoAlpha: 0,
      y: 8,
      duration: 0.22,
      stagger: 0.03,
      ease: "power2.out"
    });
  }
}

// Периодический пересчет таймеров обратного отсчета — без данных не дергаем DOM
export function refreshMonitorTimers() {
  if (currentMonitors.length > 0) {
    renderMonitors();
  }
}

// Modal Edit Functions
function openEditModal(id) {
  const m = currentMonitors.find(item => item.id === id);
  if (!m) return;

  editMonitorId.value = m.id;
  editModalTitle.textContent = `Редактирование: ${m.chat_title}`;
  editIntervalMin.value = String(m.interval_minutes || 60);
  editMsgLimit.value = m.limit || 20;
  document.getElementById('editMonitorPrompt').value = m.prompt || '';
}

window.openEditModal = openEditModal;

function closeEditModal() {
  closeModalAnimated(editMonitorModal);
}

closeEditModalBtn.addEventListener('click', closeEditModal);
cancelEditBtn.addEventListener('click', closeEditModal);

saveEditBtn.addEventListener('click', async () => {
  const id = editMonitorId.value;
  const interval = parseInt(editIntervalMin.value);
  const limit = parseInt(editMsgLimit.value);
  const promptVal = document.getElementById('editMonitorPrompt').value.trim();

  if (!limit || limit < 1 || limit > 100) {
    showToast('Лимит должен быть от 1 до 100', true);
    return;
  }

  saveEditBtn.disabled = true;
  saveEditBtn.textContent = 'Сохранение...';

  try {
    const res = await apiFetch(`/api/monitors/${id}`, {
      method: 'PATCH',
      body: {
        interval_minutes: interval,
        limit: limit,
        prompt: promptVal
      }
    });

    saveEditBtn.disabled = false;
    saveEditBtn.textContent = '💾 Сохранить изменения';

    if (res.ok) {
      showToast('Параметры источника успешно обновлены!');
      closeEditModal();
      loadConfig();
    } else {
      showToast('Ошибка сохранения параметров', true);
    }
  } catch (e) {
    saveEditBtn.disabled = false;
    saveEditBtn.textContent = '💾 Сохранить изменения';
    showToast('Ошибка: ' + e.message, true);
  }
});

async function toggleMonitor(id, isActive) {
  try {
    const res = await apiFetch(`/api/monitors/${id}`, {
      method: 'PATCH',
      body: { is_active: isActive }
    });
    if (res.ok) {
      showToast(isActive ? 'Мониторинг активен' : 'Мониторинг на паузе');
      loadConfig();
    }
  } catch (e) {
    showToast('Ошибка обновления статуса', true);
  }
}

window.toggleMonitor = toggleMonitor;

async function resetDedup(id) {
  if (!confirm('Сбросить историю отправленных ID для этого канала в SQLite?')) return;
  try {
    const res = await apiFetch(`/api/monitors/${id}/reset-dedup`, { method: 'POST' });
    if (res.ok) {
      showToast('История дедубликации сброшена в SQLite!');
      loadConfig();
    }
  } catch (e) {
    showToast('Ошибка сброса истории', true);
  }
}

window.resetDedup = resetDedup;

async function deleteMonitor(id) {
  if (!confirm('Удалить этот канал из мониторинга?')) return;
  try {
    const res = await apiFetch(`/api/monitors/${id}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Канал удален');
      loadConfig();
    }
  } catch (e) {
    showToast('Ошибка удаления', true);
  }
}

window.deleteMonitor = deleteMonitor;

async function runMonitor(id) {
  const card = document.getElementById(`monitor-${id}`);
  if (card) card.style.opacity = '0.5';
  showToast('Загрузка сообщений из Telegram...');

  try {
    const res = await apiFetch(`/api/monitors/${id}/run`, { method: 'POST' });
    const data = await res.json();
    if (card) card.style.opacity = '1';

    if (data.messages) {
      if (data.new_messages_count > 0 && data.sent_to_webhook) {
        showToast(`✅ Отправлено ${data.new_messages_count} новых постов в n8n (${data.duplicates_filtered} дублей отфильтровано)`);
      } else if (data.duplicates_filtered > 0 && data.new_messages_count === 0) {
        showToast(`ℹ️ Новых постов нет (все ${data.total_fetched} сообщений уже отправлялись)`);
      } else {
        showToast(`Извлечено ${data.messages.length} сообщений`);
      }

      mergeMessages(data.messages, {
        chat_title: data.chat_title,
        chat_username: data.chat_username,
        chat_id: data.chat_id
      });

      loadConfig();
    }
  } catch (e) {
    if (card) card.style.opacity = '1';
    showToast('Ошибка: ' + e.message, true);
  }
}

window.runMonitor = runMonitor;

refreshAllBtn.addEventListener('click', async () => {
  if (currentMonitors.length === 0) {
    showToast('Список каналов пуст', true);
    return;
  }
  showToast('Обновление всех активных каналов...');
  for (const m of currentMonitors) {
    if (m.is_active) {
      await runMonitor(m.id);
    }
  }
});

addMonitorForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const target = document.getElementById('chatTarget').value.trim();
  const interval = parseInt(document.getElementById('intervalMin').value);
  const limit = parseInt(document.getElementById('msgLimit').value);

  if (!target) return;
  showToast('Подключение к каналу...');

  try {
    const promptVal = document.getElementById('channelPrompt').value.trim();
    const res = await apiFetch('/api/monitors', {
      method: 'POST',
      body: {
        chat_target: target,
        interval_minutes: interval,
        limit: limit,
        prompt: promptVal
      }
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Не удалось добавить');
    }

    const data = await res.json();
    showToast(`Канал "${data.chat_title}" добавлен в SQLite!`);
    document.getElementById('chatTarget').value = '';
    document.getElementById('channelPrompt').value = '';
    addChannelDrawer.classList.remove('open');
    toggleAddChannelBtn.textContent = '➕ Добавить канал';
    toggleAddChannelBtn.className = 'btn btn-primary btn-sm';
    loadConfig();
    runMonitor(data.id);
  } catch (e) {
    showToast(e.message, true);
  }
});

autoWebhookInput.addEventListener('change', async () => {
  const url = webhookUrlInput.value.trim();
  const auto = autoWebhookInput.checked;
  try {
    const res = await apiFetch('/api/webhook', {
      method: 'POST',
      body: { webhook_url: url, auto_webhook_enabled: auto }
    });
    if (res.ok) {
      showToast(`Отправка в n8n Webhook: ${auto ? 'Включена' : 'Отключена'}`);
    }
  } catch (e) {
    showToast('Ошибка сохранения', true);
  }
});

saveWebhookBtn.addEventListener('click', async () => {
  const url = webhookUrlInput.value.trim();
  const auto = autoWebhookInput.checked;
  try {
    const res = await apiFetch('/api/webhook', {
      method: 'POST',
      body: { webhook_url: url, auto_webhook_enabled: auto }
    });
    if (res.ok) showToast('Настройки Webhook сохранены в SQLite');
  } catch (e) {
    showToast('Ошибка сохранения', true);
  }
});

testWebhookBtn.addEventListener('click', async () => {
  showToast('Отправка тестового запроса в n8n...');
  try {
    const res = await apiFetch('/api/webhook/test', { method: 'POST' });
    if (res.ok) {
      showToast('✅ Тестовый вебхук успешно принят n8n!');
    } else {
      const err = await res.json();
      showToast('Ошибка n8n: ' + (err.detail || 'Проверьте статус воркфлоу'), true);
    }
  } catch (e) {
    showToast('Не удалось связаться с n8n', true);
  }
});

// Dialogs Modal
document.getElementById('openDialogsModalBtn').addEventListener('click', async () => {
  dialogsModal.classList.add('active');
  dialogsModalBody.innerHTML = '<div style="text-align: center; padding: 24px; color: var(--mute);">Загрузка диалогов...</div>';
  try {
    const res = await apiGet('/dialogs?limit=30');
    const data = await res.json();
    if (data.dialogs) {
      dialogsModalBody.innerHTML = data.dialogs.map(d => html`
        <div class="dialog-item">
          <div>
            <div style="font-weight: 600; color: var(--ink);">${d.name}</div>
            <div style="font-size: 11px; color: var(--body-mid);">
              ${d.username ? '@' + d.username : 'ID: ' + d.id} • Тип: ${d.type}
            </div>
          </div>
          <button class="btn btn-secondary btn-sm" data-dialog-select="${d.username ? '@' + d.username : String(d.id)}">+ Выбрать</button>
        </div>
      `).join('');
    }
  } catch (e) {
    dialogsModalBody.innerHTML = '<div style="color: var(--accent-red); padding: 20px;">Ошибка загрузки диалогов</div>';
  }
});

function selectDialog(target) {
  document.getElementById('chatTarget').value = target;
  closeModalAnimated(dialogsModal);
  showToast(`Выбран: ${target}`);
  addChannelDrawer.classList.add('open');
  toggleAddChannelBtn.textContent = '✕ Закрыть форму';
  toggleAddChannelBtn.className = 'btn btn-secondary btn-sm';
}

// Выбор диалога через data-атрибут и делегирование: значение пользователя
// не попадает в inline-JS (экранирование атрибута не защищает JS-строку
// внутри onclick — парсер HTML декодирует &amp;#39; обратно в кавычку).
dialogsModalBody.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-dialog-select]');
  if (btn) selectDialog(btn.dataset.dialogSelect);
});

document.getElementById('closeDialogsModal').addEventListener('click', () => closeModalAnimated(dialogsModal));