/* Shared dashboard jobs; read state stays in this browser. */
(() => {
  const bell = document.querySelector('[data-job-bell]');
  if (!bell) return;
  const badge = bell.querySelector('[data-job-count]');
  const status = bell.querySelector('[data-job-status]');
  const list = bell.querySelector('[data-job-items]');
  const key = 'radar.preparation.seen.v1';
  let seen = new Set(), items = [], timer, pending = false;
  const read = () => {
    try { const ids = JSON.parse(localStorage.getItem(key) || '[]'); if (Array.isArray(ids)) seen = new Set(ids.slice(-200)); } catch (_) { /* Private browsing. */ }
  };
  read();
  const terminal = item => ['done', 'failed'].includes(item.state);
  const markRead = () => {
    items.filter(terminal).forEach(item => seen.add(item.id));
    seen = new Set([...seen].slice(-200));
    try { localStorage.setItem(key, JSON.stringify([...seen])); } catch (_) { /* In-memory read state still works. */ }
  };
  const render = () => {
    if (bell.open) markRead();
    const unread = items.filter(item => terminal(item) && !seen.has(item.id)).length;
    badge.hidden = !unread;
    badge.textContent = String(unread);
    bell.querySelector('summary').setAttribute('aria-label', `작업 알림${unread ? ` · 새 알림 ${unread}개` : ''}`);
    list.replaceChildren();
    items.forEach(item => {
      const link = document.createElement('a');
      link.href = item.url;
      link.className = `job-item ${item.state}`;
      const company = document.createElement('strong');
      company.textContent = item.company;
      const label = document.createElement('span');
      label.textContent = item.label;
      const at = document.createElement('small');
      const date = new Date(item.at);
      at.textContent = Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(date) + ' KST';
      link.append(company, label, at);
      list.append(link);
    });
  };
  const poll = async () => {
    clearTimeout(timer);
    if (pending || document.hidden) return;
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/preparation/notifications', {signal: controller.signal, cache: 'no-store', headers: {Accept: 'application/json'}});
      if (!response.ok || response.redirected) throw new Error('unavailable');
      const data = await response.json();
      items = data.items;
      status.textContent = items.length ? '완료된 작업을 눌러 결과를 확인하세요.' : '아직 제안 준비 작업이 없습니다.';
      render();
    } catch (_) { status.textContent = '알림을 갱신하지 못했습니다. 잠시 후 다시 확인합니다.'; }
    finally {
      clearTimeout(timeout);
      pending = false;
      if (!document.hidden) timer = setTimeout(poll, 15000);
    }
  };
  bell.addEventListener('toggle', () => { if (bell.open) { render(); poll(); } });
  document.addEventListener('visibilitychange', poll);
  document.addEventListener('click', event => { if (!bell.contains(event.target)) bell.open = false; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && bell.open) { bell.open = false; bell.querySelector('summary').focus(); } });
  window.addEventListener('storage', event => { if (event.key === key) { read(); render(); } });
  poll();
})();
