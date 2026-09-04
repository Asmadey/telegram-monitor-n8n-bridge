// feed.js — вкладка «Лента»: AI-сводки и задачи анализа
// (задача 5.1, разрез index.html).
//
// selectFeedItem вызывается из inline-onclick шаблона карточки —
// обязана быть на window (ES-модули file-scoped).

import { apiFetch, apiGet } from './api.js';
import { html, raw, formatRelativeTime, formatTelegramText, showToast } from './render.js';

const tabFeedCount = document.getElementById('tabFeedCount');
const feedTotalCount = document.getElementById('feedTotalCount');
const feedListContainer = document.getElementById('feedListContainer');
const feedDetailPlaceholder = document.getElementById('feedDetailPlaceholder');
const feedDetailContent = document.getElementById('feedDetailContent');
const feedDetailAvatar = document.getElementById('feedDetailAvatar');
const feedDetailTitle = document.getElementById('feedDetailTitle');
const feedDetailTime = document.getElementById('feedDetailTime');
const feedDetailRelativeTime = document.getElementById('feedDetailRelativeTime');
const feedDetailCount = document.getElementById('feedDetailCount');
const feedDetailSummary = document.getElementById('feedDetailSummary');
const feedRawCount = document.getElementById('feedRawCount');
const feedRawMessagesList = document.getElementById('feedRawMessagesList');
const feedDetailTgLink = document.getElementById('feedDetailTgLink');
const copyFeedSummaryBtn = document.getElementById('copyFeedSummaryBtn');
const reanalyzeFeedItemBtn = document.getElementById('reanalyzeFeedItemBtn');
const deleteFeedItemBtn = document.getElementById('deleteFeedItemBtn');
const refreshFeedBtn = document.getElementById('refreshFeedBtn');

let currentFeed = [];
let selectedFeedId = null;

export async function loadFeed(silent = false) {
  try {
    const res = await apiGet('/api/feed?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    const prevCount = currentFeed.length;
    currentFeed = data.feed || [];

    if (tabFeedCount) tabFeedCount.textContent = currentFeed.length;
    if (feedTotalCount) feedTotalCount.textContent = currentFeed.length;

    renderFeedList();

    if (currentFeed.length > 0) {
      if (!selectedFeedId || !currentFeed.find(f => f.id === selectedFeedId)) {
        selectFeedItem(currentFeed[0].id);
      }
    } else {
      selectedFeedId = null;
      if (feedDetailPlaceholder) feedDetailPlaceholder.style.display = 'flex';
      if (feedDetailContent) feedDetailContent.style.display = 'none';
    }

    if (!silent && prevCount > 0 && currentFeed.length > prevCount) {
      showToast(`Получено новых отчетов: ${currentFeed.length - prevCount}`);
    }
  } catch (e) {
    console.error('Error loading feed:', e);
  }
}

function renderFeedList() {
  if (!feedListContainer) return;
  if (currentFeed.length === 0) {
    feedListContainer.innerHTML = html`
      <div style="text-align: center; padding: 48px 16px; color: var(--mute); font-size: 13px;">
        Пока нет выполненных задач анализа.<br>
        <span style="font-size: 11.5px; color: var(--body-mid); display: inline-block; margin-top: 6px;">
          Запустите опрос канала на вкладке «Каналы».
        </span>
      </div>
    `;
    return;
  }

  feedListContainer.innerHTML = currentFeed.map(item => {
    const isActive = item.id === selectedFeedId;
    const initial = (item.chat_title || 'Т').charAt(0).toUpperCase();
    // Аватарка — с отдельного эндпоинта с кешом браузера (задача 5.4),
    // не из строки ленты. Ветка photo_base64 — совместимость с монолитом
    // (server.py, рантайм до закрытия К2): он ещё отдаёт аватарку строкой.
    const avatarHtml = item.photo_base64
      ? html`<img src="${item.photo_base64}" class="feed-avatar" alt="${item.chat_title}">`
      : item.chat_id
        ? html`<img src="/api/avatars/${item.chat_id}" class="feed-avatar" alt="${item.chat_title}" data-initial="${initial}">`
        : html`<div class="feed-avatar">${initial}</div>`;

    const rawMsgs = Array.isArray(item.messages) ? item.messages : [];
    const snippet = item.ai_analysis
      ? item.ai_analysis.replace(/[*#`_]/g, '')
      : (rawMsgs[0]?.text || 'Выборка сообщений Telegram');

    return html`
      <div class="feed-card ${isActive ? 'active' : ''}" onclick="selectFeedItem(${item.id})">
        <div class="feed-card-header">
          ${raw(avatarHtml)}
          <div style="min-width: 0; flex: 1;">
            <div class="feed-card-title">${item.chat_title || 'Канал'}</div>
            <div class="feed-card-meta">
              <span>${formatRelativeTime(item.created_at)}</span>
              <span style="opacity: 0.5;">•</span>
              <span style="color: #008715; font-weight: 500;">${item.messages_count} постов</span>
            </div>
          </div>
        </div>
        <div class="feed-card-snippet">${snippet}</div>
      </div>
    `;
  }).join('');

  // Аватарки нет в chat_avatars (канал без фото / воркер не ходил) —
  // 404 превращаем в букву-заглушку, а не в битый img.
  feedListContainer.querySelectorAll('img.feed-avatar').forEach(img => {
    img.addEventListener('error', () => {
      const div = document.createElement('div');
      div.className = 'feed-avatar';
      div.textContent = img.dataset.initial || 'Т';
      img.replaceWith(div);
    }, { once: true });
  });
}

async function selectFeedItem(id) {
  selectedFeedId = id;
  let item = currentFeed.find(f => f.id === id);
  if (!item) return;

  // Новая сборка (5.4): список несёт только метаданные, исходные посты —
  // детальным видом. В монолите (server.py, рантайм до К2) посты приходят
  // списком — лишний запрос не дёргаем.
  if (!Array.isArray(item.messages)) {
    try {
      const res = await apiGet(`/api/feed/${id}`);
      if (res.ok) {
        const data = await res.json();
        if (data.feed_item && Array.isArray(data.feed_item.messages)) {
          item = { ...item, ...data.feed_item };
        }
      }
    } catch (e) {
      console.error('Error loading feed detail:', e);
    }
  }

  document.querySelectorAll('.feed-card').forEach(el => el.classList.remove('active'));
  const cards = document.querySelectorAll('.feed-card');
  const activeCard = Array.from(cards).find(el => el.getAttribute('onclick')?.includes(String(id)));
  if (activeCard) activeCard.classList.add('active');

  if (feedDetailPlaceholder) feedDetailPlaceholder.style.display = 'none';
  if (feedDetailContent) {
    feedDetailContent.style.display = 'block';
    if (window.gsap) {
      gsap.fromTo(feedDetailContent, { autoAlpha: 0, y: 6 }, { autoAlpha: 1, y: 0, duration: 0.2, ease: "power2.out" });
    }
  }

  const initial = (item.chat_title || 'Т').charAt(0).toUpperCase();
  if (feedDetailAvatar) {
    if (item.photo_base64) {
      feedDetailAvatar.innerHTML = html`<img src="${item.photo_base64}" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover;">`;
    } else if (item.chat_id) {
      // аватарка с эндпоинта (5.4); 404 → буква-заглушка
      feedDetailAvatar.innerHTML = html`<img src="/api/avatars/${item.chat_id}" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover;" alt="">`;
      const img = feedDetailAvatar.querySelector('img');
      if (img) {
        img.addEventListener('error', () => {
          feedDetailAvatar.textContent = initial;
        }, { once: true });
      }
    } else {
      feedDetailAvatar.textContent = initial;
    }
  }

  if (feedDetailTitle) feedDetailTitle.textContent = item.chat_title || 'Канал';
  if (feedDetailTime) feedDetailTime.textContent = new Date(item.created_at).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  });
  if (feedDetailRelativeTime) feedDetailRelativeTime.textContent = formatRelativeTime(item.created_at);
  if (feedDetailCount) feedDetailCount.textContent = `${item.messages_count} постов`;

  if (feedDetailSummary) {
    const text = item.ai_analysis || 'AI-анализ не был сгенерирован для этой выборки (OpenRouter был выключен).';
    feedDetailSummary.innerHTML = formatTelegramText(text);
  }

  if (feedDetailTgLink) {
    if (item.chat_username) {
      feedDetailTgLink.href = `https://t.me/${item.chat_username}`;
      feedDetailTgLink.style.display = 'inline-flex';
    } else {
      feedDetailTgLink.style.display = 'none';
    }
  }

  // Рендерим исходные посты Telegram
  const msgs = Array.isArray(item.messages) ? item.messages : [];
  if (feedRawCount) feedRawCount.textContent = msgs.length;
  if (feedRawMessagesList) {
    if (msgs.length === 0) {
      feedRawMessagesList.innerHTML = '<div style="color: var(--mute); font-size: 12.5px;">Нет исходных постов в этой выборке.</div>';
    } else {
      feedRawMessagesList.innerHTML = msgs.map((m, idx) => html`
        <div class="feed-raw-post">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <div style="font-size: 12px; font-weight: 600; color: var(--ink);">
              Пост #${m.id || idx + 1}
            </div>
            <div style="font-size: 12px; color: var(--body-mid); font-variant-numeric: tabular-nums;">
              ${m.date ? new Date(m.date).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
            </div>
          </div>
          <div style="font-size: 13px; line-height: 1.5; color: var(--body); margin-bottom: 8px;">
            ${raw(formatTelegramText(m.text || ''))}
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            ${m.views !== null && m.views !== undefined ? raw(`<span class="badge-metric">👁️ ${Number(m.views).toLocaleString('ru-RU')}</span>`) : ''}
            ${m.reactions_count ? raw(`<span class="badge-metric" style="color: #ed52cb;">❤️ ${m.reactions_count}</span>`) : ''}
            ${m.forwards ? raw(`<span class="badge-metric">↗️ ${m.forwards}</span>`) : ''}
            ${m.has_media ? raw(`<span class="badge-media">📎 Медиа</span>`) : ''}
            ${m.post_url ? raw(html`<a href="${m.post_url}" target="_blank" class="post-link-btn" style="margin-left: auto;">🔗 Открыть в TG</a>`) : ''}
          </div>
        </div>
      `).join('');
    }
  }
}

window.selectFeedItem = selectFeedItem;

if (refreshFeedBtn) {
  refreshFeedBtn.addEventListener('click', () => {
    loadFeed();
    showToast('Лента обновлена');
  });
}

if (reanalyzeFeedItemBtn) {
  reanalyzeFeedItemBtn.addEventListener('click', async () => {
    if (!selectedFeedId) return;
    reanalyzeFeedItemBtn.disabled = true;
    reanalyzeFeedItemBtn.textContent = '⏳ Анализ LLM...';
    showToast('Запуск анализа постов задачи через OpenRouter...');

    try {
      const res = await apiFetch(`/api/feed/${selectedFeedId}/reanalyze`, { method: 'POST' });
      const data = await res.json();
      reanalyzeFeedItemBtn.disabled = false;
      reanalyzeFeedItemBtn.textContent = '🔄 Обновить анализ';

      if (res.ok && data.status === 'success') {
        showToast('✨ AI Анализ успешно сформирован и сохранен!');
        const idx = currentFeed.findIndex(f => f.id === selectedFeedId);
        if (idx >= 0) {
          currentFeed[idx] = data.feed_item;
        }
        renderFeedList();
        selectFeedItem(selectedFeedId);
      } else {
        showToast(data.detail || 'Ошибка выполнения анализа', true);
      }
    } catch (e) {
      reanalyzeFeedItemBtn.disabled = false;
      reanalyzeFeedItemBtn.textContent = '🔄 Обновить анализ';
      showToast('Ошибка сети: ' + e.message, true);
    }
  });
}

if (copyFeedSummaryBtn) {
  copyFeedSummaryBtn.addEventListener('click', () => {
    const item = currentFeed.find(f => f.id === selectedFeedId);
    if (!item || !item.ai_analysis) {
      showToast('Нет текста Summary для копирования', true);
      return;
    }
    navigator.clipboard.writeText(item.ai_analysis);
    showToast('✨ AI Summary скопировано в буфер обмена!');
  });
}

if (deleteFeedItemBtn) {
  deleteFeedItemBtn.addEventListener('click', async () => {
    if (!selectedFeedId) return;
    if (!confirm('Удалить эту карточку из ленты?')) return;
    try {
      const res = await apiFetch(`/api/feed/${selectedFeedId}`, { method: 'DELETE' });
      if (res.ok) {
        showToast('Карточка удалена из ленты');
        selectedFeedId = null;
        loadFeed();
      }
    } catch (e) {
      showToast('Ошибка удаления', true);
    }
  });
}