// main.js — входная точка фронтенда: переключение вкладок (вкладка в URL),
// стартовая инициализация, фоновые интервалы (задача 5.1, разрез index.html).
//
// Единственный модуль, знающий все вкладки: переключение тянет loaders
// из модулей вкладок, сами модули друг про друга не знают (кроме
// ребра channels → messages и integration → logs).

import { checkHealth } from './auth.js';
import { loadFeed } from './feed.js';
import { loadConfig, refreshMonitorTimers } from './channels.js';
import { loadSavedMessages } from './messages.js';
import { loadOpenRouterConfig, loadTgForwardConfig, loadCleanupConfig } from './integration.js';
import { loadLogs } from './logs.js';

const VALID_TABS = ['feed', 'messages', 'channels', 'integration', 'logs'];

function switchTab(tabId, updateUrl = true) {
  if (!VALID_TABS.includes(tabId)) {
    tabId = 'feed';
  }

  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));

  const selectedBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.getAttribute('onclick')?.includes(tabId));
  if (selectedBtn) selectedBtn.classList.add('active');

  const targetPane = document.getElementById(`tab-${tabId}`);
  if (targetPane) {
    targetPane.classList.add('active');
    if (window.gsap) {
      gsap.fromTo(targetPane,
        { autoAlpha: 0, y: 8 },
        { autoAlpha: 1, y: 0, duration: 0.22, ease: "power2.out" }
      );
    }
  }

  if (updateUrl) {
    const targetPath = tabId === 'feed' ? '/' : `/${tabId}`;
    if (window.location.pathname !== targetPath) {
      history.pushState({ tab: tabId }, '', targetPath);
    }
  }

  if (tabId === 'feed') {
    loadFeed();
  } else if (tabId === 'messages') {
    loadSavedMessages();
  } else if (tabId === 'logs') {
    loadLogs();
  } else if (tabId === 'channels') {
    loadConfig();
  } else if (tabId === 'integration') {
    loadConfig();
    loadOpenRouterConfig();
    loadTgForwardConfig();
  }
}

window.switchTab = switchTab;

// Слушатель кнопок браузера «Назад» и «Вперед»
window.addEventListener('popstate', () => {
  initTabFromUrl();
});

function initTabFromUrl() {
  const cleanPath = window.location.pathname.replace(/^\/+|\/+$/g, '').toLowerCase();
  if (cleanPath === 'messages') {
    switchTab('messages', false);
  } else if (cleanPath === 'channels') {
    switchTab('channels', false);
  } else if (cleanPath === 'integration') {
    switchTab('integration', false);
  } else if (cleanPath === 'logs') {
    switchTab('logs', false);
  } else {
    switchTab('feed', false);
  }
}

checkHealth();
loadConfig();
loadFeed();
loadLogs();
loadCleanupConfig();
loadSavedMessages();
loadOpenRouterConfig();
loadTgForwardConfig();
initTabFromUrl();

// Фоновое авто-обновление ленты (каждые 8 секунд)
setInterval(() => {
  loadFeed(true);
}, 8000);

// Периодический пересчет таймеров обратного отсчета (каждые 15 секунд)
setInterval(() => {
  refreshMonitorTimers();
}, 15000);