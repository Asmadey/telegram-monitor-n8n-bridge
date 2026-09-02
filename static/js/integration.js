// integration.js — вкладка «Интеграции»: OpenRouter (каталог моделей,
// тест LLM), Telegram-бот, JSON-пример вебхука, автоочистка БД
// (задача 5.1, разрез index.html).
//
// selectModelItem и quickFilterModel вызываются из inline-JS разметки —
// обязаны быть на window (ES-модули file-scoped).

import { apiFetch, apiGet } from './api.js';
import { escapeHtml, showToast, openModalAnimated, closeModalAnimated } from './render.js';
import { loadLogs } from './logs.js';

// OpenRouter
const openrouterBaseUrl = document.getElementById('openrouterBaseUrl');
const openrouterModel = document.getElementById('openrouterModel');
const openrouterModelsDropdown = document.getElementById('openrouterModelsDropdown');
const modelDropdownTrigger = document.getElementById('modelDropdownTrigger');
const refreshModelsBtn = document.getElementById('refreshModelsBtn');
const openrouterApiKey = document.getElementById('openrouterApiKey');
const toggleApiKeyVisibility = document.getElementById('toggleApiKeyVisibility');
const openrouterEnabled = document.getElementById('openrouterEnabled');
const saveOpenRouterBtn = document.getElementById('saveOpenRouterBtn');
const testOpenRouterBtn = document.getElementById('testOpenRouterBtn');
const openrouterTestResultBox = document.getElementById('openrouterTestResultBox');
const openrouterTestResultText = document.getElementById('openrouterTestResultText');

// Telegram Bot Forward
const tgBotToken = document.getElementById('tgBotToken');
const toggleTgBotTokenVisibility = document.getElementById('toggleTgBotTokenVisibility');
const tgSenderId = document.getElementById('tgSenderId');
const tgForwardEnabled = document.getElementById('tgForwardEnabled');
const saveTgForwardBtn = document.getElementById('saveTgForwardBtn');
const testTgForwardBtn = document.getElementById('testTgForwardBtn');

// JSON Example Modal
const jsonExampleModal = document.getElementById('jsonExampleModal');
const openJsonExampleModalBtn = document.getElementById('openJsonExampleModalBtn');
const closeJsonExampleModalBtn = document.getElementById('closeJsonExampleModalBtn');
const closeJsonExampleBtn2 = document.getElementById('closeJsonExampleBtn2');
const copyJsonExampleBtn = document.getElementById('copyJsonExampleBtn');
const jsonExampleCode = document.getElementById('jsonExampleCode');

// Auto-Cleanup
const cleanupModal = document.getElementById('cleanupModal');
const openCleanupModalBtn = document.getElementById('openCleanupModalBtn');
const closeCleanupModalBtn = document.getElementById('closeCleanupModalBtn');
const closeCleanupModalBtn2 = document.getElementById('closeCleanupModalBtn2');
const cleanupEnabledInput = document.getElementById('cleanupEnabledInput');
const cleanupDaysSelect = document.getElementById('cleanupDaysSelect');
const cleanupStatusLabel = document.getElementById('cleanupStatusLabel');
const cleanupLastRunDate = document.getElementById('cleanupLastRunDate');
const saveCleanupConfigBtn = document.getElementById('saveCleanupConfigBtn');
const runCleanupNowBtn = document.getElementById('runCleanupNowBtn');

let allOpenRouterModels = [
  { id: "deepseek/deepseek-v4-flash", name: "DeepSeek: DeepSeek V4 Flash" },
  { id: "deepseek/deepseek-chat", name: "DeepSeek: DeepSeek V3 (Chat)" },
  { id: "deepseek/deepseek-r1", name: "DeepSeek: DeepSeek R1 (Reasoning)" },
  { id: "deepseek/deepseek-r1-distill-llama-70b", name: "DeepSeek: R1 Distill Llama 70B" },
  { id: "deepseek/deepseek-r1-distill-qwen-32b", name: "DeepSeek: R1 Distill Qwen 32B" },
  { id: "deepseek/deepseek-coder", name: "DeepSeek: DeepSeek Coder" },
  { id: "google/gemini-2.0-flash-001", name: "Google: Gemini 2.0 Flash" },
  { id: "google/gemini-2.5-pro", name: "Google: Gemini 2.5 Pro" },
  { id: "google/gemini-flash-1.5", name: "Google: Gemini 1.5 Flash" },
  { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet" },
  { id: "anthropic/claude-3.5-haiku", name: "Anthropic: Claude 3.5 Haiku" },
  { id: "openai/gpt-4o", name: "OpenAI: GPT-4o" },
  { id: "openai/gpt-4o-mini", name: "OpenAI: GPT-4o Mini" },
  { id: "openai/o3-mini", name: "OpenAI: o3-mini" },
  { id: "meta-llama/llama-3.3-70b-instruct", name: "Meta: Llama 3.3 70B Instruct" },
  { id: "meta-llama/llama-3.1-405b-instruct", name: "Meta: Llama 3.1 405B Instruct" },
  { id: "qwen/qwen-2.5-72b-instruct", name: "Qwen: Qwen 2.5 72B Instruct" },
  { id: "mistralai/mistral-large-2411", name: "Mistral: Mistral Large 2411" },
  { id: "x-ai/grok-2-1212", name: "xAI: Grok 2" }
];
let highlightedModelIndex = -1;

toggleApiKeyVisibility.addEventListener('click', () => {
  openrouterApiKey.type = openrouterApiKey.type === 'password' ? 'text' : 'password';
  toggleApiKeyVisibility.textContent = openrouterApiKey.type === 'password' ? '👁️' : '🙈';
});

function selectModelItem(modelId) {
  openrouterModel.value = modelId;
  hideModelsDropdown();
  showToast(`Выбрана модель: ${modelId}`);
}

window.selectModelItem = selectModelItem;

function hideModelsDropdown() {
  openrouterModelsDropdown.style.display = 'none';
  highlightedModelIndex = -1;
}

function showModelsDropdown() {
  openrouterModelsDropdown.style.display = 'block';
}

function highlightText(text, query) {
  if (!query) return escapeHtml(text);
  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escapedQuery})`, 'gi');
  return escapeHtml(text).replace(regex, '<span style="background: #fff3a8; color: #78350f; font-weight: 700; border-radius: 2px; padding: 0 2px;">$1</span>');
}

function renderModelsDropdown(query = '') {
  const q = query.trim().toLowerCase();
  let filtered = allOpenRouterModels;

  if (q) {
    filtered = allOpenRouterModels.filter(m =>
      m.id.toLowerCase().includes(q) || (m.name && m.name.toLowerCase().includes(q))
    );
  }

  if (filtered.length === 0) {
    openrouterModelsDropdown.innerHTML = `
      <div style="padding: 14px; text-align: center; color: var(--mute); font-size: 12.5px;">
        Модели по запросу <b>"${escapeHtml(query)}"</b> не найдены в каталоге.<br>
        <span style="font-size: 11px; color: var(--body-mid);">Вы можете использовать введенный ID модели.</span>
      </div>
    `;
    showModelsDropdown();
    return;
  }

  const groups = {
    'DeepSeek': [],
    'Google (Gemini)': [],
    'Anthropic (Claude)': [],
    'OpenAI (ChatGPT)': [],
    'Meta (Llama)': [],
    'Qwen': [],
    'Mistral': [],
    'xAI (Grok)': [],
    'Другие модели': []
  };

  filtered.forEach(m => {
    const id = m.id.toLowerCase();
    if (id.startsWith('deepseek/')) groups['DeepSeek'].push(m);
    else if (id.startsWith('google/')) groups['Google (Gemini)'].push(m);
    else if (id.startsWith('anthropic/')) groups['Anthropic (Claude)'].push(m);
    else if (id.startsWith('openai/')) groups['OpenAI (ChatGPT)'].push(m);
    else if (id.startsWith('meta-llama/')) groups['Meta (Llama)'].push(m);
    else if (id.startsWith('qwen/')) groups['Qwen'].push(m);
    else if (id.startsWith('mistralai/')) groups['Mistral'].push(m);
    else if (id.startsWith('x-ai/')) groups['xAI (Grok)'].push(m);
    else groups['Другие модели'].push(m);
  });

  let html = `<div style="padding: 6px 12px; font-size: 11px; color: #5533ff; background: #f5f3ff; border-bottom: 1px solid var(--hairline); font-weight: 600;">Найдено моделей: ${filtered.length} ${q ? `по запросу "${escapeHtml(q)}"` : ''}</div>`;
  for (const [groupName, groupModels] of Object.entries(groups)) {
    if (groupModels.length > 0) {
      html += `<div class="autocomplete-group-header">🌟 ${groupName} (${groupModels.length})</div>`;
      html += groupModels.map(m => {
        const isCurrent = (m.id === openrouterModel.value.trim());
        // HTML строится в переменных: экранирование выполняется внутри
        // highlightText (через escapeHtml), в разметку уходит готовый безопасный HTML.
        const nameHtml = highlightText(m.name || m.id, q);
        const idHtml = highlightText(m.id, q);
        return `
          <div class="autocomplete-item ${isCurrent ? 'selected' : ''}" data-model-id="${m.id}" onclick="selectModelItem('${m.id}')">
            <div>
              <div class="autocomplete-item-name">${nameHtml}</div>
              <div class="autocomplete-item-id">${idHtml}</div>
            </div>
            ${isCurrent ? '<span style="font-size: 12px; color: #5533ff; font-weight: 700;">✓</span>' : ''}
          </div>
        `;
      }).join('');
    }
  }

  openrouterModelsDropdown.innerHTML = html;
  showModelsDropdown();
}

// Input events for real-time live filtering
openrouterModel.addEventListener('input', () => {
  renderModelsDropdown(openrouterModel.value);
});

openrouterModel.addEventListener('focus', () => {
  renderModelsDropdown(openrouterModel.value);
});

modelDropdownTrigger.addEventListener('click', (e) => {
  e.stopPropagation();
  if (openrouterModelsDropdown.style.display === 'block') {
    hideModelsDropdown();
  } else {
    openrouterModel.focus();
    renderModelsDropdown('');
  }
});

window.quickFilterModel = function(tag) {
  openrouterModel.value = tag;
  openrouterModel.focus();
  renderModelsDropdown(tag);
};

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
  if (!openrouterModel.contains(e.target) &&
      !openrouterModelsDropdown.contains(e.target) &&
      !modelDropdownTrigger.contains(e.target)) {
    hideModelsDropdown();
  }
});

// Keyboard navigation (ArrowDown, ArrowUp, Enter, Escape)
openrouterModel.addEventListener('keydown', (e) => {
  const items = openrouterModelsDropdown.querySelectorAll('.autocomplete-item');
  if (!items || items.length === 0 || openrouterModelsDropdown.style.display !== 'block') return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    highlightedModelIndex = (highlightedModelIndex + 1) % items.length;
    updateItemHighlight(items);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    highlightedModelIndex = (highlightedModelIndex - 1 + items.length) % items.length;
    updateItemHighlight(items);
  } else if (e.key === 'Enter') {
    if (highlightedModelIndex >= 0 && highlightedModelIndex < items.length) {
      e.preventDefault();
      const targetId = items[highlightedModelIndex].getAttribute('data-model-id');
      if (targetId) selectModelItem(targetId);
    } else {
      hideModelsDropdown();
    }
  } else if (e.key === 'Escape') {
    hideModelsDropdown();
  }
});

function updateItemHighlight(items) {
  items.forEach((it, idx) => {
    if (idx === highlightedModelIndex) {
      it.classList.add('selected');
      it.scrollIntoView({ block: 'nearest' });
    } else {
      it.classList.remove('selected');
    }
  });
}

async function loadOpenRouterModels() {
  const svgIcon = refreshModelsBtn?.querySelector('svg');
  try {
    if (refreshModelsBtn) refreshModelsBtn.disabled = true;
    if (window.gsap && svgIcon) {
      gsap.to(svgIcon, { rotation: "+=360", duration: 0.6, repeat: -1, ease: "linear" });
    }

    // Была мёртвая ссылка на никогда не определённый populateDatalist():
    // ReferenceError ловился ближайшим catch и молча глотался — вызов убран,
    // поведение не изменилось (функции не существовало).
    const res = await apiGet('/api/openrouter/models');
    const data = await res.json();
    if (data.models && data.models.length > 0) {
      allOpenRouterModels = data.models;
    }

    if (refreshModelsBtn) refreshModelsBtn.disabled = false;
    if (window.gsap && svgIcon) {
      gsap.killTweensOf(svgIcon);
      gsap.to(svgIcon, { rotation: 0, duration: 0.2, ease: "power2.out" });
    }
  } catch (e) {
    console.error('Error fetching OpenRouter models:', e);
    if (refreshModelsBtn) refreshModelsBtn.disabled = false;
    if (window.gsap && svgIcon) {
      gsap.killTweensOf(svgIcon);
      gsap.set(svgIcon, { rotation: 0 });
    }
  }
}

refreshModelsBtn.addEventListener('click', async () => {
  showToast('Загрузка актуального каталога моделей с OpenRouter...');
  await loadOpenRouterModels();
  renderModelsDropdown(openrouterModel.value);
  showToast('✅ Каталог моделей обновлен!');
});

export async function loadOpenRouterConfig() {
  try {
    const res = await apiGet('/api/openrouter');
    const data = await res.json();
    openrouterBaseUrl.value = data.base_url || 'https://openrouter.ai/api/v1';
    const currentModel = data.model || 'deepseek/deepseek-v4-flash';
    openrouterModel.value = currentModel;
    openrouterEnabled.checked = Boolean(data.is_enabled);
    // Сырой ключ больше не приходит с сервера: поле остаётся пустым,
    // сохранение не затирает ключ (маска "******" игнорируется на бэкенде).
    openrouterApiKey.value = '';
    if (data.has_key) {
      openrouterApiKey.placeholder = `Ключ: ${data.api_key_masked}`;
    }
    await loadOpenRouterModels();
  } catch (e) {
    console.error('Error loading OpenRouter config:', e);
  }
}

openrouterEnabled.addEventListener('change', async () => {
  try {
    const res = await apiFetch('/api/openrouter', {
      method: 'POST',
      body: {
        base_url: openrouterBaseUrl.value.trim() || 'https://openrouter.ai/api/v1',
        model: openrouterModel.value.trim() || 'deepseek/deepseek-v4-flash',
        api_key: openrouterApiKey.value.trim(),
        is_enabled: openrouterEnabled.checked
      }
    });
    if (res.ok) {
      showToast(`AI Обработка: ${openrouterEnabled.checked ? 'Включена' : 'Отключена'}`);
    }
  } catch (e) {
    showToast('Ошибка сохранения', true);
  }
});

saveOpenRouterBtn.addEventListener('click', async () => {
  saveOpenRouterBtn.disabled = true;
  saveOpenRouterBtn.textContent = 'Сохранение...';
  try {
    const res = await apiFetch('/api/openrouter', {
      method: 'POST',
      body: {
        base_url: openrouterBaseUrl.value.trim() || 'https://openrouter.ai/api/v1',
        model: openrouterModel.value.trim() || 'deepseek/deepseek-v4-flash',
        api_key: openrouterApiKey.value.trim(),
        is_enabled: openrouterEnabled.checked
      }
    });
    saveOpenRouterBtn.disabled = false;
    saveOpenRouterBtn.textContent = 'Сохранить настройки OpenRouter';

    if (res.ok) {
      showToast('Настройки OpenRouter сохранены в SQLite');
      loadOpenRouterConfig();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Ошибка сохранения', true);
    }
  } catch (e) {
    saveOpenRouterBtn.disabled = false;
    saveOpenRouterBtn.textContent = 'Сохранить настройки OpenRouter';
    showToast('Ошибка: ' + e.message, true);
  }
});

testOpenRouterBtn.addEventListener('click', async () => {
  testOpenRouterBtn.disabled = true;
  testOpenRouterBtn.textContent = 'Тестирование LLM...';
  openrouterTestResultBox.style.display = 'none';

  try {
    const res = await apiFetch('/api/openrouter/test', {
      method: 'POST',
      body: {
        sample_text: "Требуется Senior AI/Python разработчик для создания Telegram-мониторов и Webhook-интеграций. Зарплата: $5000/мес."
      }
    });
    const data = await res.json();
    testOpenRouterBtn.disabled = false;
    testOpenRouterBtn.textContent = 'Тест OpenRouter';

    if (res.ok && data.status === 'success') {
      showToast(`Тест успешен (${data.model})!`);
      openrouterTestResultText.textContent = data.response;
      openrouterTestResultBox.style.display = 'block';
    } else {
      showToast(data.detail || 'Ошибка тестирования OpenRouter', true);
    }
  } catch (e) {
    testOpenRouterBtn.disabled = false;
    testOpenRouterBtn.textContent = 'Тест OpenRouter';
    showToast('Ошибка сети: ' + e.message, true);
  }
});

// ==================== TELEGRAM BOT FORWARD ====================

toggleTgBotTokenVisibility.addEventListener('click', () => {
  tgBotToken.type = tgBotToken.type === 'password' ? 'text' : 'password';
  toggleTgBotTokenVisibility.textContent = tgBotToken.type === 'password' ? '👁️' : '🙈';
});

export async function loadTgForwardConfig() {
  try {
    const res = await apiGet('/api/telegram-forward');
    const data = await res.json();
    tgSenderId.value = data.sender_id || '';
    tgForwardEnabled.checked = Boolean(data.is_enabled);
    // Сырой токен больше не приходит с сервера: поле остаётся пустым,
    // сохранение не затирает токен (маска "******" игнорируется на бэкенде).
    tgBotToken.value = '';
    if (data.has_token) {
      tgBotToken.placeholder = `Токен: ${data.bot_token_masked}`;
    }
  } catch (e) {
    console.error('Error loading Telegram forward config:', e);
  }
}

tgForwardEnabled.addEventListener('change', async () => {
  try {
    const res = await apiFetch('/api/telegram-forward', {
      method: 'POST',
      body: {
        bot_token: tgBotToken.value.trim(),
        sender_id: tgSenderId.value.trim(),
        is_enabled: tgForwardEnabled.checked
      }
    });
    if (res.ok) {
      showToast(`Отправка в Telegram: ${tgForwardEnabled.checked ? 'Включена' : 'Отключена'}`);
    }
  } catch (e) {
    showToast('Ошибка сохранения', true);
  }
});

saveTgForwardBtn.addEventListener('click', async () => {
  saveTgForwardBtn.disabled = true;
  saveTgForwardBtn.textContent = 'Сохранение...';
  try {
    const res = await apiFetch('/api/telegram-forward', {
      method: 'POST',
      body: {
        bot_token: tgBotToken.value.trim(),
        sender_id: tgSenderId.value.trim(),
        is_enabled: tgForwardEnabled.checked
      }
    });
    saveTgForwardBtn.disabled = false;
    saveTgForwardBtn.textContent = 'Сохранить настройки бота';

    if (res.ok) {
      showToast('Настройки Telegram-бота сохранены в SQLite');
      loadTgForwardConfig();
    } else {
      const err = await res.json();
      showToast(err.detail || 'Ошибка сохранения', true);
    }
  } catch (e) {
    saveTgForwardBtn.disabled = false;
    saveTgForwardBtn.textContent = 'Сохранить настройки бота';
    showToast('Ошибка: ' + e.message, true);
  }
});

testTgForwardBtn.addEventListener('click', async () => {
  testTgForwardBtn.disabled = true;
  testTgForwardBtn.textContent = '⏳ Отправка...';

  try {
    const res = await apiFetch('/api/telegram-forward/test', { method: 'POST' });
    const data = await res.json();
    testTgForwardBtn.disabled = false;
    testTgForwardBtn.textContent = '⚡ Отправить тестовое сообщение';

    if (res.ok && data.status === 'success') {
      showToast('✅ Тестовое сообщение успешно доставлено в Telegram!');
    } else {
      showToast(data.detail || 'Ошибка отправки тестового сообщения', true);
    }
  } catch (e) {
    testTgForwardBtn.disabled = false;
    testTgForwardBtn.textContent = '⚡ Отправить тестовое сообщение';
    showToast('Ошибка: ' + e.message, true);
  }
});

// ==================== JSON EXAMPLE MODAL ====================

if (openJsonExampleModalBtn) {
  openJsonExampleModalBtn.addEventListener('click', () => {
    openModalAnimated(jsonExampleModal);
  });
}
if (closeJsonExampleModalBtn) {
  closeJsonExampleModalBtn.addEventListener('click', () => {
    closeModalAnimated(jsonExampleModal);
  });
}
if (closeJsonExampleBtn2) {
  closeJsonExampleBtn2.addEventListener('click', () => {
    closeModalAnimated(jsonExampleModal);
  });
}
if (copyJsonExampleBtn && jsonExampleCode) {
  copyJsonExampleBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(jsonExampleCode.innerText);
    showToast('JSON скопирован в буфер обмена!');
  });
}

// ==================== AUTO-CLEANUP ====================

export async function loadCleanupConfig() {
  try {
    const res = await apiGet('/api/cleanup');
    if (!res.ok) return;
    const data = await res.json();
    if (cleanupEnabledInput) cleanupEnabledInput.checked = Boolean(data.enabled);
    if (cleanupDaysSelect) cleanupDaysSelect.value = String(data.days || 30);

    if (cleanupStatusLabel) {
      if (data.enabled) {
        cleanupStatusLabel.textContent = `Вкл (${data.days} дн.)`;
        cleanupStatusLabel.style.color = '#008715';
      } else {
        cleanupStatusLabel.textContent = 'Выкл';
        cleanupStatusLabel.style.color = 'var(--body-mid)';
      }
    }

    if (cleanupLastRunDate) {
      if (data.last_run) {
        cleanupLastRunDate.textContent = new Date(data.last_run).toLocaleString([], {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
        });
      } else {
        cleanupLastRunDate.textContent = 'еще не выполнялась';
      }
    }
  } catch (e) {
    console.error('Error loading cleanup config:', e);
  }
}

if (openCleanupModalBtn) {
  openCleanupModalBtn.addEventListener('click', () => {
    loadCleanupConfig();
    openModalAnimated(cleanupModal);
  });
}

if (closeCleanupModalBtn) closeCleanupModalBtn.addEventListener('click', () => closeModalAnimated(cleanupModal));
if (closeCleanupModalBtn2) closeCleanupModalBtn2.addEventListener('click', () => closeModalAnimated(cleanupModal));

if (cleanupEnabledInput) {
  cleanupEnabledInput.addEventListener('change', async () => {
    const enabled = cleanupEnabledInput.checked;
    const days = parseInt(cleanupDaysSelect.value) || 30;
    try {
      const res = await apiFetch('/api/cleanup', {
        method: 'POST',
        body: { enabled: enabled, days: days }
      });
      if (res.ok) {
        showToast(`Автоочистка базы: ${enabled ? `Включена (${days} дн.)` : 'Выключена'}`);
        loadCleanupConfig();
      }
    } catch (e) {
      showToast('Ошибка сохранения настроек очистки', true);
    }
  });
}

if (saveCleanupConfigBtn) {
  saveCleanupConfigBtn.addEventListener('click', async () => {
    const enabled = cleanupEnabledInput.checked;
    const days = parseInt(cleanupDaysSelect.value) || 30;
    saveCleanupConfigBtn.disabled = true;
    saveCleanupConfigBtn.textContent = 'Сохранение...';
    try {
      const res = await apiFetch('/api/cleanup', {
        method: 'POST',
        body: { enabled: enabled, days: days }
      });
      saveCleanupConfigBtn.disabled = false;
      saveCleanupConfigBtn.textContent = 'Сохранить';
      if (res.ok) {
        showToast('Настройки автоочистки базы сохранены!');
        closeModalAnimated(cleanupModal);
        loadCleanupConfig();
      } else {
        showToast('Ошибка сохранения настроек', true);
      }
    } catch (e) {
      saveCleanupConfigBtn.disabled = false;
      saveCleanupConfigBtn.textContent = 'Сохранить';
      showToast('Ошибка: ' + e.message, true);
    }
  });
}

if (runCleanupNowBtn) {
  runCleanupNowBtn.addEventListener('click', async () => {
    runCleanupNowBtn.disabled = true;
    runCleanupNowBtn.textContent = 'Очистка...';
    try {
      const res = await apiFetch('/api/cleanup/run-now', { method: 'POST' });
      const data = await res.json();
      runCleanupNowBtn.disabled = false;
      runCleanupNowBtn.textContent = '⚡ Очистить сейчас';
      if (res.ok) {
        showToast(`✅ Удалено ${data.deleted_logs || 0} логов и ${data.deleted_messages || 0} записей сообщений`);
        loadLogs();
        loadCleanupConfig();
      } else {
        showToast('Ошибка выполнения очистки', true);
      }
    } catch (e) {
      runCleanupNowBtn.disabled = false;
      runCleanupNowBtn.textContent = '⚡ Очистить сейчас';
      showToast('Ошибка сети: ' + e.message, true);
    }
  });
}