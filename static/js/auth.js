// auth.js — статус Telegram-аккаунта, визард входа (телефон → код →
// 2FA), настройки MTProto (задача 5.1, разрез index.html).
//
// Состояние визарда (currentPhone/phone_code_hash) — локальное для
// модуля: другим вкладкам оно не нужно.

import { apiFetch, apiGet } from './api.js';
import { showToast } from './render.js';

const statusBadge = document.getElementById('statusBadge');
const statusUser = document.getElementById('statusUser');

const settingsModal = document.getElementById('settingsModal');
const openSettingsModalBtn = document.getElementById('openSettingsModalBtn');
const closeSettingsModal = document.getElementById('closeSettingsModal');
const settingsApiId = document.getElementById('settingsApiId');
const settingsApiHash = document.getElementById('settingsApiHash');

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

let currentPhone = '';
let currentPhoneCodeHash = '';

export async function checkHealth() {
  try {
    const res = await apiGet('/health');
    const data = await res.json();
    if (data.authorized && data.user) {
      statusUser.textContent = `${data.user.first_name} (@${data.user.username || 'id' + data.user.id})`;
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

export async function loadSettings() {
  try {
    const res = await apiGet('/api/settings');
    const data = await res.json();
    settingsApiId.value = data.api_id || '';
    // Задача 1.2 (PLAN.md): сырой hash не приходит и не сохраняется из UI,
    // показываем только маску (или предупреждение, если ключа нет).
    settingsApiHash.value = data.has_api_hash
      ? `API HASH: ${data.api_hash_masked}`
      : 'не задан (TELEGRAM_API_HASH)';

    if (data.is_authorized && data.user) {
      authStatusBox.style.display = 'block';
      authWizardBox.style.display = 'none';
      authUserDetails.textContent = `${data.user.first_name} ${data.user.last_name || ''} (@${data.user.username || 'нет юзернейма'}) • ID: ${data.user.id}`;
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

// Задача 1.2 (PLAN.md): кнопка «Сохранить в .env» и POST /api/settings
// удалены — ключи задаются только переменными окружения.

sendCodeBtn.addEventListener('click', async () => {
  const phone = authPhone.value.trim();
  if (!phone) {
    showToast('Введите номер телефона', true);
    return;
  }
  currentPhone = phone;
  sendCodeBtn.disabled = true;
  sendCodeBtn.textContent = 'Отправка...';

  try {
    const res = await apiFetch('/api/auth/send-code', {
      method: 'POST',
      body: { phone: phone }
    });
    const data = await res.json();
    sendCodeBtn.disabled = false;
    sendCodeBtn.textContent = 'Получить код в Telegram';

    if (res.ok && data.status === 'code_sent') {
      currentPhoneCodeHash = data.phone_code_hash;
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
    const res = await apiFetch('/api/auth/sign-in', {
      method: 'POST',
      body: {
        phone: currentPhone,
        code: code,
        phone_code_hash: currentPhoneCodeHash
      }
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
    const res = await apiFetch('/api/auth/sign-in', {
      method: 'POST',
      body: {
        phone: currentPhone,
        code: authCode.value.trim(),
        phone_code_hash: currentPhoneCodeHash,
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
    const res = await apiFetch('/api/auth/logout', { method: 'POST' });
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