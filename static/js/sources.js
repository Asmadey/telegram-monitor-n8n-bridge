// sources.js — вкладка «Источники»: задача поиска и её каналы (11.7).
//
// До Фазы 11 интерфейс знал только «канал = монитор»: одна ссылка, один
// промпт, один лимит. Источник отвечает на вопрос «что ищем», канал — «где
// ищем», и у каждого канала свои лимит и промпт извлечения: шумный канал
// публикует полсотни постов в день, тихий — пять, и искать в них надо
// разное.
//
// Обработчики вешаются делегированием, а не атрибутом в разметке: при
// script-src 'self' инлайновый обработчик не исполняется, и кнопка молча
// мертва (9.8). Подстановки идут через html``, который экранирует: имя
// канала пишет его владелец, а не сервис (0.4).

import { apiFetch, apiGet } from './api.js';
import { html, raw, formatIntervalHuman, showToast, openModalAnimated, closeModalAnimated } from './render.js';
import { withSecret, clearSecret, fillSecretField } from './secrets.js';
import { setFilterChatOptions } from './messages.js';

const sourcesList = document.getElementById('sourcesList');
const sourcesCount = document.getElementById('sourcesCount');
const tabSourcesCount = document.getElementById('tabSourcesCount');

const addSourceDrawer = document.getElementById('addSourceDrawer');
const toggleAddSourceBtn = document.getElementById('toggleAddSourceBtn');
const closeAddSourceBtn = document.getElementById('closeAddSourceBtn');
const addSourceForm = document.getElementById('addSourceForm');

const sourceModal = document.getElementById('sourceModal');
const sourceModalTitle = document.getElementById('sourceModalTitle');
const editSourceId = document.getElementById('editSourceId');
const editSourceTitle = document.getElementById('editSourceTitle');
const editSourceInterval = document.getElementById('editSourceInterval');
const editSourceStopWords = document.getElementById('editSourceStopWords');
const editSourceAnswerPrompt = document.getElementById('editSourceAnswerPrompt');
const editChannelsList = document.getElementById('editChannelsList');
const editChannelsCount = document.getElementById('editChannelsCount');
const newChannelTarget = document.getElementById('newChannelTarget');
const addChannelBtn = document.getElementById('addChannelBtn');
const saveSourceBtn = document.getElementById('saveSourceBtn');

// Webhook n8n (живёт на вкладке «Интеграция», обслуживается отсюда)
const webhookUrlInput = document.getElementById('webhookUrl');
const autoWebhookInput = document.getElementById('autoWebhook');
const saveWebhookBtn = document.getElementById('saveWebhookBtn');
const testWebhookBtn = document.getElementById('testWebhookBtn');
const clearWebhookBtn = document.getElementById('clearWebhookBtn');
const webhookUrlStatus = document.getElementById('webhookUrlStatus');

// Выбор канала из диалогов аккаунта
const dialogsModal = document.getElementById('dialogsModal');
const dialogsModalBody = document.getElementById('dialogsModalBody');

let currentSources = [];
let editing = null;

toggleAddSourceBtn.addEventListener('click', () => {
  const isOpen = addSourceDrawer.classList.toggle('open');
  toggleAddSourceBtn.textContent = isOpen ? '✕ Закрыть форму' : '+ Создать источник';
  if (isOpen) document.getElementById('sourceTitle').focus();
});

closeAddSourceBtn.addEventListener('click', () => {
  addSourceDrawer.classList.remove('open');
  toggleAddSourceBtn.textContent = '+ Создать источник';
});

export async function loadSources() {
  try {
    const res = await apiGet('/api/sources');
    if (!res.ok) throw new Error('Не удалось загрузить источники');
    const data = await res.json();
    currentSources = data.sources || [];
    renderSources();
    await loadWebhookConfig();
    setFilterChatOptions(
      currentSources.flatMap(s => s.channels.map(c => ({
        chat_id: c.chat_id,
        chat_title: c.chat_title,
      })))
    );
  } catch (e) {
    showToast('Ошибка загрузки источников', true);
  }
}

function channelChips(source) {
  if (!source.channels.length) {
    return html`<span class="clean-pill" title="Источник без каналов не опрашивается">Нет каналов</span>`;
  }
  return source.channels.map(c => html`
    <span class="clean-pill ${c.is_active ? '' : 'clean-pill-muted'}" title="Лимит: ${c.limit} постов за опрос">
      ${c.chat_title || c.chat_target} <span class="clean-pill-sub">• ${c.limit}</span>
    </span>
  `).join('');
}

function renderSources() {
  sourcesCount.textContent = currentSources.length;
  tabSourcesCount.textContent = currentSources.length;

  if (currentSources.length === 0) {
    sourcesList.innerHTML = html`
      <div style="text-align: center; color: var(--mute); padding: 40px; background: var(--canvas); border: 1px solid var(--hairline); border-radius: var(--rounded-md);">
        Источников пока нет. Нажмите <b>«+ Создать источник»</b> — это задача поиска, в которую потом добавляются каналы.
      </div>
    `;
    return;
  }

  sourcesList.innerHTML = currentSources.map(s => html`
    <div class="monitor-row-card ${s.is_active ? '' : 'inactive'}" id="source-${s.public_id}">
      <div class="channel-main-info">
        <div class="channel-title" title="${s.title}">${s.title}</div>
        <div class="channel-target">${s.channels.length} канал(ов)</div>
      </div>

      <div class="channel-meta-group">
        <div class="meta-pills-row">${raw(channelChips(s))}</div>
        <div class="meta-timeline-row">
          <span class="timeline-segment">${raw(formatIntervalHuman(s.interval_minutes))}</span>
          <span class="timeline-divider">•</span>
          <span class="timeline-segment">
            Прогон: ${s.last_run_at ? new Date(s.last_run_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : 'ещё не было'}
          </span>
          ${s.stop_words ? raw(html`<span class="timeline-divider">•</span><span class="timeline-segment">стоп-слова заданы</span>`) : ''}
        </div>
      </div>

      <div class="channel-actions-group" style="display: flex; align-items: center; gap: 8px;">
        <label class="switch" title="Включить / приостановить источник">
          <input type="checkbox" ${s.is_active ? 'checked' : ''} data-action="toggle" data-source-id="${s.public_id}">
          <span class="slider"></span>
        </label>
        <button class="btn btn-primary btn-sm" data-action="run" data-source-id="${s.public_id}" title="Запустить прогон сейчас">⚡ Запустить</button>
        <button class="btn btn-secondary btn-icon-sm" data-action="edit" data-source-id="${s.public_id}" title="Каналы, лимиты и промпты">✎</button>
        <button class="btn btn-danger btn-icon-sm" data-action="delete" data-source-id="${s.public_id}" title="Удалить источник">🗑</button>
      </div>
    </div>
  `).join('');
}

addSourceForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const title = document.getElementById('sourceTitle').value.trim();
  if (!title) return;
  try {
    const res = await apiFetch('/api/sources', {
      method: 'POST',
      body: {
        title,
        interval_minutes: parseInt(document.getElementById('sourceInterval').value, 10),
        stop_words: document.getElementById('sourceStopWords').value,
        answer_prompt: document.getElementById('sourceAnswerPrompt').value,
      },
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Не удалось создать источник');
    }
    const created = await res.json();
    document.getElementById('sourceTitle').value = '';
    addSourceDrawer.classList.remove('open');
    toggleAddSourceBtn.textContent = '+ Создать источник';
    showToast(`Источник «${created.title}» создан — добавьте в него каналы`);
    await loadSources();
    openSourceModal(created.public_id);
  } catch (e) {
    showToast(e.message, true);
  }
});

// --------------------------------------------------------------------------
// Модалка источника
// --------------------------------------------------------------------------

function renderChannelRows() {
  editChannelsCount.textContent = editing.channels.length;
  if (!editing.channels.length) {
    editChannelsList.innerHTML = html`
      <div style="color: var(--mute); font-size: 12.5px; padding: 10px 0;">
        Каналов нет. Источник без каналов не опрашивается.
      </div>
    `;
    return;
  }
  editChannelsList.innerHTML = editing.channels.map(c => html`
    <div class="card" data-channel-id="${c.channel_id}" style="padding: 12px; margin-bottom: 10px;">
      <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px;">
        <b>${c.chat_title || c.chat_target}</b>
        <div style="display: flex; align-items: center; gap: 8px;">
          <label style="font-size: 12px; color: var(--body-mid);">Лимит</label>
          <input type="number" class="channel-limit" min="1" max="200" value="${c.limit}" style="width: 84px;">
          <button class="btn btn-danger btn-icon-sm" data-action="remove-channel" data-channel-id="${c.channel_id}" title="Убрать канал из источника">🗑</button>
        </div>
      </div>
      <textarea class="channel-prompt" rows="3" placeholder="Что извлекать именно из этого канала" style="width: 100%; margin-top: 8px; border: 1px solid var(--hairline); border-radius: var(--rounded-xs); padding: 10px 12px; font-family: inherit; font-size: 13px; resize: vertical;">${c.extract_prompt || ''}</textarea>
    </div>
  `).join('');
}

export function openSourceModal(publicId) {
  const source = currentSources.find(s => s.public_id === publicId);
  if (!source) return;
  editing = JSON.parse(JSON.stringify(source));
  editSourceId.value = source.public_id;
  sourceModalTitle.textContent = `Источник: ${source.title}`;
  editSourceTitle.value = source.title;
  editSourceInterval.value = String(source.interval_minutes || 60);
  editSourceStopWords.value = source.stop_words || '';
  editSourceAnswerPrompt.value = source.answer_prompt || '';
  newChannelTarget.value = '';
  renderChannelRows();
  openModalAnimated(sourceModal);
}

function closeSourceModal() {
  closeModalAnimated(sourceModal);
  editing = null;
}

document.getElementById('closeSourceModalBtn').addEventListener('click', closeSourceModal);
document.getElementById('cancelSourceBtn').addEventListener('click', closeSourceModal);

addChannelBtn.addEventListener('click', async () => {
  if (!editing) return;
  const target = newChannelTarget.value.trim();
  if (!target) {
    showToast('Укажите ссылку на канал', true);
    return;
  }
  addChannelBtn.disabled = true;
  try {
    const res = await apiFetch(`/api/sources/${editing.public_id}/channels`, {
      method: 'POST',
      body: { chat_target: target, limit: 20, extract_prompt: '' },
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Не удалось добавить канал');
    editing.channels.push(body);
    newChannelTarget.value = '';
    renderChannelRows();
    showToast('Канал добавлен — задайте лимит и промпт');
  } catch (e) {
    showToast(e.message, true);
  } finally {
    addChannelBtn.disabled = false;
  }
});

editChannelsList.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action="remove-channel"]');
  if (!button || !editing) return;
  const channelId = Number(button.dataset.channelId);
  try {
    const res = await apiFetch(
      `/api/sources/${editing.public_id}/channels/${channelId}`,
      { method: 'DELETE' }
    );
    if (!res.ok) throw new Error('Не удалось убрать канал');
    editing.channels = editing.channels.filter(c => c.channel_id !== channelId);
    renderChannelRows();
  } catch (e) {
    showToast(e.message, true);
  }
});

saveSourceBtn.addEventListener('click', async () => {
  if (!editing) return;
  if (!editing.channels.length) {
    // Источник без каналов не опрашивается вовсе — сохранять его молча
    // значит показать «сохранено» и не получить ни одного поста
    showToast('Добавьте хотя бы один канал: без них источник не опрашивается', true);
    return;
  }
  saveSourceBtn.disabled = true;
  try {
    const patched = await apiFetch(`/api/sources/${editing.public_id}`, {
      method: 'PATCH',
      body: {
        title: editSourceTitle.value.trim(),
        interval_minutes: parseInt(editSourceInterval.value, 10),
        stop_words: editSourceStopWords.value,
        answer_prompt: editSourceAnswerPrompt.value,
      },
    });
    if (!patched.ok) {
      const err = await patched.json();
      throw new Error(err.detail || 'Не удалось сохранить источник');
    }
    for (const row of editChannelsList.querySelectorAll('[data-channel-id]')) {
      const channelId = Number(row.dataset.channelId);
      const limit = parseInt(row.querySelector('.channel-limit').value, 10) || 20;
      const prompt = row.querySelector('.channel-prompt').value;
      const res = await apiFetch(
        `/api/sources/${editing.public_id}/channels/${channelId}`,
        { method: 'PATCH', body: { limit, extract_prompt: prompt } }
      );
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Не удалось сохранить канал');
      }
    }
    showToast('Источник сохранён');
    closeSourceModal();
    await loadSources();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    saveSourceBtn.disabled = false;
  }
});

// --------------------------------------------------------------------------
// Действия карточки
// --------------------------------------------------------------------------

document.addEventListener('change', (event) => {
  const toggle = event.target.closest('[data-action="toggle"][data-source-id]');
  if (!toggle) return;
  toggleSource(toggle.dataset.sourceId, toggle.checked);
});

document.addEventListener('click', (event) => {
  const el = event.target.closest('[data-action][data-source-id]');
  if (!el) return;
  const id = el.dataset.sourceId;
  if (!id || id === 'undefined') {
    showToast('Не удалось определить источник — обновите страницу', true);
    return;
  }
  if (el.dataset.action === 'run') runSource(id, el);
  else if (el.dataset.action === 'edit') openSourceModal(id);
  else if (el.dataset.action === 'delete') deleteSource(id);
});

async function toggleSource(id, isActive) {
  try {
    const res = await apiFetch(`/api/sources/${id}`, {
      method: 'PATCH',
      body: { is_active: isActive },
    });
    if (res.ok) showToast(isActive ? 'Источник включён' : 'Источник приостановлен');
  } catch (e) {
    showToast('Не удалось изменить состояние', true);
  }
}

async function runSource(id, button) {
  // Кнопка гаснет до ответа сервера: сервер идемпотентен (11.6), но и
  // выглядеть работающей, пока прогон идёт, она не должна.
  if (button) {
    button.disabled = true;
    button.textContent = '⏳ Идёт прогон';
  }
  try {
    const res = await apiFetch(`/api/sources/${id}/run`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Не удалось запустить прогон');
    // Ответ 202 «queued», а не «готово»: работу делает воркер, и сообщать
    // об успехе здесь значило бы врать о том, чего ещё не случилось (8.х).
    if (data.status === 'queued') {
      showToast('Прогон поставлен в очередь — результат появится в ленте');
    }
    await waitForRun(id, button);
  } catch (e) {
    showToast(e.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = '⚡ Запустить';
    }
  }
}

async function waitForRun(id, button, attempt = 0) {
  // Состояние спрашивается у сервера, а не угадывается: прогон мог
  // запустить другой сеанс, и оптимистичная отрисовка врала бы.
  if (attempt > 60) {
    if (button) {
      button.disabled = false;
      button.textContent = '⚡ Запустить';
    }
    return;
  }
  await new Promise(resolve => setTimeout(resolve, 3000));
  try {
    const res = await apiGet(`/api/sources/${id}/status`);
    if (!res.ok) return;
    const status = await res.json();
    if (!status.running) {
      if (button) {
        button.disabled = false;
        button.textContent = '⚡ Запустить';
      }
      await loadSources();
      return;
    }
  } catch (e) {
    /* сеть моргнула — попробуем на следующем круге */
  }
  await waitForRun(id, button, attempt + 1);
}

async function deleteSource(id) {
  const source = currentSources.find(s => s.public_id === id);
  const name = source ? source.title : 'источник';
  if (!confirm(`Удалить источник «${name}»? Лента и история дедупликации сохранятся.`)) return;
  try {
    const res = await apiFetch(`/api/sources/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Не удалось удалить источник');
    showToast('Источник удалён');
    await loadSources();
  } catch (e) {
    showToast(e.message, true);
  }
}

// --------------------------------------------------------------------------
// n8n webhook (карточка на вкладке «Интеграция»)
// --------------------------------------------------------------------------

export async function loadWebhookConfig() {
  try {
    const res = await apiGet('/api/webhook');
    if (!res.ok) return;
    const data = await res.json();
    autoWebhookInput.checked = Boolean(data.auto_webhook_enabled);
    fillSecretField(webhookUrlInput, webhookUrlStatus, {
      present: data.has_webhook, masked: data.webhook_url_masked,
      value: data.webhook_url || '', noun: 'Адрес'
    });
  } catch (e) {
    console.error('Error loading webhook config:', e);
  }
}

async function saveWebhook(quiet) {
  const auto = autoWebhookInput.checked;
  try {
    const res = await apiFetch('/api/webhook', {
      method: 'POST',
      body: withSecret({ auto_webhook_enabled: auto }, 'webhook_url', webhookUrlInput)
    });
    if (res.ok) {
      showToast(quiet
        ? `Отправка в n8n Webhook: ${auto ? 'Включена' : 'Отключена'}`
        : 'Настройки Webhook сохранены');
      loadWebhookConfig();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Ошибка сохранения', true);
    }
  } catch (e) {
    showToast('Ошибка сохранения', true);
  }
}

autoWebhookInput.addEventListener('change', () => saveWebhook(true));
saveWebhookBtn.addEventListener('click', () => saveWebhook(false));

if (clearWebhookBtn) {
  clearWebhookBtn.addEventListener('click', async () => {
    if (!confirm('Удалить сохранённый адрес вебхука? Отправка в n8n перестанет работать.')) return;
    try {
      const res = await clearSecret('/api/webhook', 'webhook_url');
      if (res.ok) {
        showToast('Адрес вебхука удалён');
        loadWebhookConfig();
      } else {
        showToast('Не удалось удалить адрес', true);
      }
    } catch (e) {
      showToast('Ошибка: ' + e.message, true);
    }
  });
}

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

// --------------------------------------------------------------------------
// Выбор канала из диалогов аккаунта
// --------------------------------------------------------------------------

const openDialogsBtn = document.getElementById('openDialogsModalBtn');
if (openDialogsBtn) {
  openDialogsBtn.addEventListener('click', async () => {
    openModalAnimated(dialogsModal);
    dialogsModalBody.innerHTML = html`<div style="text-align: center; padding: 24px; color: var(--mute);">Загрузка диалогов...</div>`;
    try {
      const res = await apiGet('/api/telegram/dialogs?limit=30');
      const data = await res.json();
      dialogsModalBody.innerHTML = (data.dialogs || []).map(d => html`
        <div class="autocomplete-item" data-dialog-target="${d.username ? '@' + d.username : String(d.id)}">
          <div><div class="autocomplete-item-name">${d.name}</div>
          <div class="autocomplete-item-id">${d.username ? '@' + d.username : d.id}</div></div>
        </div>
      `).join('');
    } catch (e) {
      dialogsModalBody.innerHTML = html`<div style="color: var(--accent-red); padding: 20px;">Ошибка загрузки диалогов</div>`;
    }
  });
}

dialogsModalBody.addEventListener('click', (event) => {
  const item = event.target.closest('[data-dialog-target]');
  if (!item) return;
  newChannelTarget.value = item.dataset.dialogTarget;
  closeModalAnimated(dialogsModal);
});

const closeDialogs = document.getElementById('closeDialogsModal');
if (closeDialogs) closeDialogs.addEventListener('click', () => closeModalAnimated(dialogsModal));
