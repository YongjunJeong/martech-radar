// Form controls are the selection state; the server owns counts and verdicts.
(() => {
  const form = document.querySelector('[data-filter-form]');
  let results = document.querySelector('[data-sales-results]');
  if (!form || !results) return;
  const notice = form.querySelector('[data-filter-status]');
  const message = notice.querySelector('[data-filter-message]');
  const retry = notice.querySelector('[data-filter-retry]');
  const reset = form.querySelector('[data-filter-reset]');
  let timer;
  let controller;
  let revision = 0;
  let retryRequest;
  let composing = false;

  const refresh = () => {
    for (const group of form.querySelectorAll('[data-filter-group]')) {
      const name = group.dataset.filterGroup;
      const values = group.querySelector('[data-filter-values]');
      const checked = [...form.querySelectorAll('input[type="checkbox"]')]
        .filter(input => input.name === name && input.checked);
      group.querySelector('[data-filter-clear]').hidden = checked.length === 0;
      values.replaceChildren();
      for (const input of checked) {
        const chip = document.createElement('span');
        chip.className = name === 'observation'
          ? `observation-tag pattern-${input.value}` : 'filter-value';
        const label = input.closest('label').querySelector('span').textContent;
        const text = document.createElement('span');
        text.textContent = label;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'filter-remove';
        remove.dataset.filterRemove = input.value;
        remove.setAttribute('aria-label', `${group.dataset.filterTitle}: ${label} 해제`);
        remove.title = `${label} 해제`;
        const icon = document.createElement('span');
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = '×';
        remove.append(icon);
        chip.append(text, remove);
        values.append(chip);
      }
      if (!checked.length) {
        const empty = document.createElement('span');
        empty.className = 'filter-summary-empty';
        empty.textContent = group.dataset.emptyLabel;
        values.append(empty);
      }
    }
    reset.hidden = !form.elements.q.value && !form.querySelector('input[type="checkbox"]:checked');
  };
  const formURL = () => {
    const url = new URL(location.pathname, location.origin);
    url.search = new URLSearchParams(new FormData(form)).toString();
    return url;
  };
  const restore = () => {
    const params = new URL(location.href).searchParams;
    form.elements.q.value = params.get('q') || '';
    for (const input of form.querySelectorAll('input[type="checkbox"]')) {
      input.checked = params.getAll(input.name).includes(input.value);
    }
    refresh();
  };
  const report = (text, failed = false) => {
    message.textContent = text;
    notice.hidden = false;
    retry.hidden = !failed;
  };

  const load = async (url, ticket, historyMode, focusResults) => {
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort(), 15000);
    try {
      const response = await fetch(url, {signal: request.signal, credentials: 'same-origin',
        headers: {'Accept': 'text/html'}});
      if (ticket !== revision) return;
      if (response.redirected && new URL(response.url).pathname === '/login') {
        location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      if (ticket !== revision) return;
      const next = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-sales-results]');
      if (!next) throw new Error('Missing search results');
      // Counts, cards, state links and pagination arrive together in one snapshot.
      const replacement = document.importNode(next, true);
      results.replaceWith(replacement);
      results = replacement;
      url.searchParams.set('status', results.dataset.status);
      if (url.searchParams.has('page')) url.searchParams.set('page', results.dataset.page);
      if (historyMode === 'push' && url.href !== location.href) history.pushState(null, '', url);
      report(`${results.dataset.count}곳 표시 중`);
      if (focusResults) results.querySelector('#company-list-title').focus({preventScroll: true});
      else if (document.activeElement === retry) form.elements.q.focus({preventScroll: true});
    } catch (error) {
      if (ticket !== revision) return;
      // Keep old results visibly unavailable rather than presenting them as a new match.
      report('결과를 갱신하지 못했습니다. 아래는 이전 결과입니다. 다시 시도해 주세요.', true);
    } finally {
      clearTimeout(timeout);
      if (ticket === revision) controller = null;
    }
  };
  const schedule = (delay = 200, url = formURL(), historyMode = 'push', focusResults = false) => {
    retryRequest = [url, historyMode, focusResults];
    clearTimeout(timer);
    controller?.abort();
    const ticket = ++revision; // Invalidate old responses before the debounce expires.
    results.setAttribute('aria-busy', 'true');
    results.inert = true;
    report('갱신 중…');
    timer = setTimeout(() => load(url, ticket, historyMode, focusResults), delay);
  };
  form.addEventListener('change', event => {
    if (event.target.type !== 'checkbox') return;
    refresh();
    schedule();
  });
  form.addEventListener('input', event => {
    if (event.target.name === 'q' && !event.isComposing && !composing) {
      refresh();
      schedule(350);
    }
  });
  form.addEventListener('compositionstart', event => {
    if (event.target.name !== 'q') return;
    composing = true;
    clearTimeout(timer);
    controller?.abort();
    ++revision;
  });
  form.addEventListener('compositionend', event => {
    if (event.target.name === 'q') { composing = false; refresh(); schedule(350); }
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (composing) return;
    schedule(0);
  });
  form.addEventListener('click', event => {
    const button = event.target.closest('[data-filter-remove], [data-filter-clear]');
    if (button) {
      const group = button.closest('[data-filter-group]');
      const removeOne = button.hasAttribute('data-filter-remove');
      for (const input of form.querySelectorAll('input[type="checkbox"]')) {
        if (input.name === group.dataset.filterGroup
            && (!removeOne || input.value === button.dataset.filterRemove)) input.checked = false;
      }
      refresh();
      (group.querySelector('[data-filter-remove]') || form.querySelector('.filter-summary'))
        .focus({preventScroll: true});
      schedule();
    }
    if (event.target.closest('[data-filter-retry]')) schedule(0, ...retryRequest);
    if (event.target.closest('[data-filter-reset]')) {
      event.preventDefault();
      for (const input of form.querySelectorAll('input[type="checkbox"]')) input.checked = false;
      form.elements.q.value = '';
      refresh();
      form.querySelector('.filter-summary').focus({preventScroll: true});
      schedule(0);
    }
  });
  document.addEventListener('click', event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('[data-sales-results] .sales-stats a, [data-sales-results] .pagination a, [data-sales-results] .all-companies, [data-sales-results] .sales-empty a');
    if (!link) return;
    event.preventDefault();
    if (results.inert) return;
    schedule(0, new URL(link.href), 'push', true);
  });
  window.addEventListener('popstate', () => {
    restore();
    schedule(0, new URL(location.href), 'none');
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) {
      restore();
      schedule(0, new URL(location.href), 'none');
    }
  });
  refresh();
  report('입력·선택하면 자동 반영');
})();
