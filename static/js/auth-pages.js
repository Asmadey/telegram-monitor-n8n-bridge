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
  // --- вход через Google (Фаза 6) ---
  //
  // Firebase здесь — ПРОВАЙДЕР ИДЕНТИЧНОСТИ, а не система сессий: SDK лишь
  // добывает ID-токен, дальше его проверяет бэкенд и выдаёт СВОЮ cookie.
  // Поэтому отзыв доступа, блокировка пользователя и админка продолжают
  // работать, а GitHub или Apple добавятся без переписывания входа.
  //
  // Версия SDK закреплена точно: плавающая ссылка на чужой скрипт означает
  // код, который может смениться между двумя загрузками страницы входа.
  // При обновлении менять обе строки разом — модули одной версии.
  const FIREBASE_APP = 'https://www.gstatic.com/firebasejs/10.13.2/firebase-app.js';
  const FIREBASE_AUTH = 'https://www.gstatic.com/firebasejs/10.13.2/firebase-auth.js';

  const googleButton = document.getElementById('googleSignIn');
  if (googleButton) {
    const errorBox = document.querySelector('.google-error');
    const setError = function (text) {
      if (errorBox) errorBox.textContent = text; // textContent, не innerHTML (5.2)
    };

    // Кнопка скрыта в разметке и показывается ТОЛЬКО если Firebase
    // настроен: кнопка, ведущая в ошибку инициализации, хуже отсутствующей.
    fetch(apiBase() + '/auth/google/config', { credentials: 'include' })
      .then(function (res) { return res.json(); })
      .then(function (cfg) {
        if (!cfg || !cfg.enabled) return;
        googleButton.hidden = false;
        googleButton.addEventListener('click', function () {
          signInWithGoogle(cfg, setError, googleButton);
        });
      })
      .catch(function () { /* API недоступен — вход по паролю остаётся */ });
  }

  async function signInWithGoogle(cfg, setError, button) {
    setError('');
    button.disabled = true;
    try {
      // динамический import: SDK грузится только по нажатию, а не на
      // каждой загрузке страницы входа
      const appMod = await import(FIREBASE_APP);
      const authMod = await import(FIREBASE_AUTH);
      const app = appMod.initializeApp({
        apiKey: cfg.apiKey,
        authDomain: cfg.authDomain,
        projectId: cfg.projectId
      });
      const auth = authMod.getAuth(app);
      const result = await authMod.signInWithPopup(
        auth, new authMod.GoogleAuthProvider()
      );
      const idToken = await result.user.getIdToken();

      const r = await postJson('/auth/google', { id_token: idToken });
      if (r.ok) {
        window.location.href = '/';
        return;
      }
      setError(detailText(r.data, 'Не удалось войти через Google'));
    } catch (e) {
      // закрытое пользователем окно — не ошибка, о которой стоит кричать
      const code = (e && e.code) || '';
      if (code !== 'auth/popup-closed-by-user' && code !== 'auth/cancelled-popup-request') {
        setError('Не удалось войти через Google');
      }
    } finally {
      button.disabled = false;
    }
  }
})();