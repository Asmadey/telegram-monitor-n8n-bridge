// account.js — забрать своё и уйти (13.2).
//
// Две кнопки в кабинете, потому что право, до которого не дойти из
// интерфейса, правом не является: сегодня единственный способ удалить
// аккаунт — просить владельца сервиса почистить базу руками.
//
// Удаление спрашивает адрес учётной записи, а не «вы уверены?». Необратимое
// действие не должно случаться с одного нажатия, а адрес нельзя набрать по
// инерции. Сервер проверяет то же самое сам — подтверждение в браузере это
// удобство, а не защита.

import { apiFetch, apiGet } from './api.js';
import { showToast } from './render.js';

const exportAccountBtn = document.getElementById('exportAccountBtn');
const deleteAccountBtn = document.getElementById('deleteAccountBtn');

async function exportAccount() {
  exportAccountBtn.disabled = true;
  exportAccountBtn.textContent = 'Готовим файл...';
  try {
    const res = await apiGet('/api/account/export');
    if (!res.ok) throw new Error('Не удалось собрать выгрузку');
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `teleton-export-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast('Выгрузка скачана');
  } catch (e) {
    showToast(e.message, true);
  } finally {
    exportAccountBtn.disabled = false;
    exportAccountBtn.textContent = '📦 Скачать мои данные';
  }
}

async function deleteAccount() {
  const typed = prompt(
    'Удаление аккаунта необратимо: уйдут источники, лента, журнал и ключи. ' +
    'Сессия в Telegram будет завершена.\n\n' +
    'Введите адрес своей учётной записи, чтобы подтвердить:'
  );
  if (!typed) return;

  deleteAccountBtn.disabled = true;
  try {
    const res = await apiFetch('/api/account', {
      method: 'DELETE',
      body: { confirm: typed },
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Не удалось удалить аккаунт');
    // Сессия больше не существует — оставаться в оболочке не на чем.
    window.location.replace('/login');
  } catch (e) {
    showToast(e.message, true);
    deleteAccountBtn.disabled = false;
  }
}

if (exportAccountBtn) exportAccountBtn.addEventListener('click', exportAccount);
if (deleteAccountBtn) deleteAccountBtn.addEventListener('click', deleteAccount);
