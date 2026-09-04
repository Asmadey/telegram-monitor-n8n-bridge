// auth-pages.js — общий крошечный скрипт экранов входа (задача 5.3).
// Самодостаточен: никаких import-ов (не тянет граф SPA) и никакого
// построения HTML — ошибки показываются через textContent (5.2).

(function () {
  'use strict';

  // CSRF (2.6): cookie выдаёт сервер на GET страницы; на каждом не-GET
  // её значение уходит в заголовок.
  function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // База API: пусто = тот же origin (Vercel переписывает /auth/* на Railway).
  // Прямые межсайтовые вызовы включаются заданием window.__TELETON_API__.
  function apiBase() {
    return (window.__TELETON_API__ || '').replace(/\/$/, '');
  }

  async function postJson(url, payload) {
    const res = await fetch(apiBase() + url, {
      method: 'POST',
      // include, а не same-origin: при отдельном домене фронтенда cookie
      // сессии к межсайтовому запросу не приложится, и вход «не сработает»
      // без единой ошибки в консоли
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken()
      },
      body: JSON.stringify(payload)
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      // тело не JSON — останется пустым, сообщение даст fallback
    }
    return { ok: res.ok, data: data || {} };
  }

  // detail FastAPI — строка ИЛИ массив ошибок валидации (422)
  function detailText(data, fallback) {
    const d = data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map(function (item) { return (item && item.msg) || ''; })
        .filter(Boolean).join('; ');
    }
    return fallback;
  }

  function showError(form, message) {
    const el = form.querySelector('.form-error');
    if (el) el.textContent = message; // textContent, не innerHTML (5.2)
  }

  // --- вход ---
  const loginForm = document.getElementById('loginForm');
  if (loginForm) {
    loginForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      showError(loginForm, '');
      const r = await postJson('/auth/login', {
        email: document.getElementById('email').value,
        password: document.getElementById('password').value
      });
      if (r.ok) {
        window.location.href = '/';
        return;
      }
      showError(loginForm, detailText(r.data, 'Ошибка входа'));
    });
  }

  // --- регистрация ---
  const signupForm = document.getElementById('signupForm');
  if (signupForm) {
    signupForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      showError(signupForm, '');
      const tz = document.getElementById('timezone');
      const r = await postJson('/auth/signup', {
        email: document.getElementById('email').value,
        password: document.getElementById('password').value,
        timezone: tz ? tz.value : 'UTC'
      });
      if (r.ok) {
        window.location.href = '/';
        return;
      }
      showError(signupForm, detailText(r.data, 'Ошибка регистрации'));
    });
  }

  // --- сброс: запрос письма ---
  const requestForm = document.getElementById('requestResetForm');
  if (requestForm) {
    requestForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      showError(requestForm, '');
      const r = await postJson('/auth/password-reset', {
        email: document.getElementById('email').value
      });
      // Ответ одинаков для существующего и несуществующего адреса (2.5) —
      // сообщение безличное, показывает его всегда.
      const msg = requestForm.querySelector('.form-success');
      if (msg) msg.textContent = r.data.detail || 'Если адрес зарегистрирован, письмо отправлено';
    });
  }

  // --- сброс: новый пароль по токену из письма ---
  const confirmForm = document.getElementById('confirmResetForm');
  if (confirmForm) {
    const tokenInput = document.getElementById('token');
    if (tokenInput && !tokenInput.value) {
      // ссылка из письма: /password-reset?token=...
      tokenInput.value = new URLSearchParams(window.location.search).get('token') || '';
    }
    confirmForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      showError(confirmForm, '');
      const r = await postJson('/auth/password-reset/confirm', {
        token: document.getElementById('token').value,
        new_password: document.getElementById('newPassword').value
      });
      if (r.ok) {
        window.location.href = '/login';
        return;
      }
      showError(confirmForm, detailText(r.data, 'Ссылка устарела или неверна'));
    });
  }
})();