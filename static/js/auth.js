// auth.js — статус Telegram-аккаунта, визард входа (телефон → код →
// 2FA), настройки MTProto (задача 5.1, разрез index.html).
//
// Состояние попытки авторизации хранится в БД на сервере. Клиент хранит
// только номер для подписи текущего шага формы.

import { apiFetch, apiGet } from './api.js';
import { showToast } from './render.js';

const statusBadge = document.getElementById('statusBadge');
const accountEmail = document.getElementById('accountEmail');
const signOutBtn = document.getElementById('signOutBtn');
const statusUser = document.getElementById('statusUser');

const settingsModal = document.getElementById('settingsModal');
const openSettingsModalBtn = document.getElementById('openSettingsModalBtn');
const closeSettingsModal = document.getElementById('closeSettingsModal');
const settingsApiId = document.getElementById('settingsApiId');
const settingsApiHash = document.getElementById('settingsApiHash');
const saveApiKeysBtn = document.getElementById('saveApiKeysBtn');
const apiKeysHint = document.getElementById('apiKeysHint');

const authStatusBox = document.getElementById('authStatusBox');
const authUserDetails = document.getElementById('authUserDetails');
const authWizardBox = document.getElementById('authWizardBox');
const stepPhone = document.getElementById('stepPhone');
const stepCode = document.getElementById('stepCode');
const step2fa = document.getElementById('step2fa');
const authPhone = document.getElementById('authPhone');
const authCode = document.getElementById('authCode');
const auth2faPassword = document.getElementById('auth2faPassword');
const sendCodeBtn = document.getElementById('sendCodeBtn');
const submitCodeBtn = document.getElementById('submitCodeBtn');
const submit2faBtn = document.getElementById('submit2faBtn');
const backToPhoneBtn = document.getElementById('backToPhoneBtn');
const logoutBtn = document.getElementById('logoutBtn');
const phoneDisplay = document.getElementById('phoneDisplay');

export async function checkHealth() {
  try {
    const res = await apiGet('/api/telegram/status');
    if (!res.ok) throw new Error('Telegram status unavailable');
    const data = await res.json();
    if (data.is_authorized && data.user) {
      statusUser.textContent = data.user.username
        ? `@${data.user.username}`
        : `ID ${data.user.id}`;
      statusBadge.classList.remove('offline');
    } else {
      statusUser.textContent = 'Требуется авторизация';
      statusBadge.classList.add('offline');
    }
  } catch (e) {
    statusUser.textContent = 'Offline';
    statusBadge.classList.add('offline');
  }
}

export function showAccount(email) {
  // Почта видна всегда, пока открыт кабинет: понять, под кем ты сидишь,
  // должно быть можно не открывая ни одной модалки.
  accountEmail.textContent = email;
  accountEmail.hidden = false;
  signOutBtn.hidden = false;
}

// Выход из КАБИНЕТА (сессия сервиса), а не из Telegram-аккаунта.
signOutBtn.addEventListener('click', async () => {
  signOutBtn.disabled = true;
  try {
    await apiFetch('/auth/logout', { method: 'POST' });
  } catch (e) {
    // Сеть подвела — всё равно уводим на вход: остаться в оболочке,
    // думая, что вышел, хуже честного повторного входа.
  }
  window.location.replace('/login');
});

export async function loadSettings() {
  try {
    const res = await apiGet('/api/telegram/status');
    if (!res.ok) throw new Error('Telegram status unavailable');
    const data = await res.json();
    settingsApiId.value = data.api_id || '';
    // Сырой hash в браузер не возвращается никогда: показываем только
    // признак наличия, поле остаётся пустым (правило 0.3).
    settingsApiHash.value = '';
    settingsApiHash.placeholder = data.has_api_hash ? '•••••••• (сохранён)' : 'ваш api_hash';
    apiKeysHint.textContent = data.has_api_hash ? '' : 'Ключи ещё не заданы';

    if (data.is_authorized && data.user) {
      authStatusBox.style.display = 'block';
      authWizardBox.style.display = 'none';
      const username = data.user.username ? `@${data.user.username}` : 'нет юзернейма';
      authUserDetails.textContent = `${data.user.phone} (${username}) • ID: ${data.user.id}`;
    } else {
      authStatusBox.style.display = 'none';
      authWizardBox.style.display = 'block';
      setAuthStep(1);
    }
  } catch (e) {
    showToast('Ошибка загрузки настроек', true);
  }
}

function setAuthStep(step) {
  stepPhone.classList.toggle('active', step === 1);
  stepCode.classList.toggle('active', step === 2);
  step2fa.classList.toggle('active', step === 3);
}

// Ключи приложения принадлежат пользователю: сохраняются в его кабинет,
// api_hash шифруется на сервере и обратно не приходит.
saveApiKeysBtn.addEventListener('click', async () => {
  const apiId = Number.parseInt(settingsApiId.value.trim(), 10);
  if (!Number.isInteger(apiId) || apiId <= 0) {
    showToast('API ID — целое число с my.telegram.org', true);
    return;
  }
  const apiHash = settingsApiHash.value.trim();
  saveApiKeysBtn.disabled = true;
  try {
    // Пустой api_hash не передаётся вовсе: так сохранение одного лишь
    // API ID не затирает уже сохранённый секрет.
    const body = apiHash ? { api_id: apiId, api_hash: apiHash } : { api_id: apiId };
    const res = await apiFetch('/api/telegram/credentials', { method: 'POST', body });
    if (!res.ok) {
      const problem = await res.json().catch(() => ({}));
      throw new Error(problem.detail || 'Не удалось сохранить ключи');
    }
    showToast('Ключи сохранены');
    await loadSettings();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    saveApiKeysBtn.disabled = false;
  }
});

sendCodeBtn.addEventListener('click', async () => {
  const phone = authPhone.value.trim();
  if (!phone) {
    showToast('Введите номер телефона', true);
    return;
  }
  sendCodeBtn.disabled = true;
  sendCodeBtn.textContent = 'Отправка...';

  try {
    const res = await apiFetch('/api/telegram/send-code', {
      method: 'POST',
      body: { phone: phone }
    });
    const data = await res.json();
    sendCodeBtn.disabled = false;
    sendCodeBtn.textContent = 'Получить код в Telegram';

    if (res.ok && data.status === 'code_sent') {
      phoneDisplay.textContent = phone;
      setAuthStep(2);
      showToast(data.message);
    } else {
      showToast(data.detail || 'Ошибка отправки кода', true);
    }
  } catch (e) {
    sendCodeBtn.disabled = false;
    sendCodeBtn.textContent = 'Получить код в Telegram';
    showToast('Ошибка отправки кода', true);
  }
});

submitCodeBtn.addEventListener('click', async () => {
  const code = authCode.value.trim();
  if (!code) {
    showToast('Введите код', true);
    return;
  }
  submitCodeBtn.disabled = true;
  submitCodeBtn.textContent = 'Проверка...';

  try {
    const res = await apiFetch('/api/telegram/sign-in', {
      method: 'POST',
      body: { code: code }
    });
    const data = await res.json();
    submitCodeBtn.disabled = false;
    submitCodeBtn.textContent = 'Подтвердить вход';

    if (data.status === '2fa_required') {
      setAuthStep(3);
      showToast(data.message);
    } else if (data.status === 'authorized') {
      showToast(data.message);
      checkHealth();
      loadSettings();
    } else {
      showToast(data.detail || 'Неверный код', true);
    }
  } catch (e) {
    submitCodeBtn.disabled = false;
    submitCodeBtn.textContent = 'Подтвердить вход';
    showToast('Ошибка проверки кода', true);
  }
});

submit2faBtn.addEventListener('click', async () => {
  const pwd = auth2faPassword.value.trim();
  if (!pwd) {
    showToast('Введите пароль 2FA', true);
    return;
  }
  submit2faBtn.disabled = true;
  submit2faBtn.textContent = 'Проверка 2FA...';

  try {
    const res = await apiFetch('/api/telegram/sign-in', {
      method: 'POST',
      body: {
        code: authCode.value.trim(),
        password: pwd
      }
    });
    const data = await res.json();
    submit2faBtn.disabled = false;
    submit2faBtn.textContent = 'Войти с 2FA паролем';

    if (data.status === 'authorized') {
      showToast(data.message);
      checkHealth();
      loadSettings();
    } else {
      showToast(data.detail || 'Неверный пароль 2FA', true);
    }
  } catch (e) {
    submit2faBtn.disabled = false;
    submit2faBtn.textContent = 'Войти с 2FA паролем';
    showToast('Ошибка проверки 2FA пароля', true);
  }
});

backToPhoneBtn.addEventListener('click', () => setAuthStep(1));

logoutBtn.addEventListener('click', async () => {
  if (!confirm('Выйти из Telegram-аккаунта и сбросить сессию?')) return;
  try {
    const res = await apiFetch('/api/telegram/logout', { method: 'POST' });
    if (res.ok) {
      showToast('Сессия сброшена');
      checkHealth();
      loadSettings();
    }
  } catch (e) {
    showToast('Ошибка при выходе', true);
  }
});

openSettingsModalBtn.addEventListener('click', () => {
  settingsModal.classList.add('active');
  loadSettings();
});
closeSettingsModal.addEventListener('click', () => settingsModal.classList.remove('active'));
