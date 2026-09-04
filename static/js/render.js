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
// escapeHtml/esc остаются для точек вне билдера: атрибуты внутри raw()
// и значения, экранируемые до построения разметки.

export function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Короткий алиас + экранирование кавычек: безопасен и в текстовом узле,
// и внутри атрибута (escapeHtml кавычки не экранирует).
export function esc(text) {
  return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
  safe = safe.replace(/`([^`]+)`/g, '<code style="background: var(--canvas-soft); padding: 2px 5px; border-radius: var(--rounded-xs); font-family: monospace; font-size: 12px; border: 1px solid var(--hairline);">$1</code>');

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

// Форматирование времени следующей отправки (last_checked + interval)
export function formatNextRun(m) {
  if (!m.is_active) return '<span style="color: var(--mute);">На паузе</span>';
  if (!m.last_checked && !m.next_run) return '<span style="color: var(--accent-blue-deep);">При первом запуске</span>';

  try {
    const nextDate = m.next_run ? new Date(m.next_run) : new Date(new Date(m.last_checked).getTime() + (m.interval_minutes || 60) * 60000);
    const now = new Date();
    const diffMs = nextDate.getTime() - now.getTime();
    const timeFormatted = nextDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    if (diffMs <= 0) {
      return `<b style="color: #008715;">Сейчас</b> (${timeFormatted})`;
    }

    const diffMins = Math.round(diffMs / 60000);
    if (diffMins < 60) {
      return `<b>${timeFormatted}</b> <span style="opacity: 0.75; font-size: 11px;">(${diffMins} мин)</span>`;
    } else {
      const hours = Math.floor(diffMins / 60);
      const remainingMins = diffMins % 60;
      return `<b>${timeFormatted}</b> <span style="opacity: 0.75; font-size: 11px;">(${hours}ч ${remainingMins}м)</span>`;
    }
  } catch (e) {
    return '—';
  }
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