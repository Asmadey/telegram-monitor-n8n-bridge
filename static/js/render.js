// render.js — общие помощники DOM: экранирование, форматирование,
// модалки и тосты (задача 5.1, разрез index.html).
//
// Задача 5.2 — экранирование ПО УМОЛЧАНИЮ: html — единственный способ
// строить разметку с данными. Это tagged-template: каждая подстановка
// ${...} экранируется сама (включая кавычки — безопасно и в текстовом
// узле, и внутри атрибута); raw() — осознанный opt-out для готового
// безопасного HTML (например raw(formatTelegramText(...)) — виден в
// ревью, XSS-сканер 0.4 treats raw( как отсутствие экранирования).
// Прямой innerHTML со строковой интерполяцией запрещён (test_48).
//
// escapeHtml остаётся для точек вне билдера: атрибуты внутри raw()
// и значения, экранируемые до построения разметки.

export function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Чистый (без DOM) escaper для билдера: исполним и в node (test_48),
// и в браузере; порядок важен — & первым, иначе двойное экранирование.
function escapeAll(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

const RAW = Symbol('html-raw');

// Метка «значение уже безопасно»: внутри html` вставляется как есть.
export function raw(value) {
  return { [RAW]: true, value: String(value) };
}

// html`<a href="${url}">${title}</a>` — каждая подстановка экранируется
// автоматически; числа и null/undefined не калечатся.
export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => {
    if (value !== null && value !== undefined) {
      out += value && typeof value === 'object' && RAW in value
        ? value.value
        : escapeAll(value);
    }
    out += strings[i + 1];
  });
  return out;
}

export function formatTelegramText(text) {
  if (!text) return '';
  // 1. Безопасное экранирование спецсимволов HTML
  let safe = escapeHtml(text);

  // 2. Жирный шрифт: **текст** -> <strong>текст</strong>
  safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // 3. Моноширинный инлайн-код: `текст` -> <code>текст</code>
  safe = safe.replace(/`([^`]+)`/g, '<code style="background: var(--canvas-soft); padding: var(--space-xxs) var(--space-xs); border-radius: var(--rounded-xs); font-family: monospace; font-size: var(--text-eyebrow-sm); border: 1px solid var(--hairline);">$1</code>');

  return safe;
}

export function formatRelativeTime(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  const now = new Date();
  const diffSec = Math.floor((now - d) / 1000);
  if (diffSec < 45) return 'только что';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} мин назад`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours} ч назад`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays === 1) return 'вчера';
  if (diffDays < 7) return `${diffDays} дн назад`;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function formatIntervalHuman(minutes) {
  if (!minutes) return 'Каждый час';
  if (minutes === 1440) return 'Раз в сутки (24 ч)';
  if (minutes >= 60 && minutes % 60 === 0) {
    const h = minutes / 60;
    return h === 1 ? 'Каждый 1 час' : `Каждые ${h} ч`;
  }
  if (minutes >= 60) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return `Каждые ${h}ч ${m}м`;
  }
  return `Каждые ${minutes} мин`;
}

// Расчётное время следующего прогона источника — ТЕКСТОМ, а не разметкой.
// Прежняя версия (до 11.7) возвращала HTML и вставлялась через `raw()`; всё,
// что идёт через `raw()`, обязано быть доверенным навсегда, включая правки,
// которых ещё нет, а экранирование по умолчанию (5.2) держится ровно на том,
// что таких мест мало. Здесь доверять нечему: строку соберёт html``.
//
// `now` — параметр, а не `new Date()` внутри: иначе расчёт нельзя проверить,
// не подменяя часы всему процессу.
export function formatNextRun(source, now = new Date()) {
  if (!source || !source.is_active) return 'На паузе';
  const interval = Number(source.interval_minutes) || 60;
  const last = source.last_run_at ? new Date(source.last_run_at) : null;
  // Источник без прогонов и источник с просроченным прогоном — одно и то же
  // состояние: воркер возьмёт оба ближайшим тиком. Различает их соседний
  // сегмент («Последний: ещё не было»), и повторять это здесь незачем.
  if (last === null || Number.isNaN(last.getTime())) return 'Следующий: сейчас';
  const next = new Date(last.getTime() + interval * 60000);
  const minutes = Math.round((next.getTime() - now.getTime()) / 60000);
  if (minutes <= 0) return 'Следующий: сейчас';
  const time = next.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (minutes < 60) return `Следующий: ${time} (через ${minutes} мин)`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest
    ? `Следующий: ${time} (через ${hours} ч ${rest} мин)`
    : `Следующий: ${time} (через ${hours} ч)`;
}

// Значок исхода проверки канала (13.6): цвет, подпись и пояснение.
//
// Три тона, а не два. «Не проверяли» — отдельное состояние: пустое поле,
// нарисованное зелёным, — тот же класс ошибки, что «совпадений нет» против
// «не смогли посмотреть» (фаза 9). Поэтому непроверенный канал выглядит
// серым и говорит об этом словами.
//
// Пояснение берётся С СЕРВЕРА: именно там известно, ЧТО нашлось по адресу и
// почему не читается. Придумывать текст здесь значило бы потерять причину —
// ровно то, из-за чего человека отсылали в журнал.
// Имя класса тут ПОЛНОЕ, а не собранное из кусков (`check-dot-${tone}`).
// Собранное имя не находится ничем — ни поиском по проекту, ни свипом
// осиротевших стилей (13.6 поймал это на себе же: четыре правила выглядели
// мусором ровно потому, что их имена нигде не написаны целиком).
const CHECK_TONES = {
  ok: { cls: 'check-dot-ok', mark: '✓', fallback: 'канал читается' },
  no_posts: { cls: 'check-dot-warn', mark: '!', fallback: 'читается, но текста нет' },
  duplicate: { cls: 'check-dot-warn', mark: '!', fallback: 'дубль внутри источника' },
  not_found: { cls: 'check-dot-bad', mark: '×', fallback: 'адрес не открылся' },
  no_access: { cls: 'check-dot-bad', mark: '×', fallback: 'история не читается' },
  error: { cls: 'check-dot-bad', mark: '×', fallback: 'проверка не удалась' },
};

export function checkBadge(channel) {
  const status = (channel || {}).check_status;
  const known = CHECK_TONES[status];
  if (!known) {
    return {
      cls: 'check-dot-unknown',
      mark: '?',
      title: 'Канал ещё не проверяли — нажмите «Проверить»',
    };
  }
  return {
    cls: known.cls,
    mark: known.mark,
    title: (channel || {}).check_detail || known.fallback,
  };
}

// Значок заполненности промпта извлечения (13.7). Пустой промпт — законное
// состояние базы, но не рабочее: канал разбирается ничем. Красный тут значит
// «это надо заполнить», а не «сломалось».
//
// Имена классов, как и у `checkBadge`, написаны ЦЕЛИКОМ: собранное из кусков
// имя (`prompt-dot-${tone}`) не находится ничем — ни поиском по проекту, ни
// свипом осиротевших стилей.
export function promptBadge(prompt) {
  // Подпись словом, а не глиф. Первая версия рисовала карандаш — тот же знак,
  // что и у кнопки правки рядом: в строке оказывалось два карандаша подряд,
  // зелёный и серый, и первый читался как вторая кнопка (найдено владельцем).
  // Показатель состояния не должен выглядеть как действие.
  //
  // Строка из пробелов — пустое поле: модель получит ровно столько же
  // указаний, сколько при пустом, а зелёная подпись сказала бы обратное.
  const filled = String(prompt || '').trim().length > 0;
  return filled
    ? {
        cls: 'prompt-flag-ok',
        label: 'Промпт',
        title: 'Промпт извлечения задан',
      }
    : {
        cls: 'prompt-flag-bad',
        label: 'Промпт',
        title: 'Промпт извлечения пуст — каналу нечем объяснить, что искать',
      };
}

// Строка канала в окне «Параметры источника» (13.7).
//
// Свёрнутая по умолчанию: имя, лимит текстом, два значка, карандаш и корзина.
// Развёрнутая часть — та же, что была всегда, но под `hidden`.
//
// **Поля живут в документе ВСЕГДА, свёрнутость — это `hidden`.** Сохранение
// обходит `[data-channel-row]` и читает `.channel-limit`,
// `.channel-token-limit`, `.channel-prompt` у каждой строки; если у свёрнутой
// строки полей нет, `querySelector` вернёт `null`, `.value` уронит цикл, и
// каналы после первого молча не сохранятся. Так уже было — закрыто `test_121`.
//
// Скрыто разметкой, а не скриптом: до первого исполнения скрипта (или при его
// отказе) человек увидел бы ровно то, от чего уходим, — девять развёрнутых
// полей (урок 13.1).
export function channelRowMarkup(channel) {
  const name = channel.chat_title || channel.chat_target;
  const link = checkBadge(channel);
  const prompt = promptBadge(channel.extract_prompt);
  return html`
    <div class="card channel-row" data-channel-row="${channel.channel_id}">
      <div class="channel-row-head">
        <span class="check-dot ${link.cls}" title="${link.title}">${link.mark}</span>
        <b class="channel-row-name" title="${channel.chat_target}">${name}</b>
        <span class="channel-row-limit">Лимит <b data-limit-label>${channel.limit}</b></span>
        <span class="prompt-flag ${prompt.cls}" title="${prompt.title}">${prompt.label}</span>
        <span class="channel-row-actions">
          <button class="btn btn-secondary btn-icon-sm" type="button" data-action="edit-channel" title="Изменить лимит и промпт">✎</button>
          <button class="btn btn-danger btn-icon-sm" type="button" data-action="remove-channel" data-channel-id="${channel.channel_id}" title="Убрать канал из источника">🗑</button>
        </span>
        <span class="channel-row-confirm" hidden>
          <span class="channel-row-ask">Удалить канал «${name}»?</span>
          <button class="btn btn-danger btn-sm" type="button" data-action="confirm-remove-channel" data-channel-id="${channel.channel_id}">Да</button>
          <button class="btn btn-secondary btn-sm" type="button" data-action="cancel-remove-channel">Нет</button>
        </span>
      </div>
      <div class="channel-row-edit" hidden>
        <textarea class="channel-prompt" rows="3" placeholder="Что извлекать именно из этого канала">${channel.extract_prompt || ''}</textarea>
        <div class="channel-row-fields">
          <label class="channel-row-field">Лимит <input type="number" class="channel-limit" min="1" max="200" value="${channel.limit}"></label>
          <label class="channel-row-field" title="Потолок расхода токенов на этот канал за месяц. 0 — без потолка">Токены <input type="number" class="channel-token-limit" min="0" step="1000" value="${channel.token_limit || 0}"></label>
        </div>
      </div>
    </div>
  `;
}

export function openModalAnimated(modalEl) {
  if (!modalEl) return;
  modalEl.classList.add('active');
  const inner = modalEl.querySelector('.modal');
  if (window.gsap && inner) {
    gsap.fromTo(modalEl, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.2, ease: "power2.out" });
    gsap.fromTo(inner,
      { scale: 0.94, y: 12, autoAlpha: 0 },
      { scale: 1, y: 0, autoAlpha: 1, duration: 0.28, ease: "back.out(1.3)" }
    );
  }
}

export function closeModalAnimated(modalEl) {
  if (!modalEl) return;
  const inner = modalEl.querySelector('.modal');
  if (window.gsap && inner) {
    gsap.to(inner, { scale: 0.96, y: 8, autoAlpha: 0, duration: 0.18, ease: "power2.in" });
    gsap.to(modalEl, {
      autoAlpha: 0,
      duration: 0.2,
      ease: "power2.in",
      onComplete: () => {
        modalEl.classList.remove('active');
        gsap.set([modalEl, inner], { clearProps: "all" });
      }
    });
  } else {
    modalEl.classList.remove('active');
  }
}

let toastTimer = null;

export function showToast(msg, isError = false) {
  const toast = document.getElementById('toast');
  const toastMsg = document.getElementById('toastMsg');
  toastMsg.textContent = msg;
  toast.className = `toast show ${isError ? 'error' : ''}`;

  if (window.gsap) {
    gsap.killTweensOf(toast);
    if (toastTimer) clearTimeout(toastTimer);

    gsap.fromTo(toast,
      { y: 30, autoAlpha: 0, scale: 0.95 },
      { y: 0, autoAlpha: 1, scale: 1, duration: 0.3, ease: "power3.out" }
    );

    toastTimer = setTimeout(() => {
      gsap.to(toast, {
        y: -10,
        autoAlpha: 0,
        scale: 0.97,
        duration: 0.22,
        ease: "power2.in",
        onComplete: () => {
          toast.className = 'toast';
          gsap.set(toast, { clearProps: "all" });
        }
      });
    }, 3200);
  } else {
    setTimeout(() => { toast.className = 'toast'; }, 3500);
  }
}